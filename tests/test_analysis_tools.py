"""analysis/ 도구(신뢰구간, 재현 점검, 변이 점검)의 작은 시험. 도구 자체를 돌리는 것은 각 도구의 설명대로 따로 한다."""
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "analysis"))

import bootstrap_ci as bc  # noqa: E402
import check_reproduce as cr  # noqa: E402
import mutation_check as mc  # noqa: E402


class Bootstrap(unittest.TestCase):
    def test_percentile_index(self):
        xs = list(range(10000))
        self.assertEqual(bc.percentile(xs, 0.025), 250)
        self.assertEqual(bc.percentile(xs, 0.975), 9749)

    def test_degenerate_cases(self):
        # 차이가 모든 seed에서 같으면 구간이 한 점이다. 상대 변화도 마찬가지다.
        d, rel = bc.bootstrap([10.0] * 10, [11.0] * 10, True, random.Random("x"), reps=200)
        self.assertAlmostEqual(d[0], 1.0)
        self.assertAlmostEqual(d[1], 1.0)
        self.assertAlmostEqual(rel[0], 10.0)
        self.assertAlmostEqual(rel[1], 10.0)
        d, rel = bc.bootstrap(list(range(10)), list(range(10)), True, random.Random("x"), reps=200)
        self.assertEqual(d, (0.0, 0.0))

    def test_paired_is_tighter_than_unpaired_when_seeds_move_together(self):
        a = [float(i) for i in range(10)]
        b = [x + 5 for x in a]
        dp, _ = bc.bootstrap(a, b, True, random.Random("y"), reps=500)
        du, _ = bc.bootstrap(a, b, False, random.Random("y"), reps=500)
        self.assertAlmostEqual(dp[0], 5.0)
        self.assertAlmostEqual(dp[1], 5.0)
        self.assertLess(du[0], 5.0)
        self.assertGreater(du[1], 5.0)

    def test_deterministic(self):
        a = [random.Random(1).random() for _ in range(10)]
        b = [x * 1.1 for x in a]
        r1 = bc.bootstrap(a, b, True, random.Random("c|m|default"), reps=300)
        r2 = bc.bootstrap(a, b, True, random.Random("c|m|default"), reps=300)
        self.assertEqual(r1, r2)

    def test_relative_undefined_when_baseline_can_be_zero(self):
        a = [0.0] * 5 + [1.0] * 5
        d, rel = bc.bootstrap(a, [2.0] * 10, True, random.Random("z"), reps=500)
        self.assertIsNone(rel)  # 재표본의 a 평균이 0이 될 수 있으면 상대 변화를 정의하지 않는다

    def test_t_interval(self):
        lo, hi = bc.t_interval([0.0, 2.0] * 5)  # 평균 1, 표준편차 sqrt(10/9), t(9) = 2.262
        self.assertAlmostEqual(lo, 1 - 2.262 * (10 / 9) ** 0.5 / 10 ** 0.5, places=6)
        self.assertAlmostEqual(hi, 1 + 2.262 * (10 / 9) ** 0.5 / 10 ** 0.5, places=6)
        self.assertEqual(bc.t_interval([1.0] * 10), (1.0, 1.0))
        self.assertIsNone(bc.t_interval([1.0] * 11))  # 표에 없는 자유도

    def test_saved_value_lookup(self):
        old = bc.RESULTS
        try:
            with tempfile.TemporaryDirectory() as td:
                (Path(td) / "t.csv").write_text("workload,q,lock_cost,v,w\nmixed,4,0.0,1.5,9\nmixed,16,0.25,2.5,8\n", encoding="utf-8")
                bc.RESULTS = Path(td)
                self.assertEqual(bc.saved_value(("t.csv", {"workload": "mixed", "q": 16}, None), "v"), 2.5)
                self.assertEqual(bc.saved_value(("t.csv", {"workload": "mixed", "lock_cost": 0.0}, None), "v"), 1.5)
                self.assertEqual(bc.saved_value(("t.csv", {"workload": "mixed", "q": 4}, {"v": "w"}), "v"), 9.0)
                self.assertIsNone(bc.saved_value(("t.csv", {"workload": "mixed", "q": 4}, None), "없는열"))
                self.assertIsNone(bc.saved_value(("t.csv", {"workload": "batch"}, None), "v"))
        finally:
            bc.RESULTS = old

    def test_contrast_list_is_consistent(self):
        cs = bc.contrasts()
        ids = [c.cid for c in cs]
        self.assertEqual(len(ids), len(set(ids)), "비교 이름이 겹친다")
        for c in cs:
            if c.model_effect:
                self.assertFalse(c.cfg_a.defer_requeue)
                self.assertTrue(c.cfg_b.defer_requeue)
            if c.ref_a or c.ref_b:
                self.assertTrue(c.ref_a and c.ref_b, c.cid)
            if c.cfg_a.ncpu == 1 and c.cfg_b.ncpu == 1:
                self.assertEqual(c.models, ("default",), "1코어에서는 보정 모델이 같은 결과라 따로 계산하지 않는다")

    def test_evaluate_small_contrast_both_models(self):
        c = bc.Contrast("T-01", "T", "mixed", "global", "percpu", bc.Config(ncpu=4, mode="global"),
                        bc.Config(ncpu=4, mode="percpu"), 3, ("avg_wait", "migrations"), models=bc.BOTH)
        for model in bc.BOTH:
            rows, bad = bc.evaluate(c, model)
            self.assertEqual(bad, [])
            self.assertEqual([r["metric"] for r in rows], ["avg_wait", "migrations"])
            self.assertTrue(all(r["model"] == model and r["n"] == 10 and r["csv_check"] == "n/a" for r in rows))
            for r in rows:
                self.assertEqual(r["b_gt_a"] + r["b_lt_a"] + r["b_eq_a"], 10)
                self.assertLessEqual(r["diff_lo"], r["diff"] + 1e-3)
                self.assertGreaterEqual(r["diff_hi"], r["diff"] - 1e-3)


