import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mlfqsim import Config, make_workload, run  # noqa: E402
from mlfqsim.policies import MLFQ, make_policy  # noqa: E402


class SimTests(unittest.TestCase):
    def test_all_finish_and_conserve_work(self):
        for mode in ("global", "percpu"):
            for pol in ("static", "load", "feedback", "hybrid"):
                tasks = make_workload("mixed", 4, 1, per_cpu=4)
                r = run(tasks, Config(ncpu=4, mode=mode, policy=pol))
                self.assertEqual(r["unfinished"], 0, (mode, pol))
                self.assertLessEqual(r["util"], 1.0)

    def test_deterministic(self):
        a = run(make_workload("bursty", 2, 7, per_cpu=4), Config(ncpu=2, mode="percpu"))
        b = run(make_workload("bursty", 2, 7, per_cpu=4), Config(ncpu=2, mode="percpu"))
        self.assertEqual(a, b)

    def test_quantum_policies(self):
        m = MLFQ(4, make_policy("static", 4), 1)
        self.assertEqual([m.quantum(i) for i in range(4)], [4, 8, 16, 32])
        m = MLFQ(4, make_policy("load", 4), 1)
        quiet = m.quantum(0)

        class T:
            level = 0
        for _ in range(7):
            m.push(T())
        self.assertLess(m.quantum(0), quiet)  # 붐비면 quantum 감소

    def test_boost_moves_to_level0(self):
        m = MLFQ(3, make_policy("static", 4, 3), 1)

        class T:
            level = 2
        t = T()
        m.push(t)
        m.boost()
        self.assertEqual(t.level, 0)
        self.assertEqual(m.top_level(), 0)


if __name__ == "__main__":
    unittest.main()
