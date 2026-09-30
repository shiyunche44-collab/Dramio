import base64
import json
import struct
import unittest
from urllib.error import URLError

from poc import images, pricing, seedream

SENTINEL = "ark-SENTINEL-7f3b2c9e1d"
ENV = {"ARK_API_KEY": SENTINEL}
PRO = seedream.MODELS["pro"]
FLASH = seedream.MODELS["flash"]


def jpeg(width=1152, height=2048, payload=b"xx"):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xda" + struct.pack(">H", 2) + payload + b"\xff\xd9"


def png(width=16, height=9):
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    return images.PNG_SIG + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"


def ok_body(*imgs, generated=None, url=False):
    data = []
    for img in imgs:
        item = {"size": "1152x2048", "output_format": "jpeg"}
        if url:
            item["url"] = "https://example.invalid/x.jpeg"
        else:
            item["b64_json"] = base64.b64encode(img).decode()
        data.append(item)
    usage = {"generated_images": len(imgs) if generated is None else generated, "output_tokens": 9216, "total_tokens": 9216}
    return json.dumps({"model": PRO, "created": 1, "data": data, "usage": usage}).encode()


def err_body(code, message="boom"):
    return json.dumps({"error": {"code": code, "message": message, "type": "BadRequest"}}).encode()


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


def resp(status, body, **headers):
    return seedream.Response(status, {k.lower().replace("_", "-"): v for k, v in headers.items()}, body)


class RequestTest(unittest.TestCase):
    def test_text_to_image_body_and_headers(self):
        fake = Fake(resp(200, ok_body(jpeg()), x_request_id="rid-1"))
        res = seedream.generate("画一个人", model=PRO, size="1152x2048", env=ENV, transport=fake)
        req = fake.requests[0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, "https://ark.cn-beijing.volces.com/api/v3/images/generations")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {SENTINEL}")
        self.assertEqual(body, {
            "model": PRO, "prompt": "画一个人", "size": "1152x2048", "response_format": "b64_json",
            "output_format": "jpeg", "watermark": False,
        })
        self.assertNotIn("image", body)
        self.assertEqual(res.images[0].data, jpeg())
        self.assertEqual(res.request_id, "rid-1")
        self.assertEqual(res.usage["generated_images"], 1)

    def test_single_ref_is_string_and_multi_ref_is_list(self):
        uri = seedream.data_uri(b"abc", "JPG")
        self.assertEqual(uri, "data:image/jpeg;base64,YWJj")
        self.assertEqual(seedream.build_body(PRO, "p", size="1K", refs=[uri])["image"], uri)
        self.assertEqual(seedream.build_body(PRO, "p", size="1K", refs=[uri, uri])["image"], [uri, uri])
        with self.assertRaises(ValueError):
            seedream.build_body(PRO, "p", size="1K", refs=[uri] * 11)

    def test_missing_key(self):
        with self.assertRaises(seedream.ImageError) as ctx:
            seedream.generate("p", model=PRO, size="1K", env={}, transport=Fake())
        self.assertEqual(ctx.exception.kind, "config")
        self.assertFalse(ctx.exception.transient)


class ResponseTest(unittest.TestCase):
    def test_url_response_has_no_data(self):
        imgs, usage, _ = seedream.parse_response(ok_body(b"", url=True))
        self.assertIsNone(imgs[0].data)
        self.assertEqual(imgs[0].url, "https://example.invalid/x.jpeg")

    def test_bad_responses(self):
        for body in (b"not json", json.dumps({"data": "x"}).encode(), json.dumps({"data": [1]}).encode(),
                     json.dumps({"data": [{"size": "1x1"}]}).encode(), json.dumps({"data": [{"b64_json": "!!"}]}).encode()):
            with self.subTest(body=body), self.assertRaises(seedream.ImageError) as ctx:
                seedream.parse_response(body)
            self.assertEqual(ctx.exception.kind, "bad_response")

    def test_group_item_errors_are_skipped_and_all_failed_is_empty(self):
        body = json.dumps({"data": [{"error": {"code": "OutputImageSensitiveContentDetected", "message": "x"}}], "usage": {}}).encode()
        with self.assertRaises(seedream.ImageError) as ctx:
            seedream.parse_response(body)
        self.assertEqual(ctx.exception.kind, "empty")
        self.assertFalse(ctx.exception.billable)


