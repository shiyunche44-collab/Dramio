import base64
import json
import unittest

from poc import speech

SENTINEL = "vs-SENTINEL-4c1e9a7d2b"
ENV = {"VOLC_SPEECH_API_KEY": SENTINEL}


def tts_body(chunks=(b"abc", b"def"), final=True, usage=11, extra_lines=()):
    lines = [json.dumps({"code": 0, "message": "", "data": base64.b64encode(c).decode()}) for c in chunks]
    lines.append(json.dumps({"code": 0, "message": "", "data": None, "sentence": {"text": "x"}}))
    lines.extend(extra_lines)
    if final:
        lines.append(json.dumps({"code": 20000000, "message": "ok", "data": None, "usage": {"text_words": usage}}))
    return ("\n".join(lines) + "\n").encode()


class Fake:
    """假 transport：按顺序返回预设的 Response 或抛出异常，并记录请求。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(body, **headers):
    return speech.Response(200, {k.lower().replace("_", "-"): v for k, v in headers.items()}, body)


class TTSTest(unittest.TestCase):
    def test_stream_is_joined_and_headers_sent(self):
        t = Fake(ok(tts_body(), x_tt_logid="log-1"))
        res = speech.synthesize("你好。", "zh_female_vv_uranus_bigtts", env=ENV, transport=t)
        self.assertEqual((res.audio, res.text_words, res.chunks, res.logid), (b"abcdef", 11, 2, "log-1"))
        req = t.requests[0]
        self.assertEqual(req.full_url, "https://openspeech.bytedance.com/api/v3/tts/unidirectional")
        self.assertEqual(req.get_header("X-api-key"), SENTINEL)
        self.assertEqual(req.get_header("X-api-resource-id"), "seed-tts-2.0")
        self.assertEqual(req.get_header("X-control-require-usage-tokens-return"), "text_words")
        body = json.loads(req.data)
        self.assertEqual(body["req_params"]["speaker"], "zh_female_vv_uranus_bigtts")
        self.assertNotIn("additions", body["req_params"])
        self.assertNotIn("speech_rate", body["req_params"]["audio_params"])

    def test_instruction_and_rate(self):
        body = speech.build_tts_body("好", "v", speech_rate=-10, context_text="你可以用愤怒的语气说这句话吗？")
        self.assertEqual(body["req_params"]["audio_params"]["speech_rate"], -10)
        additions = body["req_params"]["additions"]
        self.assertIsInstance(additions, str)  # 接口要求 JSON 字符串
        self.assertEqual(json.loads(additions), {"context_texts": ["你可以用愤怒的语气说这句话吗？"]})

    def test_missing_final_block_is_truncated_and_transient(self):
        with self.assertRaises(speech.SpeechError) as cm:
            speech.synthesize("x", "v", env=ENV, transport=Fake(ok(tts_body(final=False))))
        self.assertEqual(cm.exception.kind, "truncated")
        self.assertTrue(cm.exception.transient)

    def test_partial_last_line_is_truncated(self):
        body = tts_body(final=False) + b'{"code":0,"data":"YWJ'
        with self.assertRaises(speech.SpeechError) as cm:
            speech.parse_tts_stream(body)
        self.assertEqual(cm.exception.kind, "truncated")

    def test_api_error_code(self):
        bad = json.dumps({"code": 45000000, "message": "speaker permission denied"})
        with self.assertRaises(speech.SpeechError) as cm:
            speech.parse_tts_stream(tts_body(final=False, extra_lines=[bad]))
        self.assertEqual((cm.exception.kind, cm.exception.api_code, cm.exception.transient), ("api_error", 45000000, False))
        busy = json.dumps({"code": 55000000, "message": "server error"})
        with self.assertRaises(speech.SpeechError) as cm:
            speech.parse_tts_stream(tts_body(final=False, extra_lines=[busy]))
        self.assertTrue(cm.exception.transient)
        limit = json.dumps({"code": 45000000, "message": "quota exceeded for types: concurrency"})
        with self.assertRaises(speech.SpeechError) as cm:
            speech.parse_tts_stream(tts_body(final=False, extra_lines=[limit]))
        self.assertTrue(cm.exception.transient)

    def test_empty_audio(self):
        with self.assertRaises(speech.SpeechError) as cm:
            speech.parse_tts_stream(tts_body(chunks=()))
        self.assertEqual((cm.exception.kind, cm.exception.transient), ("empty", False))

    def test_bad_lines(self):
        with self.assertRaises(speech.SpeechError) as cm:
            speech.parse_tts_stream(b"not json\n" + tts_body())
        self.assertEqual(cm.exception.kind, "bad_response")
        with self.assertRaises(speech.SpeechError) as cm:
            speech.parse_tts_stream(tts_body() + b'{"code":0,"data":"YQ=="}\n')
        self.assertEqual(cm.exception.kind, "bad_response")

    def test_http_errors(self):
        body = json.dumps({"header": {"code": 45000030, "message": "requested resource not granted " + SENTINEL}}).encode()
        with self.assertRaises(speech.SpeechError) as cm:
            speech.synthesize("x", "v", env=ENV, transport=Fake(speech.Response(403, {}, body)))
        exc = cm.exception
        self.assertEqual((exc.kind, exc.http_status, exc.api_code, exc.transient), ("http", 403, 45000030, False))
        self.assertNotIn(SENTINEL, str(exc))
        with self.assertRaises(speech.SpeechError) as cm:
            speech.synthesize("x", "v", env=ENV, transport=Fake(speech.Response(503, {}, b"")))
        self.assertTrue(cm.exception.transient)

    def test_network_error_is_redacted(self):
        t = Fake(speech.SpeechError("network", f"ConnectionResetError: reset {SENTINEL}"))
        with self.assertRaises(speech.SpeechError) as cm:
            speech.synthesize("x", "v", env=ENV, transport=t)
        self.assertTrue(cm.exception.transient)
        self.assertNotIn(SENTINEL, str(cm.exception))

    def test_missing_key(self):
        with self.assertRaises(speech.SpeechError) as cm:
            speech.synthesize("x", "v", env={}, transport=Fake())
        self.assertEqual((cm.exception.kind, cm.exception.transient), ("config", False))


def status(code, body=b"{}"):
    return ok(body, x_api_status_code=str(code), x_api_message="m")


DONE = {
    "audio_info": {"duration": 2064},
    "result": {"text": "保安，把他带出去。", "utterances": [{"text": "保安，把他带出去。", "start_time": 360, "end_time": 1760, "words": []}]},
}


class ASRTest(unittest.TestCase):
    def test_submit_sends_inline_audio(self):
        t = Fake(status(20000000))
        rid = speech.asr_submit(b"\x01\x02", env=ENV, transport=t, request_id="rid-1")
        self.assertEqual(rid, "rid-1")
        req = t.requests[0]
        self.assertTrue(req.full_url.endswith("/api/v3/auc/bigmodel/submit"))
        self.assertEqual(req.get_header("X-api-resource-id"), "volc.seedasr.auc")
        self.assertEqual(req.get_header("X-api-request-id"), "rid-1")
        body = json.loads(req.data)
        self.assertEqual(base64.b64decode(body["audio"]["data"]), b"\x01\x02")
        self.assertFalse(body["request"]["enable_itn"])

    def test_submit_rejected(self):
        with self.assertRaises(speech.SpeechError) as cm:
            speech.asr_submit(b"x", env=ENV, transport=Fake(status(45000151)))
        self.assertEqual((cm.exception.kind, cm.exception.api_code, cm.exception.transient), ("api_error", 45000151, False))

    def test_poll_until_done(self):
        t = Fake(status(20000002), status(20000001), status(20000000, json.dumps(DONE).encode()))
        sleeps = []
        res = speech.poll_asr("rid", lambda rid: speech.asr_query(rid, env=ENV, transport=t), sleep=sleeps.append)
        self.assertEqual(res.polls, 3)
        self.assertEqual(res.text, "保安，把他带出去。")
        self.assertEqual(res.duration_ms, 2064)
        self.assertEqual(res.utterances, [{"text": "保安，把他带出去。", "start_ms": 360, "end_ms": 1760}])
        self.assertEqual(res.to_json()["resource_id"], "volc.seedasr.auc")
        self.assertEqual(sleeps, [1.0, 1.0, 1.0])

    def test_silent_audio(self):
        res = speech.poll_asr("rid", lambda rid: speech.asr_query(rid, env=ENV, transport=Fake(status(20000003))), sleep=lambda s: None)
        self.assertTrue(res.silent)
        self.assertEqual(res.text, "")

    def test_query_failure_code(self):
        with self.assertRaises(speech.SpeechError) as cm:
            speech.asr_query("rid", env=ENV, transport=Fake(status(55000031)))
        self.assertEqual(cm.exception.kind, "api_error")
        self.assertTrue(cm.exception.transient)

    def test_poll_timeout(self):
        now = [0.0]

        def clock():
            now[0] += 10
            return now[0]

        with self.assertRaises(speech.SpeechError) as cm:
            speech.poll_asr("rid", lambda rid: (20000001, None), sleep=lambda s: None, clock=clock, max_wait=25)
        self.assertEqual((cm.exception.kind, cm.exception.transient), ("timeout", True))

    def test_done_without_result_is_bad_response(self):
        with self.assertRaises(speech.SpeechError) as cm:
            speech.asr_query("rid", env=ENV, transport=Fake(status(20000000, b'{"audio_info":{}}')))
        self.assertEqual(cm.exception.kind, "bad_response")


if __name__ == "__main__":
    unittest.main()