class ReproduceCompare(unittest.TestCase):
    def test_compare(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "a").write_bytes(b"x,y\n1,2\n3,4\n")
            (d / "b").write_bytes(b"x,y\r\n1,2\r\n3,4\r\n")
            (d / "c").write_bytes(b"x,y\n1,2\n3,5\n")
            self.assertEqual(cr.compare(d / "a", d / "b"), (True, "같다"))
            ok, msg = cr.compare(d / "a", d / "b", normalize_eol=False)
            self.assertFalse(ok)
            ok, msg = cr.compare(d / "a", d / "c")
            self.assertFalse(ok)
            self.assertIn("처음 다른 줄 3", msg)
            self.assertEqual(cr.compare(d / "none", d / "a"), (False, "저장본이 없다"))
            self.assertEqual(cr.compare(d / "a", d / "none"), (False, "다시 만든 파일이 없다"))

    def test_steps_cover_all_saved_csv(self):
        saved = sorted(p.name for p in (ROOT / "results").glob("e*.csv"))
        self.assertEqual(saved, sorted(v[0] for v in cr.STEPS.values()))


class MutationList(unittest.TestCase):
    def test_every_mutant_applies_exactly_once(self):
        # 코드가 바뀌어 변이의 원문이 사라지거나 둘 이상 나오면 변이 점검이 헛돈다. 그것을 여기서 먼저 잡는다.
        ids = [m[0] for m in mc.MUTANTS]
        self.assertEqual(len(ids), len(set(ids)))
        for mid, rel, old, new, desc in mc.MUTANTS:
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertEqual(text.count(old), 1, mid)
            self.assertNotEqual(old, new, mid)

    def test_copied_tests_are_self_contained(self):
        # 변이 점검은 시뮬레이터 시험(SIM_TESTS)만 임시 사본에 복사한다. 그 시험이 analysis/나 experiments.py를 가져오면
        # 사본에서 가져오기 오류가 나서 모든 변이가 이유 없이 '잡힘'이 된다(analysis 도구 시험을 복사했을 때 실제로 그랬다).
        for name in mc.SIM_TESTS:
            src = (ROOT / "tests" / name).read_text(encoding="utf-8")
            for mod in ("bootstrap_ci", "check_reproduce", "mutation_check", "diag_f1", "experiments"):
                self.assertNotIn(f"import {mod}", src, name)
                self.assertNotIn(f"from {mod}", src, name)
        with tempfile.TemporaryDirectory() as td:
            mc.make_tree(Path(td))
            self.assertEqual(sorted(p.name for p in (Path(td) / "tests").iterdir()), sorted(mc.SIM_TESTS))
            self.assertTrue((Path(td) / "mlfqsim" / "sim.py").exists())
            self.assertFalse((Path(td) / "analysis").exists())


if __name__ == "__main__":
    unittest.main()
