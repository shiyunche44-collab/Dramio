import unittest

from poc import audio


def frame_v2(padding=0):
    """MPEG-2 Layer III，24 kHz，64 kbps，单声道：帧长 72 × 64000 / 24000 = 192 字节，576 个采样。"""
    return bytes([0xFF, 0xF3, 0x84 | (padding << 1), 0xC4]) + bytes(192 + padding - 4)


def frame_v1():
    """MPEG-1 Layer III，44.1 kHz，128 kbps，立体声：帧长 144 × 128000 / 44100 = 417 字节，1152 个采样。"""
    return bytes([0xFF, 0xFB, 0x90, 0x44]) + bytes(417 - 4)


def xing_v2():
    f = bytearray(frame_v2())
    f[4 + 9 : 4 + 13] = b"Info"  # MPEG-2 单声道 side info 为 9 字节
    return bytes(f)


def id3(payload_len=35):
    size = payload_len
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x04\x00\x00" + syncsafe + bytes(payload_len)


def mp3_v2(n=50):
    return id3() + frame_v2() * n


class MP3Test(unittest.TestCase):
    def test_mpeg2_uses_576_samples_per_frame(self):
        info = audio.mp3_info(mp3_v2(50))
        self.assertEqual((info.version, info.sample_rate, info.samples_per_frame, info.frames), ("2", 24000, 576, 50))
        self.assertAlmostEqual(info.duration_s, 50 * 576 / 24000)  # 1.2 秒；按 1152 算会得到 2.4 秒
        self.assertEqual(info.id3_bytes, 45)
        self.assertEqual(info.bitrate_kbps_avg, 64.0)

    def test_mpeg1_uses_1152_samples_per_frame(self):
        info = audio.mp3_info(frame_v1() * 10)
        self.assertEqual((info.version, info.samples_per_frame), ("1", 1152))
        self.assertAlmostEqual(info.duration_s, 10 * 1152 / 44100)

    def test_padding_bit_changes_frame_length(self):
        info = audio.mp3_info(frame_v2(padding=1) + frame_v2())
        self.assertEqual(info.frames, 2)

    def test_xing_frame_is_not_counted(self):
        info = audio.mp3_info(xing_v2() + frame_v2() * 4)
        self.assertTrue(info.xing)
        self.assertEqual(info.frames, 4)

    def test_xing_frame_with_crc_and_vbri(self):
        f = bytearray(frame_v2())
        f[1] = 0xF2  # protection bit = 0：帧头后有 2 字节 CRC
        f[4 + 2 + 9 : 4 + 2 + 13] = b"Xing"
        self.assertEqual(audio.mp3_info(bytes(f) + frame_v2() * 3).frames, 3)
        v = bytearray(frame_v2())
        v[36:40] = b"VBRI"
        self.assertEqual(audio.mp3_info(bytes(v) + frame_v2() * 3).frames, 3)

    def test_truncated_or_garbage_raises(self):
        with self.assertRaises(audio.MP3Error):
            audio.mp3_info(frame_v2() * 3 + frame_v2()[:100])
        with self.assertRaises(audio.MP3Error):
            audio.mp3_info(frame_v2() + b"junk" + frame_v2())
        with self.assertRaises(audio.MP3Error):
            audio.mp3_info(id3())
        with self.assertRaises(audio.MP3Error):
            audio.mp3_info(frame_v1() + frame_v2())  # 版本不一致

    def test_concat_strips_tags_and_sums_duration(self):
        joined = audio.concat([mp3_v2(10), xing_v2() + frame_v2() * 5])
        self.assertFalse(joined.startswith(b"ID3"))
        self.assertAlmostEqual(audio.mp3_info(joined).duration_s, 15 * 576 / 24000)
        with self.assertRaises(audio.MP3Error):
            audio.concat([mp3_v2(2), frame_v1()])


class CERTest(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(audio.cer("abc", "abc").errors, 0)
        c = audio.cer("abcd", "axcd")
        self.assertEqual((c.substitutions, c.deletions, c.insertions), (1, 0, 0))
        c = audio.cer("abcd", "abd")
        self.assertEqual((c.substitutions, c.deletions, c.insertions), (0, 1, 0))
        c = audio.cer("abc", "abxc")
        self.assertEqual((c.substitutions, c.deletions, c.insertions), (0, 0, 1))
        self.assertAlmostEqual(audio.cer("那是我熬了三个月的方案", "那是我熬了3个月方案").rate, 2 / 11)

    def test_empty_reference(self):
        self.assertEqual(audio.cer("", "").rate, 0.0)
        self.assertEqual(audio.cer("", "啊").rate, 1.0)
        self.assertEqual(audio.cer("啊", "").rate, 1.0)

    def test_normalize(self):
        self.assertEqual(audio.normalize("你……你怎么会有这个？"), "你你怎么会有这个")
        self.assertEqual(audio.normalize("一枚 Ｕ盘，"), "一枚u盘")
        self.assertEqual(audio.normalize("U盘"), audio.normalize("u盘"))

    def test_fold_equivalents(self):
        ref, hyp = audio.normalize("把她带出去！"), audio.normalize("把他带出去。")
        self.assertEqual(audio.cer(ref, hyp).errors, 1)
        self.assertEqual(audio.cer(audio.fold_equivalents(ref), audio.fold_equivalents(hyp)).errors, 0)
        ref, hyp = audio.normalize("他说得对"), audio.normalize("他说的对")
        self.assertEqual(audio.cer(audio.fold_equivalents(ref), audio.fold_equivalents(hyp)).errors, 0)
        self.assertEqual(audio.cer(audio.fold_equivalents("粘着"), audio.fold_equivalents("藏着")).errors, 1)

    def test_digit_mismatch(self):
        self.assertTrue(audio.has_digit_mismatch("三个月", "3个月"))
        self.assertFalse(audio.has_digit_mismatch("三个月", "三个月"))
        self.assertFalse(audio.has_digit_mismatch("2 号", "2 号"))


if __name__ == "__main__":
    unittest.main()