class ErrorTest(unittest.TestCase):
    def call(self, *responses):
        with self.assertRaises(seedream.ImageError) as ctx:
            seedream.generate("p", model=PRO, size="1K", env=ENV, transport=Fake(*responses))
        return ctx.exception

    def test_model_not_open_is_rejected_not_billed_and_scrubbed(self):
        exc = self.call(resp(404, err_body("ModelNotOpen", f"Your account 2132580628 has not activated the model. key {SENTINEL}")))
        self.assertEqual((exc.kind, exc.http_status, exc.api_code), ("http", 404, "ModelNotOpen"))
        self.assertFalse(exc.transient)
        self.assertFalse(exc.billable)
        self.assertNotIn("2132580628", str(exc))
        self.assertNotIn(SENTINEL, str(exc))
        self.assertIn("account ***", str(exc))

    def test_moderation(self):
        for status in (400, 200):
            with self.subTest(status=status):
                exc = self.call(resp(status, err_body("InputTextSensitiveContentDetected")))
                self.assertEqual(exc.kind, "moderation")
                self.assertFalse(exc.transient)
                self.assertFalse(exc.billable)

    def test_transient_http_and_network_are_billable(self):
        for r, kind in ((resp(429, err_body("RateLimitExceeded.EndpointRPM")), "http"), (resp(500, b"oops"), "http"),
                        (resp(503, err_body("ServerOverloaded")), "http")):
            with self.subTest(status=r.status):
                exc = self.call(r)
                self.assertEqual(exc.kind, kind)
                self.assertTrue(exc.transient)
                self.assertTrue(exc.billable)

    def test_network_error_via_default_transport_shape(self):
        exc = self.call(seedream.ImageError("network", f"URLError: tunnel {SENTINEL}"))
        self.assertTrue(exc.transient and exc.billable)
        self.assertNotIn(SENTINEL, str(exc))

    def test_other_4xx_not_transient(self):
        exc = self.call(resp(400, err_body("InvalidParameter", "size")))
        self.assertFalse(exc.transient)
        self.assertFalse(exc.billable)

    def test_default_transport_wraps_urlerror(self):
        from unittest import mock

        with mock.patch("poc.doctor.urlopen", side_effect=URLError("Tunnel connection failed: 403")):
            with self.assertRaises(seedream.ImageError) as ctx:
                seedream.generate("p", model=PRO, size="1K", env=ENV)
        self.assertEqual(ctx.exception.kind, "network")


class ImageInfoTest(unittest.TestCase):
    def test_jpeg_and_png(self):
        info = images.image_info(jpeg(1152, 2048))
        self.assertEqual((info.fmt, info.width, info.height, info.pixels), ("jpeg", 1152, 2048, 1152 * 2048))
        self.assertEqual(info.bytes, len(jpeg(1152, 2048)))
        p = images.image_info(png(16, 9))
        self.assertEqual((p.fmt, p.width, p.height), ("png", 16, 9))

    def test_progressive_sof2(self):
        data = jpeg(10, 20).replace(b"\xff\xc0", b"\xff\xc2")
        self.assertEqual(images.image_info(data).width, 10)

    def test_invalid(self):
        for data in (b"GIF89a", b"\xff\xd8\xff\xd9", images.PNG_SIG + b"\x00" * 8, b"\xff\xd8\x00\x00"):
            with self.subTest(data=data), self.assertRaises(images.ImageFormatError):
                images.image_info(data)

    def test_parse_size(self):
        self.assertEqual(images.parse_size("1152x2048"), (1152, 2048))
        self.assertIsNone(images.parse_size("2K"))


class PricingTest(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(pricing.image_cny("ark", PRO, 1152 * 2048), 0.30)
        self.assertEqual(pricing.image_cny("ark", PRO, pricing.IMAGE_TIER_PIXELS), 0.30)
        self.assertEqual(pricing.image_cny("ark", PRO, pricing.IMAGE_TIER_PIXELS + 1), 0.60)
        self.assertEqual(pricing.image_cny("ark", PRO, pricing.size_pixels("2K")), 0.60)
        self.assertEqual(pricing.image_cny("ark", PRO, pricing.size_pixels("1.5K")), 0.30)
        self.assertEqual(pricing.image_cny("ark", FLASH, pricing.size_pixels("2K")), 0.12)

    def test_extra_refs_and_count(self):
        self.assertEqual(pricing.image_cny("ark", PRO, 1152 * 2048, n_refs=1), 0.30)
        self.assertEqual(pricing.image_cny("ark", PRO, 1152 * 2048, n_refs=3), 0.34)
        self.assertEqual(pricing.image_cny("ark", FLASH, 1152 * 2048, n_refs=3), 0.12)
        self.assertEqual(pricing.image_cny("ark", PRO, 1152 * 2048, n_images=0), 0.0)

    def test_bad_size(self):
        with self.assertRaises(ValueError):
            pricing.size_pixels("big")


if __name__ == "__main__":
    unittest.main()
