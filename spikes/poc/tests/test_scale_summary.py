import json
import tempfile
import unittest
from pathlib import Path

from poc import nodecache, nodekey, scale_summary


def write_run(d: Path, nodes, calls, shots, warnings=(), elapsed=10.0, cost=0.0):
    d.mkdir(parents=True)
    man = {"logline": "L", "nodes": nodes, "plan": {"total_s": 20.0, "shots": shots}, "warnings": list(warnings), "placeholders": [s["shot_id"] for s in shots],
           "degraded": [], "cost_cny": cost, "elapsed_s": elapsed, "hits": 0, "misses": len(nodes)}
    (d / "pipeline-manifest.json").write_text(json.dumps(man), encoding="utf-8")
    (d / "calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in calls), encoding="utf-8")


class SummaryTest(unittest.TestCase):
    def test_merges_segments_and_reconciles(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = nodecache.NodeCache(root / "cache")
            k1, k2 = nodekey.sha256_json("a"), nodekey.sha256_json("b")
            cache.put("script", k1, {"ir": b"{}"}, cost_cny=0.1, elapsed_s=50, meta={"repairs": 1})
            cache.put("keyframe", k2, {"image": b"x"}, cost_cny=0.32, elapsed_s=70)
            shots = [
                {"shot_id": "s1", "lines": [{"line_id": "l1"}], "characters": ["a", "b"]},
                {"shot_id": "s2", "lines": [], "characters": ["a"]},
            ]
            n1 = [{"stage": "script", "id": "", "key": k1, "hit": False, "cost_cny": 0.1, "elapsed_s": 50}]
            n2 = n1 + [{"stage": "keyframe", "id": "s1", "key": k2, "hit": False, "cost_cny": 0.32, "elapsed_s": 70},
                       {"stage": "compose:final", "id": "final", "key": k1, "hit": False, "cost_cny": 0.0, "elapsed_s": 3}]
            write_run(root / "r1", n1[:1], [{"cost_cny": 0.1, "status": "ok"}], shots, elapsed=60)
            write_run(root / "r2", n2, [{"cost_cny": 0.32, "status": "error", "error": "net"}, {"cost_cny": 0.32, "status": "ok"}], shots,
                      warnings=["s1：台词需要 3.79 秒，比 hint_s 3.5 秒长"], elapsed=100, cost=0.64)
            e = scale_summary.episode_summary("ep1", [root / "r1", root / "r2"], root / "cache")
            self.assertEqual(e["cost_cny_calls"], 0.74)
            self.assertEqual(e["stage"]["script"]["cost_cny"], 0.1)
            self.assertEqual(e["stage"]["keyframe"]["elapsed_s"], 70)
            self.assertEqual((e["error_calls"], e["error_calls_cost_cny"]), (1, 0.32))
            self.assertEqual(e["llm_repairs"], {"script": 1})
            self.assertEqual((e["shots"], e["lines"], e["dialogue_shots"], e["multi_char_dialogue_shots"], e["extended_shots"]), (2, 1, 1, 1, 1))
            self.assertEqual(e["elapsed_s_runs"], 160)
            t = scale_summary.totals([e, e])
            self.assertEqual((t["shots"], t["cost_cny_calls"]), (4, 1.48))
            self.assertEqual(t["cost_per_shot_cny"], 0.37)
            self.assertEqual(t["stage_cost_cny"]["keyframe"], 0.64)


if __name__ == "__main__":
    unittest.main()
