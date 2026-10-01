import hashlib
import hmac
import json
import unittest
from datetime import datetime, timedelta, timezone

from poc import volc_sign as v

# 火山文档“签名过程 Demo”（doc 6369/67270）里公开的示例 SK 与全部中间值。
# 签名只取决于 SK、日期、region、service 与请求内容，AK 只出现在 Credential= 前缀里，所以这里用占位 AK，
# 只核对文档给出的 Signature 值（文档示例 AK 的形态会被 GitHub 推送保护判为密钥，不入库）。
DOC_AK = "AKLT-doc-example-placeholder"
DOC_SK = "WkRZeE1EQmxPVGhsWWpWak5HVmtNbUUxTXpZeU9UVXlOMlE1TmpZeVlqTQ=="
DOC_QUERY = {"Action": "ListUsers", "Version": "2018-01-01", "Limit": "10", "Offset": "0"}
DOC_HEADERS = {"Host": "iam.volcengineapi.com", "X-Date": "20240619T071306Z"}
DOC_CANONICAL = (
    "GET\n/\nAction=ListUsers&Limit=10&Offset=0&Version=2018-01-01\n"
    "host:iam.volcengineapi.com\nx-date:20240619T071306Z\n\nhost;x-date\n"
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)

# 官方 Python SDK volcengine 1.0.228 的 SignerV4.sign 对同一请求生成的值（临时环境里跑，不是 poc 的依赖）
SDK_AK, SDK_SK = "AKLTexampleaccesskeyid", "exampleSecretKey+/=0123456789"
SDK_BODY = '{"Text":"雨夜里的悲伤钢琴曲，缓慢，小调，弦乐铺底","Version":"v5.0","Duration":30}'
SDK_X_DATE = "20261001T120000Z"
SDK_AUTH = (
    "HMAC-SHA256 Credential=AKLTexampleaccesskeyid/20261001/cn-beijing/imagination/request, "
    "SignedHeaders=content-type;host;x-content-sha256;x-date, "
    "Signature=ab7ece670dd0b906f05a3f8d2cc92893252cb7099d9495e7ac0c61d02a710ae2"
)

AK_SENT, SK_SENT = "AKLT-SENTINEL-ak-91d3", "sk-SENTINEL-77aa/b+c=="
ENV = {"VOLC_ACCESSKEY": AK_SENT, "VOLC_SECRETKEY": SK_SENT}
FIXED = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


