import unittest

import numpy as np

from poc import lipsync_metrics as lm

FPS = 24


def _speech_signal(n=120, windows=((2.0, 3.5),), noise=0.0, seed=1):
    """合成：语音窗口内包络和嘴部开合按同一节奏起伏，窗口外为 0。"""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / FPS
    env = np.zeros(n)
    for s, e in windows:
        m = (t >= s) & (t <= e)
        env[m] = 0.5 + 0.5 * np.abs(np.sin(2 * np.pi * 3.0 * (t[m] - s)))
    mouth = 0.02 + 0.2 * env + noise * rng.standard_normal(n)
    return env, np.maximum(mouth, 0.0)


class MetricsTest(unittest.TestCase):
    def test_synchronised_mouth_passes_all_three(self):
        env, mouth = _speech_signal(noise=0.005)
        m = lm.evaluate(mouth, env, [(2.0, 3.5)])
        self.assertTrue(m.measurable)
        self.assertTrue(m.m2_pass and m.m3_pass and m.m4_pass, m.as_dict())
        self.assertLessEqual(abs(m.onset_delta_s), 0.2)
        self.assertGreater(m.r0, m.surrogate_p95)
        self.assertEqual(m.best_lag_s, 0.0)

    def test_mouth_opening_one_second_late_fails_onset(self):
        env, mouth = _speech_signal()
        late = np.roll(mouth, FPS)  # 嘴晚 1 秒
        m = lm.evaluate(late, env, [(2.0, 3.5)])
        self.assertFalse(m.m2_pass)
        self.assertGreaterEqual(m.onset_delta_s, 0.8)

    def test_constantly_talking_mouth_fails_silence_check(self):
        env, _ = _speech_signal()
        t = np.arange(120) / FPS
        mouth = 0.05 + 0.2 * np.abs(np.sin(2 * np.pi * 3.0 * t))  # 一直在动
        m = lm.evaluate(mouth, env, [(2.0, 3.5)])
        self.assertFalse(m.m4_pass)

    def test_unrelated_rhythm_is_not_above_surrogates(self):
        env, _ = _speech_signal()
        t = np.arange(120) / FPS
        mouth = 0.02 + 0.2 * np.abs(np.sin(2 * np.pi * 1.3 * t + 0.7))
        m = lm.evaluate(mouth, env, [(2.0, 3.5)])
        self.assertFalse(m.m3_pass and m.r0 > 0.8, m.as_dict())

    def test_missing_faces_make_the_clip_unmeasurable(self):
        env, mouth = _speech_signal()
        mouth = mouth.copy()
        mouth[40:90] = np.nan
        m = lm.evaluate(mouth, env, [(2.0, 3.5)])
        self.assertFalse(m.measurable)
        self.assertIsNone(m.m2_pass)

    def test_short_gaps_are_interpolated_long_gaps_are_not(self):
        v, det = lm.fill_gaps([1.0, np.nan, np.nan, 4.0, np.nan, np.nan, np.nan, np.nan, 9.0])
        self.assertAlmostEqual(v[1], 2.0)
        self.assertTrue(np.isnan(v[5]))
        self.assertEqual(int(det.sum()), 3)

    def test_pearson_handles_constant_and_nan(self):
        self.assertTrue(np.isnan(lm.pearson([1, 1, 1, 1, 1], [1, 2, 3, 4, 5])))
        self.assertAlmostEqual(lm.pearson([1, 2, 3, 4, np.nan], [2, 4, 6, 8, 100]), 1.0)

    def test_frame_envelope_follows_amplitude(self):
        rate = 16000
        x = np.zeros(rate * 2)
        x[rate: rate + 8000] = 8000.0  # 第 1 秒起 0.5 秒
        env = lm.frame_envelope(x, rate, FPS)
        self.assertEqual(len(env), 48)
        self.assertEqual(env[:24].max(), 0.0)
        self.assertGreater(env[24:36].min(), 1000)
        self.assertEqual(env[36:].max(), 0.0)

    def test_lip_opening_normalises_by_face_width(self):
        lm106 = np.zeros((106, 2))
        lm106[0:33, 0] = np.linspace(0, 100, 33)  # 脸宽 100
        lm106[52:72, 1] = np.linspace(40, 60, 20)  # 唇高 20
        self.assertAlmostEqual(lm.lip_opening(lm106), 0.2)


if __name__ == "__main__":
    unittest.main()


class EvalTest(unittest.TestCase):
    def test_route_c_plan_tempo_and_delay(self):
        from poc import compose, lipsync_eval

        ln = compose.LinePlan("l_x", "s", "dialogue", "你好", "x.mp3", "0" * 64, 0.44, 3.6, 0.15, 0.3, 3.06, 3.82)
        plan = lipsync_eval.route_c_plan([ln], [(2.09, 4.85)])
        self.assertTrue(plan[0]["feasible"])
        self.assertAlmostEqual(plan[0]["tempo"], 1.0, places=2)  # 配音语音 2.76 秒，H3 语音 2.76 秒
        self.assertAlmostEqual(plan[0]["delay_s"], 2.09 - 0.15, places=2)  # 剪辑段内语音起点 0.15 秒
        slow = lipsync_eval.route_c_plan([ln], [(2.0, 5.5)])  # H3 说得慢：tempo ≈ 0.79
        self.assertFalse(slow[0]["feasible"])
        self.assertEqual(lipsync_eval.route_c_plan([ln, ln], [(2.0, 4.0)]), [])  # 句数不一致

    def test_cer_equiv_folds_homophones_and_punctuation(self):
        from poc import lipsync_eval

        self.assertEqual(lipsync_eval.cer_equiv("她说的方案，对吗？", "他说得方案对吗"), 0.0)
        self.assertGreater(lipsync_eval.cer_equiv("那是我熬了三个月的方案", "那是我的方案"), 0.3)

    def test_windows_from_asr_converts_ms(self):
        from poc import lipsync_eval

        self.assertEqual(lipsync_eval.windows_from_asr({"utterances": [{"start_ms": 2720, "end_ms": 4720}, {"text": "x"}]}), [(2.72, 4.72)])
