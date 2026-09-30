import io
import json
import unittest
from urllib.error import HTTPError, URLError

from poc import llm, pricing

SENTINEL = "sk-SENTINEL-7f3a9c2e5b1d4a68"
ENV = {"DEEPSEEK_API_KEY": SENTINEL}


def completion(content="{}", finish="stop", usage=None, reasoning=None):
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "id": "req-1",
        "model": "deepseek-flash",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
    }


class Recorder:
    """假 transport：记录请求，返回预设响应或抛出预设异常。"""

    def __init__(self, response=None, status=200, exc=None):
        self.response = response if response is not None else completion()
        self.status = status
        self.exc = exc
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        body = self.response if isinstance(self.response, bytes) else json.dumps(self.response).encode()
        return self.status, body


def http_error(code, body=b""):
    return HTTPError("https://api.deepseek.com/chat/completions", code, "err", {}, io.BytesIO(body))


def call(transport, **kw):
    return llm.chat("deepseek", "deepseek-flash", [{"role": "user", "content": "json"}], env=ENV, transport=transport, **kw)


class ChatTest(unittest.TestCase):
    def test_request_body_and_headers(self):
        t = Recorder()
        call(t, max_tokens=100, temperature=0.7, extra_body={"thinking": {"type": "disabled"}})
        req = t.requests[0]
        self.assertEqual(req.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(req.get_method(), "POST")
        body = json.loads(req.data)
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["max_tokens"], 100)
        self.assertEqual(body["temperature"], 0.7)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertFalse(body["stream"])
        self.assertNotIn(SENTINEL, req.data.decode())
        self.assertEqual(req.get_header("Authorization"), f"Bearer {SENTINEL}")

    def test_json_mode_off(self):
        t = Recorder()
        call(t, json_mode=False)
        self.assertNotIn("response_format", json.loads(t.requests[0].data))

    def test_success(self):
        res = call(Recorder(completion('{"a":1}', reasoning="想一想")))
        self.assertEqual(res.text, '{"a":1}')
        self.assertEqual(res.finish_reason, "stop")
        self.assertEqual(res.request_id, "req-1")
        self.assertEqual(res.reasoning_chars, 3)
        self.assertEqual(res.usage["completion_tokens"], 5)

    def test_truncated_keeps_usage(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 8192}
        with self.assertRaises(llm.LLMError) as cm:
            call(Recorder(completion('{"a":', finish="length", usage=usage)))
        self.assertEqual(cm.exception.kind, "truncated")
        self.assertEqual(cm.exception.usage, usage)

    def test_empty_content(self):
        with self.assertRaises(llm.LLMError) as cm:
            call(Recorder(completion("  ")))
        self.assertEqual(cm.exception.kind, "empty")

    def test_http_errors(self):
        for code in (401, 429, 500, 503):
            with self.subTest(code=code), self.assertRaises(llm.LLMError) as cm:
                call(Recorder(exc=http_error(code, f"bad key {SENTINEL}".encode())))
            self.assertEqual(cm.exception.kind, "http")
            self.assertEqual(cm.exception.http_status, code)
            self.assertNotIn(SENTINEL, str(cm.exception))

    def test_non_2xx_status(self):
        with self.assertRaises(llm.LLMError) as cm:
            call(Recorder(status=302))
        self.assertEqual(cm.exception.kind, "http")

    def test_network_errors(self):
        for exc in (URLError("Tunnel connection failed: 403"), TimeoutError("timed out")):
            with self.subTest(exc=exc), self.assertRaises(llm.LLMError) as cm:
                call(Recorder(exc=exc))
            self.assertEqual(cm.exception.kind, "network")

    def test_bad_response(self):
        for raw in (b"not json", json.dumps({"choices": []}).encode()):
            with self.subTest(raw=raw), self.assertRaises(llm.LLMError) as cm:
                call(Recorder(response=raw))
            self.assertEqual(cm.exception.kind, "bad_response")

    def test_missing_key(self):
        with self.assertRaises(llm.LLMError) as cm:
            llm.chat("deepseek", "m", [], env={}, transport=Recorder())
        self.assertEqual(cm.exception.kind, "config")


class PricingTest(unittest.TestCase):
    def test_cache_split(self):
        p = pricing.price_for("deepseek", "deepseek-flash")
        usage = {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 600, "prompt_cache_miss_tokens": 400, "completion_tokens": 500}
        expected = (600 * p.input_cache_hit + 400 * p.input_cache_miss + 500 * p.output) / 1e6
        self.assertAlmostEqual(pricing.estimate_cny("deepseek", "deepseek-flash", usage), expected)

    def test_no_cache_detail_counts_as_miss(self):
        p = pricing.price_for("deepseek", "deepseek-v4-pro")
        usage = {"prompt_tokens": 1000, "completion_tokens": 0}
        self.assertAlmostEqual(pricing.estimate_cny("deepseek", "deepseek-v4-pro", usage), 1000 * p.input_cache_miss / 1e6)

    def test_unknown_model_uses_fallback(self):
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
        self.assertEqual(pricing.estimate_cny("deepseek", "unknown", usage), pricing.FALLBACK.input_cache_miss)

    def test_empty_usage(self):
        self.assertEqual(pricing.estimate_cny("deepseek", "deepseek-flash", None), 0.0)


if __name__ == "__main__":
    unittest.main()
