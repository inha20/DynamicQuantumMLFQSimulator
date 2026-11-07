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
import mutation_check_checker as mcc  # noqa: E402
import plan_numbers as pn  # noqa: E402


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


class CheckerMutationList(unittest.TestCase):
    def test_every_mutant_applies_exactly_once_to_the_real_checker(self):
        # 점검기 소스가 바뀌어 변이의 원문이 사라지거나 둘 이상 나오면 점검기 변이 점검이 헛돈다. 그것을 여기서 먼저 잡는다.
        # 이 시험은 점검기 시험(tests/test_check_repo.py) 안에 두지 않는다. 변이를 넣은 사본에서는 원문이 바뀌어 이 시험이 항상 실패한다.
        text = (ROOT / mcc.SRC).read_text(encoding="utf-8")
        ids = [m[0] for m in mcc.MUTANTS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 50)
        for mid, old, new, desc in mcc.MUTANTS:
            self.assertEqual(text.count(old), 1, f"{mid}: {desc}")
            self.assertNotEqual(old, new, mid)

    def test_copied_tree_has_the_checker_and_its_tests_only_as_needed(self):
        src = (ROOT / "tests" / mcc.TEST_FILE).read_text(encoding="utf-8")
        for mod in ("mutation_check_checker", "plan_numbers", "mutation_check", "bootstrap_ci", "check_reproduce"):
            self.assertNotIn(f"import {mod}", src)
        with tempfile.TemporaryDirectory() as td:
            mcc.make_tree(Path(td))
            self.assertTrue((Path(td) / mcc.SRC).exists())
            self.assertTrue((Path(td) / "tests" / mcc.TEST_FILE).exists())
            self.assertFalse((Path(td) / ".git").exists())


class PlanNumbers(unittest.TestCase):
    ROW = {"model": "default", "cid": "X-1", "metric": "avg_wait", "n": "10", "paired": "1", "mean_a": "100",
           "t_lo": "-7.1531", "t_hi": "7.1531"}

    def test_seeds_needed_formula(self):
        self.assertEqual(pn.seeds_needed(10.0, 2.5), 64)      # (1.96 * 10 / 2.5)^2 = 61.47 -> 62, 보정 +2
        self.assertEqual(pn.seeds_needed(2.0, 1.0), 18)       # 3.92^2 = 15.37 -> 16, +2

    def test_sd_is_backed_out_of_the_t_interval(self):
        # 차이의 표준편차가 10이면 t 구간 반폭은 2.262 * 10 / sqrt(10) = 7.1531이다.
        rel = pn.compute([self.ROW], [("X-1", "avg_wait", "rel")])[0]
        self.assertEqual(rel["sd"], "10.00")                 # 기준 평균 100에 대한 %
        self.assertEqual((rel["n_a"], rel["n_b"], rel["n_c"]), (18, 64, 387))       # 반폭 5, 2.5, 1 (%)
        absolute = pn.compute([self.ROW], [("X-1", "avg_wait", "abs")])[0]
        self.assertEqual(absolute["sd"], "10.00")            # 틱
        self.assertEqual((absolute["n_a"], absolute["n_b"], absolute["n_c"]), (27, 99, 387))   # 반폭 4, 2, 1 (틱)

    def test_family_level_half_width(self):
        self.assertAlmostEqual(pn.family_z(1), 1.959964, places=5)
        self.assertAlmostEqual(pn.family_z(11), 2.837597, places=5)      # 1 - 0.05 / 22
        self.assertAlmostEqual(pn.family_z(90), 3.452433, places=5)      # 1 - 0.05 / 180
        zs = [pn.family_z(k) for k in (1, 4, 6, 11, 90)]
        self.assertEqual(zs, sorted(zs))                                  # 가족이 클수록 구간이 넓다
        self.assertAlmostEqual(pn.half_width(10.0, 100, 1), 1.959964, places=5)
        self.assertAlmostEqual(pn.half_width(10.0, 25, 1), 3.919928, places=5)    # seed가 4분의 1이면 반폭은 두 배
        self.assertAlmostEqual(pn.half_width(8.0, 100, 11), 2.270078, places=5)   # 2.837597 * 8 / 10
        for bad in (0, -3, 2.0):
            with self.assertRaises(ValueError):
                pn.family_z(bad)
        with self.assertRaises(ValueError):
            pn.half_width(10.0, 1, 1)

    def test_compute_reports_the_family_and_its_half_width(self):
        default = pn.compute([self.ROW], [("X-1", "avg_wait", "rel")])[0]      # k를 안 쓰면 1(보정 없음)
        self.assertEqual((default["k"], default["h100"]), (1, "1.96"))         # sd 10, seed 100개
        family = pn.compute([self.ROW], [("X-1", "avg_wait", "rel", 11)])[0]
        self.assertEqual((family["k"], family["h100"]), (11, "2.84"))
        self.assertEqual((family["n_a"], family["n_b"], family["n_c"]), (18, 64, 387))   # seed 수 열은 k와 무관
        self.assertEqual(pn.COLUMNS[-2:], ["k", "h100"])

    def test_unusable_inputs_are_errors(self):
        with self.assertRaises(ValueError):
            pn.compute([self.ROW], [("X-2", "avg_wait", "rel")])                    # 없는 비교
        with self.assertRaises(ValueError):
            pn.compute([dict(self.ROW, n="20")], [("X-1", "avg_wait", "rel")])      # seed 10개가 아님
        with self.assertRaises(ValueError):
            pn.compute([dict(self.ROW, mean_a="0")], [("X-1", "avg_wait", "rel")])  # 기준 평균 0
        with self.assertRaises(ValueError):
            pn.compute([dict(self.ROW, model="defer_requeue")], [("X-1", "avg_wait", "rel")])

    def test_saved_table_matches_a_fresh_computation(self):
        import csv
        with pn.SRC.open(encoding="utf-8", newline="") as f:
            fresh = pn.compute(list(csv.DictReader(f)))
        with pn.OUT.open(encoding="utf-8", newline="") as f:
            saved = list(csv.DictReader(f))
        self.assertEqual([{k: str(v) for k, v in r.items()} for r in fresh], saved)


if __name__ == "__main__":
    unittest.main()