class HmacPrimitiveTest(unittest.TestCase):
    def test_rfc4231_case2(self):
        # RFC 4231 Test Case 2
        digest = hmac.new(b"Jefe", b"what do ya want for nothing?", hashlib.sha256).hexdigest()
        self.assertEqual(digest, "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843")
        self.assertEqual(v.hmac_sha256(b"Jefe", "what do ya want for nothing?").hex(), digest)

    def test_sha256_of_empty(self):
        self.assertEqual(v.sha256_hex(b""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")


class DocVectorTest(unittest.TestCase):
    def test_each_step_matches_the_documented_values(self):
        canonical, signed = v.canonical_request("GET", "/", DOC_QUERY, DOC_HEADERS, b"")
        self.assertEqual(canonical, DOC_CANONICAL)
        self.assertEqual(signed, "host;x-date")
        self.assertEqual(v.sha256_hex(canonical.encode()), "5ed5bca3905e1fcbf789abb56a17c2d819674a3bcfa468ae476bd1ea80d135cb")
        scope = v.credential_scope("20240619T071306Z", "cn-beijing", "iam")
        self.assertEqual(scope, "20240619/cn-beijing/iam/request")
        self.assertEqual(
            v.string_to_sign("20240619T071306Z", scope, canonical),
            "HMAC-SHA256\n20240619T071306Z\n20240619/cn-beijing/iam/request\n5ed5bca3905e1fcbf789abb56a17c2d819674a3bcfa468ae476bd1ea80d135cb",
        )
        self.assertEqual(
            v.signing_key(DOC_SK, "20240619", "cn-beijing", "iam").hex(),
            "abee62e533a58934c49954459a3c3237d2fccea517c9a7c8a2651d8ea7779826",
        )

    def test_final_signature_matches_the_doc(self):
        auth = v.sign(
            method="GET", query=DOC_QUERY, headers=DOC_HEADERS, body=b"", access_key=DOC_AK, secret_key=DOC_SK,
            x_date="20240619T071306Z", region="cn-beijing", service="iam",
        )
        self.assertEqual(
            auth,
            f"HMAC-SHA256 Credential={DOC_AK}/20240619/cn-beijing/iam/request, SignedHeaders=host;x-date, "
            "Signature=e31c4558bcfe08a286001f59cedbf0791ffd0b2362f10e55ee2627467bcdde93",
        )


class SdkVectorTest(unittest.TestCase):
    def sign(self, **override):
        body = override.pop("body", SDK_BODY.encode("utf-8"))
        headers = override.pop("headers", None) or {
            "Host": "open.volcengineapi.com", "Content-Type": "application/json", "X-Content-Sha256": v.sha256_hex(body), "X-Date": SDK_X_DATE,
        }
        args = dict(
            method="POST", query={"Action": "GenBGMForTime", "Version": "2024-08-12"}, headers=headers, body=body,
            access_key=SDK_AK, secret_key=SDK_SK, x_date=SDK_X_DATE,
        )
        args.update(override)
        return v.sign(**args)

    def test_post_with_chinese_body_matches_official_sdk(self):
        self.assertEqual(self.sign(), SDK_AUTH)

    def test_changing_any_field_changes_the_signature(self):
        base = self.sign()
        self.assertNotEqual(base, self.sign(body=SDK_BODY.replace("30", "31").encode()))  # body（X-Content-Sha256 随之变）
        self.assertNotEqual(base, self.sign(query={"Action": "GenBGM", "Version": "2024-08-12"}))
        self.assertNotEqual(base, self.sign(x_date="20261001T120001Z"))
        self.assertNotEqual(base, self.sign(secret_key=SDK_SK + "x"))
        self.assertNotEqual(base, self.sign(region="cn-shanghai"))
        self.assertNotEqual(base, self.sign(service="iam"))
        headers = {"Host": "evil.example.com", "Content-Type": "application/json", "X-Content-Sha256": v.sha256_hex(SDK_BODY.encode()), "X-Date": SDK_X_DATE}
        self.assertNotEqual(base, self.sign(headers=headers))

    def test_body_bytes_not_json_are_hashed(self):
        # 同一 JSON 的不同序列化（空格）是不同字节，签名必须不同
        self.assertNotEqual(self.sign(body=b'{"a": 1}'), self.sign(body=b'{"a":1}'))

    def test_authorization_header_cannot_be_signed(self):
        with self.assertRaises(ValueError):
            self.sign(headers={"Host": "h", "Authorization": "x"})


class CanonicalFormTest(unittest.TestCase):
    def test_query_is_sorted_and_rfc3986_encoded(self):
        self.assertEqual(v.canonical_query({"b": "1", "a": "x y", "c": "~-_.", "d": "中"}), "a=x%20y&b=1&c=~-_.&d=%E4%B8%AD")

    def test_headers_lowercased_trimmed_sorted(self):
        ch, signed = v.canonical_headers({"X-Date": " 20260101T000000Z ", "Host": "h", "Content-Type": "application/json"})
        self.assertEqual(ch, "content-type:application/json\nhost:h\nx-date:20260101T000000Z\n")
        self.assertEqual(signed, "content-type;host;x-date")

    def test_duplicate_header_names_rejected(self):
        with self.assertRaises(ValueError):
            v.canonical_headers({"Host": "a", "host": "b"})

    def test_x_date_format_and_timezone(self):
        self.assertEqual(v.format_x_date(FIXED), "20261001T120000Z")
        self.assertEqual(v.format_x_date(FIXED.astimezone(timezone(timedelta(hours=8)))), "20261001T120000Z")
        with self.assertRaises(ValueError):
            v.format_x_date(datetime(2026, 10, 1))


class Fake:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def reply(obj, status=200):
    body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
    return v.Response(status, {}, body)


def meta(error=None):
    return {"RequestId": "r-1", "Action": "x", "Version": "2024-08-12", "Service": "imagination", "Region": "cn-beijing", "Error": error}


def submit_ok(task="2024083085138178500198"):
    return reply({"Code": 0, "Message": "success", "Result": {"TaskID": task, "PredictedWaitTime": 3}, "ResponseMetadata": meta()})


def song(status, **detail):
    result = {"TaskID": "t1", "Status": status, "Progress": 100 if status == 2 else 20, "FailureReason": None, "SongDetail": detail or {}}
    if status == 3:
        result["FailureReason"] = {"Code": 300061, "Msg": "InputLyricsPlagiarized"}
    return reply({"Code": 0, "Message": "success", "Result": result, "ResponseMetadata": meta()})


class SubmitTest(unittest.TestCase):
    def test_request_shape_and_signature(self):
        t = Fake(submit_ok())
        body = v.build_submit_body("雨夜悲伤钢琴曲", 30)
        res = v.submit_bgm(body, env=ENV, transport=t, clock=lambda: FIXED)
        self.assertEqual((res.task_id, res.predicted_wait_s), ("2024083085138178500198", 3.0))
        req = t.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "https://open.volcengineapi.com/?Action=GenBGMForTime&Version=2024-08-12")
        self.assertEqual(req.get_header("X-date"), "20261001T120000Z")
        self.assertEqual(req.get_header("Host"), "open.volcengineapi.com")
        self.assertEqual(req.get_header("X-content-sha256"), v.sha256_hex(req.data))
        self.assertEqual(json.loads(req.data), {"Text": "雨夜悲伤钢琴曲", "Version": "v5.0", "Duration": 30})
        auth = req.get_header("Authorization")
        self.assertTrue(auth.startswith(f"HMAC-SHA256 Credential={AK_SENT}/20261001/cn-beijing/imagination/request, SignedHeaders=content-type;host;x-content-sha256;x-date, Signature="))
        # 用同样的输入独立重算，签名必须一致
        expected = v.sign(
            method="POST", query={"Action": "GenBGMForTime", "Version": "2024-08-12"}, headers={
                "Host": "open.volcengineapi.com", "X-Date": "20261001T120000Z", "X-Content-Sha256": v.sha256_hex(req.data), "Content-Type": "application/json"},
            body=req.data, access_key=AK_SENT, secret_key=SK_SENT, x_date="20261001T120000Z",
        )
        self.assertEqual(auth, expected)
        self.assertNotIn(SK_SENT, auth)

    def test_body_validation(self):
        with self.assertRaises(ValueError):
            v.build_submit_body("x", 29)
        with self.assertRaises(ValueError):
            v.build_submit_body("x", 121)
        with self.assertRaises(ValueError):
            v.build_submit_body("  ", 30)
        with self.assertRaises(ValueError):
            v.build_submit_body("x", 0, segments=[("inst", 4), ("inst", 40)])  # 单段 < 5
        with self.assertRaises(ValueError):
            v.build_submit_body("x", 0, segments=[("inst", 10), ("inst", 10)])  # 总和 < 30
        b = v.build_submit_body("x", 0, segments=[("intro", 10), ("inst", 30)], aigc_watermark=True, implicit_watermark={"ProduceId": "p"})
        self.assertEqual(b["Segments"], [{"Name": "intro", "Duration": 10}, {"Name": "inst", "Duration": 30}])
        self.assertNotIn("Duration", b)
        self.assertEqual((b["AigcWatermark"], b["ImplicitWaterMark"]), (True, {"ProduceId": "p"}))

    def test_optional_fields_omitted_by_default(self):
        b = v.build_submit_body("x", 45)
        self.assertEqual(set(b), {"Text", "Version", "Duration"})

    def test_missing_credentials(self):
        with self.assertRaises(v.VolcError) as cm:
            v.submit_bgm({"Text": "x"}, env={"VOLC_ACCESSKEY": "a"}, transport=Fake())
        self.assertEqual(cm.exception.kind, "config")
        self.assertIn("VOLC_SECRETKEY", str(cm.exception))
        self.assertNotIn("VOLC_ACCESSKEY", str(cm.exception))
        self.assertFalse(cm.exception.transient)

    def test_missing_task_id_is_bad_response(self):
        t = Fake(reply({"Code": 0, "Result": {}, "ResponseMetadata": meta()}))
        with self.assertRaises(v.VolcError) as cm:
            v.submit_bgm({"Text": "x"}, env=ENV, transport=t, clock=lambda: FIXED)
        self.assertEqual(cm.exception.kind, "bad_response")


class ErrorClassificationTest(unittest.TestCase):
    def submit(self, resp):
        return v.submit_bgm({"Text": "x"}, env=ENV, transport=Fake(resp), clock=lambda: FIXED)

    def err(self, resp):
        with self.assertRaises(v.VolcError) as cm:
            self.submit(resp)
        return cm.exception

    def test_business_codes(self):
        e = self.err(reply({"Code": 200020, "Message": f"InvalidSign ak={AK_SENT} sk={SK_SENT}", "ResponseMetadata": meta()}))
        self.assertEqual((e.kind, e.api_code, e.transient), ("api_error", 200020, False))
        self.assertNotIn(AK_SENT, str(e))
        self.assertNotIn(SK_SENT, str(e))
        for code in (200023, 400040, 300067, 100001):
            e = self.err(reply({"Code": code, "Message": "busy", "ResponseMetadata": meta()}))
            self.assertTrue(e.transient, code)
        e = self.err(reply({"Code": 200022, "Message": "no quota", "ResponseMetadata": meta()}))
        self.assertFalse(e.transient)

    def test_platform_style_error_envelope(self):
        e = self.err(reply({"ResponseMetadata": meta({"CodeN": 100010, "Code": "InvalidParameter", "Message": "bad"})}))
        self.assertEqual((e.kind, e.api_code), ("api_error", 100010))
        self.assertIn("InvalidParameter", str(e))

    def test_http_errors(self):
        e = self.err(reply({"ResponseMetadata": meta({"Code": "SignatureDoesNotMatch", "Message": "x"})}, status=401))
        self.assertEqual((e.kind, e.http_status, e.transient), ("http", 401, False))
        self.assertTrue(self.err(reply(b"slow down", status=429)).transient)
        self.assertTrue(self.err(reply(b"oops", status=502)).transient)

    def test_non_json_and_network(self):
        self.assertEqual(self.err(reply(b"<html>")).kind, "bad_response")
        t = Fake(v.VolcError("network", f"URLError: refused {SK_SENT}"))
        with self.assertRaises(v.VolcError) as cm:
            v.submit_bgm({"Text": "x"}, env=ENV, transport=t, clock=lambda: FIXED)
        self.assertEqual(cm.exception.kind, "network")
        self.assertTrue(cm.exception.transient)
        self.assertNotIn(SK_SENT, str(cm.exception))


class QueryTest(unittest.TestCase):
    def test_success_parses_detail_and_scrubs_url(self):
        t = Fake(song(2, AudioUrl="https://v3-default.douyinvod.com/a.wav?sig=SECRETISH", Duration=31.5, Prompt="p"))
        st = v.query_song("t1", env=ENV, transport=t, clock=lambda: FIXED)
        self.assertEqual((st.status, st.duration_s, st.done), (2, 31.5, True))
        self.assertEqual(st.audio_url, "https://v3-default.douyinvod.com/a.wav?sig=SECRETISH")
        self.assertEqual(st.raw["SongDetail"]["AudioUrl"], "https://v3-default.douyinvod.com/a.wav")  # 留证据的副本去掉查询串
        req = t.requests[0]
        self.assertEqual(req.full_url, "https://open.volcengineapi.com/?Action=QuerySong&Version=2024-08-12")
        self.assertEqual(json.loads(req.data), {"TaskID": "t1"})

    def test_failed_status_parses_reason(self):
        st = v.query_song("t1", env=ENV, transport=Fake(song(3)), clock=lambda: FIXED)
        self.assertEqual((st.status, st.failure_code, st.failure_msg), (3, 300061, "InputLyricsPlagiarized"))

    def test_missing_status_is_bad_response(self):
        with self.assertRaises(v.VolcError):
            v.query_song("t1", env=ENV, transport=Fake(reply({"Code": 0, "Result": {}})), clock=lambda: FIXED)

    def test_poll_until_done(self):
        t = Fake(song(0), song(1), song(2, AudioUrl="https://x/a.wav", Duration=30.0))
        sleeps = []
        st = v.poll_song("t1", env=ENV, transport=t, clock=lambda: FIXED, sleep=sleeps.append, interval_s=2)
        self.assertEqual((st.status, sleeps), (2, [2, 2]))

    def test_poll_failure_and_timeout(self):
        with self.assertRaises(v.VolcError) as cm:
            v.poll_song("t1", env=ENV, transport=Fake(song(3)), clock=lambda: FIXED, sleep=lambda s: None)
        self.assertEqual((cm.exception.kind, cm.exception.api_code, cm.exception.transient), ("task_failed", 300061, False))
        ticks = iter([0.0, 10.0, 400.0])
        with self.assertRaises(v.VolcError) as cm:
            v.poll_song("t1", env=ENV, transport=Fake(song(1), song(1)), clock=lambda: FIXED, sleep=lambda s: None, timeout_s=300, monotonic=lambda: next(ticks))
        self.assertEqual(cm.exception.kind, "timeout")

    def test_failed_task_with_retryable_code(self):
        resp = reply({"Code": 0, "Result": {"TaskID": "t1", "Status": 3, "FailureReason": {"Code": 300067, "Msg": "ServerErrorSemantic, need retry"}}})
        with self.assertRaises(v.VolcError) as cm:
            v.poll_song("t1", env=ENV, transport=Fake(resp), clock=lambda: FIXED, sleep=lambda s: None)
        self.assertTrue(cm.exception.transient)


class UsageAndDownloadTest(unittest.TestCase):
    def test_usage_is_a_signed_get_without_body(self):
        t = Fake(reply({"Code": 0, "Result": {"Data": [{"MusicQuota": 10, "MusicUsed": 1}]}, "ResponseMetadata": meta()}))
        out = v.query_usage(env=ENV, transport=t, clock=lambda: FIXED)
        self.assertEqual(out["Data"][0]["MusicUsed"], 1)
        req = t.requests[0]
        self.assertEqual((req.get_method(), req.data), ("GET", None))
        self.assertEqual(req.full_url, "https://open.volcengineapi.com/?Action=QueryUsage&Version=2024-08-12")
        self.assertEqual(req.get_header("X-content-sha256"), v.sha256_hex(b""))

    def test_download_checks(self):
        self.assertEqual(v.download("https://v1-default.douyinvod.com/a.wav", transport=Fake(v.Response(200, {}, b"RIFF"))), b"RIFF")
        with self.assertRaises(v.VolcError):
            v.download("http://insecure/a.wav", transport=Fake())
        with self.assertRaises(v.VolcError):
            v.download("https://x/a.wav", transport=Fake(v.Response(403, {}, b"")))
        with self.assertRaises(v.VolcError):
            v.download("https://x/a.wav", transport=Fake(v.Response(200, {}, b"")))
        with self.assertRaises(v.VolcError):
            v.download("https://x/a.wav", transport=Fake(v.Response(200, {}, b"abcdef")), max_bytes=3)

    def test_download_sends_no_credentials(self):
        t = Fake(v.Response(200, {}, b"x"))
        v.download("https://x/a.wav", transport=t)
        self.assertIsNone(t.requests[0].get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
