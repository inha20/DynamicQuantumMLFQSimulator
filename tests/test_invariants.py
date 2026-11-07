"""측정 도구(시뮬레이터) 검증 시험: 손으로 계산한 사례, 불변식, 같은 입력에서 성립해야 하는 관계.

여기 적은 기대값은 모델의 규칙(README의 비용 가정과 sim.py의 규칙)으로 **먼저 손으로 계산**한 것이다.
계산 과정은 각 시험의 주석에 틱 단위로 적었다. 모델 규칙 중 이 시험에서 쓰는 것:

- 틱 t의 처리 순서: 도착과 깨어남을 큐에 넣고, boost, (per-CPU면) 주기적 밸런싱, 그다음 CPU를 0번부터 차례로 처리한다.
- 디스패치하는 틱에 문맥 전환(task id가 그 CPU의 직전 task와 다르면 cs_cost틱)과 global 락 경합
  (디스패치마다 lock_cost*(n-1)을 CPU별로 누적해 정수 부분만큼 틱)이 있으면 그 틱부터 정지한다. 정지가 없으면 같은 틱에 바로 실행한다.
- quantum을 다 쓰면 한 레벨 내려가고(마지막 레벨이면 그대로) ready_since=t+1로 큐 뒤에 들어간다. 버스트가 끝나면 I/O 시간 뒤에 깨어난다.
- 대기 시간은 큐에 들어간 틱(ready_since)부터 디스패치 틱까지다. 응답 시간은 도착부터 첫 디스패치 틱까지다.

기본 모델(Config.defer_requeue=False)에는 멀티코어에서 같은 틱 이중 사용 결함(docs/03-measurement-validation.md의 F1)이 있다.
그 결함을 그대로 확인하는 시험은 H8, 알려진 결함을 기록하는 시험은 expectedFailure로 두었다.
"""
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mlfqsim import Config, KINDS, Task, make_workload, run  # noqa: E402
from mlfqsim.policies import MLFQ, make_policy  # noqa: E402


def T(tid, arrival, kind, bursts):
    return Task(tid, arrival, kind, list(bursts))


def io_total(t):
    return sum(t.bursts[1::2])


def interactive_stream(tid, n_bursts=20):
    """cpu 1틱, I/O 1틱을 n_bursts번 반복하는 task (H5용)."""
    b = []
    for i in range(n_bursts):
        b.append(1)
        if i < n_bursts - 1:
            b.append(1)
    return T(tid, 0, "interactive", b)


class HandComputed(unittest.TestCase):
    """손으로 계산한 작은 사례. 한 사례가 모델의 한 규칙을 확인한다."""

    def test_h1_single_task_context_switch(self):
        # 규칙: 문맥 전환 1틱. 같은 task를 이어 실행하면 전환이 없다.
        # 틱 0: 디스패치(전환 1틱 정지). 틱 1..4: 실행(quantum 4, 남은 6). 틱 4 끝에 quantum 소진 -> 레벨 1.
        # 틱 5: 같은 task라 전환 없이 이어 실행, quantum 8이라 틱 5..10에서 끝난다. 끝 = 11.
        a = T(0, 0, "batch", [10])
        r = run([a], Config(ncpu=1, policy="static", base_q=4, boost_period=None))
        self.assertEqual(a.finish, 11)
        self.assertEqual((r["makespan"], r["switches"], r["migrations"]), (11, 1, 0))
        self.assertEqual(r["avg_wait"], 0)
        self.assertAlmostEqual(r["util"], 10 / 11)
        self.assertAlmostEqual(r["overhead_frac"], 1 / 11)
        self.assertEqual(r["idle_frac"], 0)

    def test_h2_two_tasks_demotion_and_wait(self):
        # A(10틱, tid 0)와 B(3틱, tid 1)가 틱 0에 도착, quantum 4(레벨 0), 레벨 1은 8.
        # 틱 0: A 디스패치(정지), 틱 1..4 A 실행(남은 6) -> 레벨 1, ready_since=5.
        # 틱 5: B 디스패치(대기 5, 정지), 틱 6..8 B 실행 -> 끝 9.
        # 틱 9: A 디스패치(대기 9-5=4, 전환이라 정지), 틱 10..15 실행 -> 끝 16.
        a, b = T(0, 0, "batch", [10]), T(1, 0, "interactive", [3])
        r = run([a, b], Config(ncpu=1, policy="static", base_q=4, boost_period=None, starve_thresh=4))
        self.assertEqual((a.finish, b.finish), (16, 9))
        self.assertEqual((a.wait, b.wait), (4, 5))
        self.assertEqual(a.stretches, [0, 4])
        self.assertEqual(b.stretches, [5])
        self.assertEqual((r["makespan"], r["switches"], r["max_wait"]), (16, 3, 5))
        self.assertAlmostEqual(r["avg_wait"], 4.5)
        self.assertAlmostEqual(r["avg_resp"], 2.5)        # A 0, B 5
        self.assertAlmostEqual(r["int_lat_mean"], 5.0)    # interactive는 B의 대기 하나
        self.assertAlmostEqual(r["batch_slowdown"], 1.6)  # A: (16-0)/10
        self.assertAlmostEqual(r["overhead_frac"], 3 / 16)
        self.assertAlmostEqual(r["switch_per_kt"], 1000 * 3 / 16)  # 187.5
        self.assertEqual(r["int_lat_p95"], 5.0)                    # 표본이 20개 이하이면 p95는 최댓값
        self.assertEqual(r["starved"], 1)                          # 대기 최댓값이 4보다 큰 task는 B(5)뿐
        self.assertEqual(r["jain_batch"], 1.0)                     # batch가 하나뿐

    def test_h3_io_wake_idle_and_no_switch_on_resume(self):
        # [2, 5, 2]: 틱 0 디스패치(정지), 틱 1..2 실행 -> 버스트 끝, 깨어남 = 2+1+5 = 8.
        # 틱 3..7 CPU가 논다(5틱). 틱 8: 같은 task가 같은 CPU에서 재개하므로 전환이 없고 틱 8..9 실행 -> 끝 10.
        a = T(0, 0, "interactive", [2, 5, 2])
        r = run([a], Config(ncpu=1, policy="static", base_q=4, boost_period=None))
        self.assertEqual(a.finish, 10)
        self.assertEqual(a.stretches, [0, 0])
        self.assertEqual((r["makespan"], r["switches"]), (10, 1))
        self.assertAlmostEqual(r["util"], 0.4)
        self.assertAlmostEqual(r["overhead_frac"], 0.1)
        self.assertAlmostEqual(r["idle_frac"], 0.5)

    def test_h4_preemption_by_higher_level(self):
        # A(20틱, tid 0)가 틱 0에, B(2틱, tid 1, interactive)가 틱 6에 도착. quantum 4/8/16.
        # 틱 0 A 디스패치(정지), 틱 1..4 실행(남은 16) -> 레벨 1, ready_since=5.
        # 틱 5: A 재디스패치(같은 task라 전환 없음, 대기 0), 틱 5 실행(남은 15).
        # 틱 6: B 도착 -> 레벨 0 > A의 레벨 1이므로 A 선점(ready_since=6). B 디스패치(대기 0, 정지).
        # 틱 7..8: B 실행 -> 끝 9. 틱 9: A 디스패치(대기 9-6=3, 전환이라 정지).
        # 틱 10..17: quantum 8 소진(남은 7) -> 레벨 2, ready_since=18. 틱 18: 재디스패치(전환 없음) 틱 18..24 -> 끝 25.
        a, b = T(0, 0, "batch", [20]), T(1, 6, "interactive", [2])
        r = run([a, b], Config(ncpu=1, policy="static", base_q=4, boost_period=None))
        self.assertEqual((a.finish, b.finish), (25, 9))
        self.assertEqual(a.stretches, [0, 0, 3, 0])
        self.assertEqual(b.stretches, [0])
        self.assertEqual((r["makespan"], r["switches"], r["max_wait"]), (25, 3, 3))
        self.assertAlmostEqual(r["avg_wait"], 1.5)
        self.assertAlmostEqual(r["overhead_frac"], 3 / 25)
        self.assertEqual(r["idle_frac"], 0)

    def _h5(self, boost):
        # 레벨 2개, quantum 2/4, 문맥 전환 비용 0. I1, I2는 1틱 실행 + 1틱 I/O를 20번 반복해 CPU를 계속 채우고,
        # L(batch 6틱)은 틱 2..3에 한 번 돌고 레벨 1로 내려간다.
        cfg = Config(ncpu=1, policy="static", base_q=2, levels=2, boost_period=boost, cs_cost=0)
        i1, i2 = interactive_stream(0), interactive_stream(1)
        lt = T(2, 0, "batch", [6])
        run([i1, i2, lt], cfg)
        return lt

    def test_h5_boost_relieves_starvation(self):
        # boost 없음: 틱 4부터 I1, I2가 번갈아 틱 40, 41까지 CPU를 채운다(각각 20번째 버스트).
        #   L은 틱 42에 디스패치(대기 42-4=38), quantum 4로 틱 42..45에 끝낸다 -> 끝 46, 대기 2+38=40.
        # boost 10: 틱 10에 L이 레벨 0 꼬리로 올라간다. 틱 11..12에 2틱, 틱 20 boost 뒤 틱 21..22에 2틱.
        #   L의 디스패치 대기: 2(틱 2), 11-4=7(틱 11), 21-13=8(틱 21). boost는 ready_since를 바꾸지 않는다.
        #   끝 = 틱 22 + 1 = 23, 대기 17.
        lt = self._h5(None)
        self.assertEqual((lt.finish, lt.wait, lt.stretches), (46, 40, [2, 38]))
        lt = self._h5(10)
        self.assertEqual((lt.finish, lt.wait, lt.max_wait, lt.stretches), (23, 17, 8, [2, 7, 8]))

    def test_h6_global_two_cpus_lock_cost_carry(self):
        # global, CPU 2개, lock_cost 0.5이면 디스패치마다 0.5틱이 CPU별로 누적되어 두 번째 디스패치에서 1틱이 된다.
        # A, B, C가 모두 3틱, 틱 0 도착. 틱 0: CPU0이 A, CPU1이 B를 디스패치(전환 1틱씩, 누적 0.5). 틱 1..3 실행 -> 끝 4.
        # 틱 4: CPU0이 C를 디스패치: 전환 1 + 누적 1.0 -> 정수 1 = 정지 2틱(틱 4, 5). CPU1은 큐가 비어 논다.
        # 틱 6..8 C 실행 -> 끝 9. 대기: A 0, B 0, C 4.
        ts = [T(i, 0, "batch", [3]) for i in range(3)]
        r = run(ts, Config(ncpu=2, mode="global", policy="static", base_q=4, boost_period=None,
                           cs_cost=1, lock_cost=0.5, mig_penalty=3))
        self.assertEqual([t.finish for t in ts], [4, 4, 9])
        self.assertEqual([t.wait for t in ts], [0, 0, 4])
        self.assertEqual((r["makespan"], r["switches"], r["migrations"]), (9, 3, 0))
        self.assertAlmostEqual(r["avg_wait"], 4 / 3)
        self.assertAlmostEqual(r["util"], 9 / 18)
        self.assertAlmostEqual(r["overhead_frac"], 4 / 18)  # CPU0: 틱 0, 4, 5 / CPU1: 틱 0
        self.assertAlmostEqual(r["idle_frac"], 5 / 18)      # CPU1: 틱 4..8
        self.assertAlmostEqual(r["imbalance"], 0.5)         # 실행한 틱 CPU0 6, CPU1 3 -> (6-3)/6

    def _h7(self, steal):
        # per-CPU, strict, 밸런싱 없음. 틱 0에 T0..T3이 최소 부하 CPU로 놓인다: CPU0 [T0, T2], CPU1 [T1, T3].
        ts = [T(0, 0, "batch", [6]), T(1, 0, "batch", [2]), T(2, 0, "batch", [2]), T(3, 0, "batch", [2])]
        r = run(ts, Config(ncpu=2, mode="percpu", policy="static", base_q=8, boost_period=None,
                           affinity="strict", lb_period=None, idle_steal=steal))
        return ts, r

    def test_h7_percpu_idle_steal(self):
        # 틱 0: T0(CPU0), T1(CPU1) 디스패치(정지). CPU1: T1 틱 1..2 -> 끝 3. 틱 3: T3(대기 3, 정지), 틱 4..5 -> 끝 6.
        # CPU0: T0 틱 1..6 -> 끝 7.
        # 훔치기 없음: 틱 7에 CPU0이 T2(대기 7, 정지) 디스패치, 틱 8..9 -> 끝 10. 대기 합 0+0+3+7 = 10.
        # 훔치기 있음: 틱 6에 CPU1이 놀다가 CPU0 큐의 T2를 가져와(대기 6, 정지) 틱 7..8 -> 끝 9. 대기 합 9.
        #   T2는 이전에 실행한 적이 없어 이주로 세지 않는다.
        ts, r = self._h7(False)
        self.assertEqual([t.finish for t in ts], [7, 3, 10, 6])
        self.assertEqual((r["makespan"], r["switches"], r["migrations"]), (10, 4, 0))
        self.assertAlmostEqual(r["avg_wait"], 2.5)
        # 실행한 틱: CPU0 = 6+2 = 8, CPU1 = 2+2 = 4 -> (8-4)/8. batch 지연 배수 s = 끝/작업 = [7/6, 3/2, 5, 3].
        self.assertAlmostEqual(r["imbalance"], 0.5)
        self.assertAlmostEqual(r["batch_slowdown"], (7 / 6 + 3 / 2 + 5 + 3) / 4)
        self.assertAlmostEqual(r["jain_batch"], 1024 / 1354)  # (합 s)^2 / (4 * 합 s^2) = (32/3)^2 / (4 * 1354/36)
        ts, r = self._h7(True)
        self.assertEqual([t.finish for t in ts], [7, 3, 9, 6])
        self.assertEqual((r["makespan"], r["switches"], r["migrations"]), (9, 4, 0))
        self.assertAlmostEqual(r["avg_wait"], 2.25)
        self.assertEqual(ts[2].wait, 6)
        self.assertAlmostEqual(r["imbalance"], 0.0)           # CPU0 6틱, CPU1 2+2+2 = 6틱
        self.assertAlmostEqual(r["jain_batch"], 3721 / 4732)  # s = [7/6, 3/2, 9/2, 3]

    def _h9(self, affinity):
        # per-CPU, 문맥 전환 비용 0, 이주 페널티 3, 훔치기/밸런싱 없음.
        # 틱 0 배치: X -> CPU0, Y -> CPU1, Z -> CPU0 (최소 부하, 동률이면 낮은 번호).
        # 틱 0: X가 1틱 실행 후 I/O 1틱, 깨어남 = 2. Y는 틱 0에 끝(끝 1). 틱 1: Z가 CPU0에서 시작(대기 1).
        x, y, z = T(0, 0, "interactive", [1, 1, 1]), T(1, 0, "batch", [1]), T(2, 0, "batch", [20])
        r = run([x, y, z], Config(ncpu=2, mode="percpu", policy="static", base_q=8, boost_period=None,
                                  cs_cost=0, mig_penalty=3, affinity=affinity, lb_period=None,
                                  idle_steal=False))
        return x, y, z, r

    def test_h9_strict_vs_none_placement_after_wake(self):
        # 틱 2에 X가 깨어날 때 CPU0은 Z가 실행 중(부하 1), CPU1은 비어 있다(부하 0).
        # strict: X는 마지막 CPU(0)로 돌아가 Z의 quantum(틱 1..8)이 끝난 틱 9에 실행한다. 대기 9-2=7, 끝 10, 이주 0.
        #   Z는 틱 9에 X에 밀려(X는 레벨 0, Z는 레벨 1) 틱 10..21에 끝낸다 -> 끝 22.
        # none: X는 부하가 가장 낮은 CPU1로 가서 틱 2에 바로 실행한다. 이주 1회(+3틱 작업), 남은 1+3 = 4,
        #   틱 2..5 실행 -> 끝 6, 대기 0. Z는 방해받지 않고 틱 9..20에 끝낸다 -> 끝 21.
        # 작업량 합 23(X 2 + Y 1 + Z 20). 유효 이용률은 이주 페널티로 늘어난 작업량을 세지 않지만, 실제로 실행한 틱에는 들어간다.
        x, y, z, r = self._h9("strict")
        self.assertEqual((x.finish, x.wait, r["migrations"], z.finish, r["makespan"]), (10, 7, 0, 22, 22))
        self.assertEqual(r["switches"], 5)  # X, Y, Z, X(틱 9), Z(틱 10)
        self.assertAlmostEqual(r["util"], 23 / 44)
        self.assertAlmostEqual(r["idle_frac"], 21 / 44)  # 실행한 틱 23, 정지 0
        x, y, z, r = self._h9("none")
        self.assertEqual((x.finish, x.wait, r["migrations"], z.finish, r["makespan"]), (6, 0, 1, 21, 21))
        self.assertEqual(r["switches"], 4)  # X, Y, Z, X(CPU1). Z는 같은 CPU에서 이어 실행해 전환이 없다.
        self.assertAlmostEqual(r["util"], 23 / 42)
        self.assertAlmostEqual(r["idle_frac"], 16 / 42)  # 실행한 틱 23 + 이주 페널티 3 = 26

    def test_h11_soft_affinity_slack(self):
        # H9와 같은 배치에서 틱 2의 부하 차이는 loads[0] - loads[1] = 1 - 0 = 1.
        # soft는 차이가 soft_slack보다 작으면 마지막 CPU에 남고, 같거나 크면 최소 부하 CPU로 간다.
        # soft_slack 2: 1 < 2라 strict와 같은 결과(X 끝 10, 이주 0). soft_slack 1: 1 < 1이 아니라 none과 같은 결과(X 끝 6, 이주 1).
        for slack, want in ((2, (10, 7, 0)), (1, (6, 0, 1))):
            x = T(0, 0, "interactive", [1, 1, 1])
            y, z = T(1, 0, "batch", [1]), T(2, 0, "batch", [20])
            r = run([x, y, z], Config(ncpu=2, mode="percpu", policy="static", base_q=8, boost_period=None,
                                      cs_cost=0, mig_penalty=3, affinity="soft", soft_slack=slack,
                                      lb_period=None, idle_steal=False))
            self.assertEqual((x.finish, x.wait, r["migrations"]), want, slack)

    def test_h12_periodic_balance(self):
        # per-CPU, strict, 훔치기 없음, 문맥 전환 비용 0, quantum 16. T0=10, T1=1, T2=10, T3=1 (모두 틱 0).
        # 배치: CPU0 [T0, T2], CPU1 [T1, T3]. CPU1은 틱 0..1에 일을 마치고 논다(T1 끝 1, T3 끝 2, T3 대기 1).
        # 밸런싱 없음: T0 틱 0..9(끝 10), CPU0이 틱 10에 T2를 시작(대기 10), 틱 10..19 -> 끝 20. 대기 합 0+0+1+10.
        # 주기 5: 틱 5에 부하 [2, 0] (차이 2, 이상이면 이동)이라 CPU0 큐의 T2를 CPU1로 옮긴다.
        #   CPU1이 틱 5에 T2를 시작(대기 5, 처음 실행이라 이주 아님) 틱 5..14 -> 끝 15. 이어서 부하 [1, 1]이라 멈춘다. 대기 합 0+0+1+5.
        def go(lb):
            ts = [T(0, 0, "batch", [10]), T(1, 0, "batch", [1]), T(2, 0, "batch", [10]), T(3, 0, "batch", [1])]
            r = run(ts, Config(ncpu=2, mode="percpu", policy="static", base_q=16, boost_period=None,
                               cs_cost=0, affinity="strict", lb_period=lb, idle_steal=False))
            return ts, r
        ts, r = go(None)
        self.assertEqual([t.finish for t in ts], [10, 1, 20, 2])
        self.assertEqual((r["makespan"], r["migrations"]), (20, 0))
        self.assertAlmostEqual(r["avg_wait"], 11 / 4)
        ts, r = go(5)
        self.assertEqual([t.finish for t in ts], [10, 1, 15, 2])
        self.assertEqual((r["makespan"], r["migrations"]), (15, 0))
        self.assertAlmostEqual(r["avg_wait"], 6 / 4)
        self.assertEqual(ts[2].wait, 5)

    def test_h13_steal_victim_and_tail(self):
        # CPU 3개, strict, idle 훔치기, quantum 16, 문맥 전환 비용 0. T0=10, T1=10, T2=1, T3=10, T4=10, T5=1, T6=3.
        # 배치: CPU0 [T0, T3, T6], CPU1 [T1, T4], CPU2 [T2, T5]. CPU2는 틱 0..1에 T2, T5를 끝낸다(T5 대기 1).
        # 틱 2: CPU2가 논다. 큐가 가장 긴 CPU는 CPU0(대기 2개)이고, 그 큐의 꼬리인 T6을 가져와(대기 2) 틱 2..4 -> 끝 5.
        # 틱 5: CPU0 큐 [T3]과 CPU1 큐 [T4]의 길이가 같으면 낮은 번호(CPU0)에서 T3을 가져와(대기 5) 틱 5..14 -> 끝 15.
        # 틱 10: T0, T1이 끝난다(끝 10). CPU0이 CPU1 큐의 T4를 가져와(대기 10) 틱 10..19 -> 끝 20.
        ts = [T(0, 0, "batch", [10]), T(1, 0, "batch", [10]), T(2, 0, "batch", [1]), T(3, 0, "batch", [10]),
              T(4, 0, "batch", [10]), T(5, 0, "batch", [1]), T(6, 0, "batch", [3])]
        r = run(ts, Config(ncpu=3, mode="percpu", policy="static", base_q=16, boost_period=None, cs_cost=0,
                           affinity="strict", lb_period=None, idle_steal=True))
        self.assertEqual([t.finish for t in ts], [10, 10, 1, 15, 20, 2, 5])
        self.assertEqual([t.wait for t in ts], [0, 0, 0, 5, 10, 1, 2])
        self.assertEqual((r["makespan"], r["switches"], r["migrations"]), (20, 7, 0))

    def test_h14_boost_resets_level_of_running_task(self):
        # 레벨 3개, quantum 2/4/8, 문맥 전환 비용 0, boost 10. A(30틱)는 틱 0에, B(1틱)는 틱 11에 도착.
        # A: 틱 0..1(레벨 0) -> 틱 2..5(레벨 1) -> 틱 6부터 레벨 2의 quantum 8을 받는다. 틱 10 boost 때 실행 중인 A의 레벨이 0이 된다.
        # 틱 11에 B(레벨 0)가 도착해도 같은 레벨이라 A를 선점하지 못하고, A의 quantum이 끝나는 틱 13 뒤인 틱 14에 실행한다.
        #   B 대기 14-11 = 3, 끝 15. (레벨이 안 내려갔다면 B가 틱 11에 A를 선점해 끝 12.)
        # A는 틱 13에 레벨 1로 내려가고(2가 아님), 틱 15..18, 틱 19..26(틱 20 boost로 다시 레벨 0), 틱 27..30으로 끝난다 -> 끝 31.
        a, b = T(0, 0, "batch", [30]), T(1, 11, "interactive", [1])
        r = run([a, b], Config(ncpu=1, policy="static", base_q=2, levels=3, boost_period=10, cs_cost=0))
        self.assertEqual((b.wait, b.finish, a.finish), (3, 15, 31))
        self.assertEqual((r["makespan"], r["switches"]), (31, 3))

    def test_h8_same_tick_handoff_reproducer(self):
        # 결함 F1의 최소 재현. 일 1개(10틱), CPU 2개, quantum 4(레벨 1은 8), lock_cost 0.
        # 일이 하나이므로 CPU가 몇 개든 끝나는 시각은 CPU 1개와 같아야 한다(틱 0 정지 + 10틱 = 11).
        # 기본 모델: 틱 4에 CPU0에서 quantum이 끝나 큐에 들어가는데(ready_since=5), 같은 틱 4에 뒤 순서인 CPU1이
        #   그 task를 가져간다(대기 4-5=-1, 문맥 전환 1틱 정지, 이주 +3틱). 틱 5..12에 CPU1에서 8틱 실행(남은 1),
        #   틱 13에 CPU0이 다시 가져가 이주 +3, 남은 4, 틱 13..16 -> 끝 17.
        #   대기 기록 [0, -1, 0], 전환 2, 이주 2.
        # defer_requeue=True: 큐에 넣는 것을 틱 끝으로 미룬다. 틱 5에 CPU0이 이어 실행(전환 없음, 이주 없음)해 틱 5..10 -> 끝 11.
        for mode in ("global", "percpu"):
            base = Config(ncpu=2, mode=mode, policy="static", base_q=4, boost_period=None, lock_cost=0.0)
            a = T(0, 0, "batch", [10])
            r = run([a], base)
            self.assertEqual((a.finish, r["switches"], r["migrations"], a.stretches),
                             (17, 2, 2, [0, -1, 0]), mode)
            self.assertEqual(r["avg_wait"], -1.0, mode)
            a = T(0, 0, "batch", [10])
            r = run([a], replace(base, defer_requeue=True))
            self.assertEqual((a.finish, r["switches"], r["migrations"], a.stretches),
                             (11, 1, 0, [0, 0]), mode)
            a = T(0, 0, "batch", [10])
            r1 = run([a], replace(base, ncpu=1))
            self.assertEqual(a.finish, 11)
        # 문맥 전환 비용이 0이면 기본 모델은 같은 틱에 한 task를 두 CPU가 실행하기도 한다: 끝 15, 정상은 10.
        for flag, finish in ((False, 15), (True, 10)):
            a = T(0, 0, "batch", [10])
            run([a], Config(ncpu=2, mode="global", policy="static", base_q=4, boost_period=None,
                            lock_cost=0.0, cs_cost=0, defer_requeue=flag))
            self.assertEqual(a.finish, finish, flag)


    def test_h10_global_lock_cost_is_charged_without_contention(self):
        # 모델 가정(결함이 아님): global 큐는 실제로 경합하는 CPU가 몇 개인지와 상관없이 디스패치마다
        # lock_cost*(CPU 수-1)틱을 CPU별로 누적해 부과한다. 일 하나(10틱), quantum 4/8, lock_cost 1.0, 보정 모델.
        # CPU 2개: 디스패치마다 1틱. 틱 0..1 정지(전환 1 + 락 1), 틱 2..5 실행(남은 6) -> 레벨 1.
        #   틱 6: 재디스패치(전환 없음, 락 1틱 정지), 틱 7..12 실행 -> 끝 13. 정지 3틱(틱 0, 1, 6).
        # CPU 4개: 디스패치마다 3틱. 틱 0..3 정지(전환 1 + 락 3), 틱 4..7 실행, 틱 8..10 락 정지, 틱 11..16 실행 -> 끝 17.
        # CPU 1개는 락 비용이 없어 11.
        want = {1: 11, 2: 13, 4: 17}
        for n, finish in want.items():
            a = T(0, 0, "batch", [10])
            r = run([a], Config(ncpu=n, mode="global", policy="static", base_q=4, boost_period=None,
                                lock_cost=1.0, defer_requeue=True))
            self.assertEqual(a.finish, finish, n)
            self.assertEqual(r["switches"], 1, n)
            self.assertAlmostEqual(r["overhead_frac"] * n * r["makespan"], {1: 1, 2: 3, 4: 7}[n], msg=n)


class MLFQStructure(unittest.TestCase):
    """다단계 큐 자료구조가 설명한 순서 규칙을 지키는지."""

    class _T:
        def __init__(self, name, level):
            self.name, self.level = name, level

    def _filled(self):
        m = MLFQ(3, make_policy("static", 4, 3), 1)
        for name, lv in (("A", 0), ("B", 2), ("C", 2), ("D", 1)):
            m.push(self._T(name, lv))
        return m

    def test_pop_order_is_priority_then_fifo(self):
        m = self._filled()
        self.assertEqual([m.pop().name for _ in range(4)], ["A", "D", "B", "C"])
        self.assertIsNone(m.pop())
        self.assertEqual(len(m), 0)

    def test_front_push(self):
        m = MLFQ(2, make_policy("static", 4, 2), 1)
        m.push(self._T("A", 1))
        m.push(self._T("B", 1), front=True)
        self.assertEqual([m.pop().name, m.pop().name], ["B", "A"])

    def test_steal_takes_tail_of_lowest_priority_queue(self):
        m = self._filled()
        self.assertEqual([m.steal().name for _ in range(4)], ["C", "B", "D", "A"])
        self.assertIsNone(m.steal())

    def test_boost_keeps_order_and_counts(self):
        m = self._filled()
        m.boost()
        self.assertEqual(len(m), 4)
        self.assertEqual([m.pop().name for _ in range(4)], ["A", "D", "B", "C"])

    def test_top_level(self):
        m = MLFQ(3, make_policy("static", 4, 3), 1)
        self.assertEqual(m.top_level(), 3)  # 비어 있으면 레벨 수
        m.push(self._T("X", 2))
        self.assertEqual(m.top_level(), 2)


class MetricDefinitions(unittest.TestCase):
    def test_p95_is_index_floor_of_0_95n(self):
        from mlfqsim.sim import _p95
        self.assertEqual(_p95([]), 0.0)
        self.assertEqual(_p95([7]), 7.0)
        self.assertEqual(_p95(list(range(1, 21))), 20.0)    # n=20: 인덱스 19 = 최댓값
        self.assertEqual(_p95(list(range(1, 22))), 20.0)    # n=21: 인덱스 19 = 끝에서 두 번째
        self.assertEqual(_p95(list(range(100, 0, -1))), 96.0)  # n=100: 정렬한 뒤 인덱스 95


class QuantumTables(unittest.TestCase):
    """정책이 정하는 quantum을 손으로 계산한 표와 맞춘다."""

    class _T:
        level = 0

    def _queue_with(self, policy, waiting):
        m = MLFQ(4, make_policy(policy, 4), 1)
        for _ in range(waiting):
            m.push(self._T())
        return m

    def test_static(self):
        m = MLFQ(4, make_policy("static", 4), 1)
        self.assertEqual([m.quantum(i) for i in range(4)], [4, 8, 16, 32])

    def test_load_scale_table(self):
        # scale = clamp(2/(1+p), 0.25, 2), p = 대기 task 수(CPU 1개). quantum = max(1, round(scale * base)).
        # 레벨 0(base 4): p=0 -> 8, 1 -> 4, 3 -> 2, 7 -> 1, 15 -> scale 0.125가 0.25로 잘려 1.
        # 레벨 1(base 8): p=0 -> 16, 1 -> 8, 3 -> 4.
        want0 = {0: 8, 1: 4, 3: 2, 7: 1, 15: 1}
        for p, q in want0.items():
            self.assertEqual(self._queue_with("load", p).quantum(0), q, p)
        for p, q in {0: 16, 1: 8, 3: 4}.items():
            self.assertEqual(self._queue_with("load", p).quantum(1), q, p)

    def test_load_uses_cpus_served(self):
        # global 큐는 CPU 수로 나눈다: 대기 7개, CPU 4개 -> p=1.75, scale 2/2.75 = 0.727, round(0.727*4) = 3.
        m = MLFQ(4, make_policy("load", 4), 4)
        for _ in range(7):
            m.push(self._T())
        self.assertEqual(m.quantum(0), 3)

    def test_feedback_after_expiries(self):
        # ema_k = 1 - 0.5 * 0.9^k. ema > 0.75는 k=7부터이고, 그때부터 관찰마다 scale이 1.05배 된다.
        # k=6: scale 1 -> 4. k=9: 1.05^3 = 1.157625 -> round(4.6305) = 5.
        m = MLFQ(4, make_policy("feedback", 4), 1)
        for _ in range(6):
            m.policy.observe(0, True)
        self.assertEqual(m.quantum(0), 4)
        for _ in range(3):
            m.policy.observe(0, True)
        self.assertAlmostEqual(m.policy.s[0], 1.05 ** 3)
        self.assertEqual(m.quantum(0), 5)

    def test_feedback_after_early_yields(self):
        # ema_k = 0.5 * 0.9^k. ema < 0.35는 k=4부터(0.328)이고 그때부터 scale이 0.95배. k=5: round(4*0.9025) = 4, k=6: round(4*0.857) = 3.
        m = MLFQ(4, make_policy("feedback", 4), 1)
        for _ in range(5):
            m.policy.observe(0, False)
        self.assertAlmostEqual(m.policy.s[0], 0.95 ** 2)
        self.assertEqual(m.quantum(0), 4)
        m.policy.observe(0, False)
        self.assertEqual(m.quantum(0), 3)

    def test_feedback_scale_bounds(self):
        m = MLFQ(4, make_policy("feedback", 4), 1)
        for _ in range(500):
            m.policy.observe(0, True)
        self.assertAlmostEqual(m.policy.s[0], 4.0)   # hi
        for _ in range(500):
            m.policy.observe(1, False)
        self.assertAlmostEqual(m.policy.s[1], 0.5)   # lo

    def test_hybrid_is_product(self):
        m = MLFQ(4, make_policy("hybrid", 4), 1)
        for _ in range(3):
            m.push(self._T())  # p=3 -> load scale 0.5
        m.policy.s[0] = 1.5
        self.assertAlmostEqual(m.policy.scale(0, m), 0.75)
        self.assertEqual(m.quantum(0), 3)


def _grid(kinds=("mixed", "bursty", "interactive"), ncpus=(1, 2, 4), policies=("static", "load", "feedback", "hybrid"),
          seeds=(0, 1), per_cpu=4):
    for kind in kinds:
        for n in ncpus:
            for mode in (("global",) if n == 1 else ("global", "percpu")):
                for pol in policies:
                    for s in seeds:
                        yield kind, n, mode, pol, s, per_cpu


class Invariants(unittest.TestCase):
    """모든 구성에서 성립해야 하는 관계."""

    def test_work_conservation_and_time_accounting(self):
        # (CPU 용량) = (원래 작업량) + (이주 횟수 x 페널티) + (정지 틱) + (노는 틱). 기본 모델과 보정 모델 모두.
        # 정지/노는 틱과 작업량은 서로 다른 곳에서 세므로 어느 한 틱이 빠지거나 두 번 세어지면 어긋난다.
        for flag in (False, True):
            for kind, n, mode, pol, s, pc in _grid(kinds=KINDS, ncpus=(1, 2, 4, 8), seeds=(0,), per_cpu=3):
                cfg = Config(ncpu=n, mode=mode, policy=pol, defer_requeue=flag)
                tasks = make_workload(kind, n, s, per_cpu=pc)
                demand = sum(t.cpu_total for t in tasks)
                r = run(tasks, cfg)
                cap = n * r["makespan"]
                lhs = demand + r["migrations"] * cfg.mig_penalty + (r["overhead_frac"] + r["idle_frac"]) * cap
                self.assertAlmostEqual(lhs, cap, places=6, msg=(flag, kind, n, mode, pol))
                self.assertEqual(r["unfinished"], 0)

    def test_bounds(self):
        for flag in (False, True):
            for kind, n, mode, pol, s, pc in _grid(seeds=(0,)):
                cfg = Config(ncpu=n, mode=mode, policy=pol, defer_requeue=flag)
                tasks = make_workload(kind, n, s, per_cpu=pc)
                r = run(tasks, cfg)
                msg = (flag, kind, n, mode, pol)
                self.assertGreater(r["util"], 0, msg)
                self.assertLessEqual(r["util"], 1.0, msg)
                self.assertGreaterEqual(r["overhead_frac"], 0, msg)
                self.assertGreaterEqual(r["idle_frac"], 0, msg)
                self.assertLessEqual(r["overhead_frac"] + r["idle_frac"] + r["util"], 1.0 + 1e-9, msg)
                self.assertGreater(r["jain_batch"], 0, msg)
                self.assertLessEqual(r["jain_batch"], 1.0 + 1e-9, msg)
                self.assertGreaterEqual(r["batch_slowdown"], 1.0 - 1e-9, msg)
                self.assertGreaterEqual(r["imbalance"], 0, msg)
                self.assertLessEqual(r["imbalance"], 1.0, msg)
                # 종료 시각의 하한: 어떤 task도 자기 작업과 I/O 시간보다 빨리 끝날 수 없고, 전체 작업은 CPU 수로 나눈 것보다 빨리 끝날 수 없다.
                for t in tasks:
                    self.assertGreaterEqual(t.finish - t.arrival, t.cpu_total + io_total(t), msg)
                self.assertGreaterEqual(r["makespan"], sum(t.cpu_total for t in tasks) / n, msg)

    def test_per_task_time_identity_without_costs(self):
        # 비용이 모두 0이면 task마다 (끝 - 도착) = (대기 합) + (CPU 작업) + (I/O)가 정확히 성립한다.
        # 대기를 실행 중에 세거나 두 번 세면 어긋난다. 멀티코어는 보정 모델에서, 1코어는 기본 모델에서 확인한다.
        for flag, ncpus in ((True, (1, 2, 4)), (False, (1,))):
            for kind, n, mode, pol, s, pc in _grid(ncpus=ncpus):
                cfg = Config(ncpu=n, mode=mode, policy=pol, cs_cost=0, lock_cost=0.0, mig_penalty=0, defer_requeue=flag)
                tasks = make_workload(kind, n, s, per_cpu=pc)
                run(tasks, cfg)
                for t in tasks:
                    self.assertEqual(t.finish - t.arrival, t.wait + t.cpu_total + io_total(t),
                                     (flag, kind, n, mode, pol, t.tid))

    def test_per_task_time_lower_bound_with_costs(self):
        # 비용이 있으면 정지 틱과 이주 페널티만큼 더 걸린다: (끝 - 도착) >= (대기 합) + (CPU 작업) + (I/O).
        for kind, n, mode, pol, s, pc in _grid(ncpus=(1, 4), seeds=(0,)):
            tasks = make_workload(kind, n, s, per_cpu=pc)
            run(tasks, Config(ncpu=n, mode=mode, policy=pol, defer_requeue=True))
            for t in tasks:
                self.assertGreaterEqual(t.finish - t.arrival, t.wait + t.cpu_total + io_total(t))

    def test_waits_nonnegative_single_cpu(self):
        for kind, n, mode, pol, s, pc in _grid(kinds=KINDS, ncpus=(1,), seeds=(0, 1, 2)):
            tasks = make_workload(kind, n, s, per_cpu=pc)
            run(tasks, Config(ncpu=1, mode=mode, policy=pol))
            for t in tasks:
                self.assertTrue(all(x >= 0 for x in t.stretches), (kind, pol, t.tid))

    def test_waits_nonnegative_multicore_with_defer_requeue(self):
        for kind, n, mode, pol, s, pc in _grid(kinds=KINDS, ncpus=(2, 4), seeds=(0, 1)):
            tasks = make_workload(kind, n, s, per_cpu=pc)
            run(tasks, Config(ncpu=n, mode=mode, policy=pol, defer_requeue=True))
            for t in tasks:
                self.assertTrue(all(x >= 0 for x in t.stretches), (kind, n, mode, pol, t.tid))

    @unittest.expectedFailure
    def test_known_defect_f1_waits_nonnegative_multicore_default_model(self):
        # 기본 모델은 멀티코어에서 대기가 -1로 기록되는 경우가 있다(F1). 고치면 '예상 밖 성공'으로 알려 준다.
        for kind, n, mode, pol, s, pc in _grid(kinds=("mixed",), ncpus=(4,), policies=("static",), seeds=(0, 1)):
            tasks = make_workload(kind, n, s, per_cpu=pc)
            run(tasks, Config(ncpu=n, mode=mode, policy=pol))
            for t in tasks:
                self.assertTrue(all(x >= 0 for x in t.stretches))

    def test_one_cpu_global_equals_percpu_and_flag_is_inert(self):
        # CPU 1개에서는 global과 per-CPU가 같은 시스템이다. 뒤 순서 CPU가 없으므로 defer_requeue도 영향이 없다.
        for kind in KINDS:
            for pol in ("static", "hybrid"):
                base = None
                for mode in ("global", "percpu"):
                    for flag in (False, True):
                        r = run(make_workload(kind, 1, 3, per_cpu=6), Config(ncpu=1, mode=mode, policy=pol, defer_requeue=flag))
                        if base is None:
                            base = r
                        self.assertEqual(r, base, (kind, pol, mode, flag))

    def test_single_task_makespan_independent_of_cpu_count_with_defer_requeue(self):
        # 일 하나는 CPU가 늘어도 빨라지지도 느려지지도 않아야 한다. (락 경합 비용은 0으로 둔다: H10 참조)
        for bursts in ([10], [2, 5, 2], [30, 3, 25, 4, 9], [1]):
            for mode in ("global", "percpu"):
                got = []
                for n in (1, 2, 4, 8):
                    a = T(0, 0, "batch", bursts)
                    run([a], Config(ncpu=n, mode=mode, policy="static", base_q=4, boost_period=None,
                                    lock_cost=0.0, defer_requeue=True))
                    got.append(a.finish)
                self.assertEqual(len(set(got)), 1, (bursts, mode, got))

    @unittest.expectedFailure
    def test_known_defect_f1_single_task_independent_of_cpu_count_default_model(self):
        got = []
        for n in (1, 2, 4):
            a = T(0, 0, "batch", [10])
            run([a], Config(ncpu=n, mode="global", policy="static", base_q=4, boost_period=None, lock_cost=0.0))
            got.append(a.finish)
        self.assertEqual(len(set(got)), 1, got)

    def test_determinism_and_workload_independence_from_policy(self):
        # 같은 입력이면 같은 출력. 정책을 바꿔도 workload(task 목록)는 같다.
        a = make_workload("mixed", 4, 5, per_cpu=4)
        b = make_workload("mixed", 4, 5, per_cpu=4)
        self.assertEqual([(t.tid, t.arrival, t.kind, t.bursts) for t in a], [(t.tid, t.arrival, t.kind, t.bursts) for t in b])
        for flag in (False, True):
            cfg = Config(ncpu=4, mode="percpu", policy="hybrid", defer_requeue=flag)
            self.assertEqual(run(make_workload("mixed", 4, 5, per_cpu=4), cfg),
                             run(make_workload("mixed", 4, 5, per_cpu=4), cfg))


class WorkloadGenerator(unittest.TestCase):
    """README가 설명하는 workload를 생성기가 실제로 만드는지."""

    def test_counts_and_ids(self):
        for kind in KINDS:
            for n in (1, 4):
                ts = make_workload(kind, n, 0, per_cpu=5)
                want = 13 * n if kind == "starve" else 5 * n
                self.assertEqual(len(ts), want, (kind, n))
                self.assertEqual([t.tid for t in ts], list(range(want)))
                self.assertTrue(all(t.arrival >= 0 for t in ts))

    def test_burst_shapes(self):
        for kind in KINDS:
            for seed in range(5):
                for t in make_workload(kind, 2, seed, per_cpu=6):
                    self.assertEqual(len(t.bursts) % 2, 1, "버스트는 CPU로 시작해 CPU로 끝난다(홀수 개)")
                    cpu, io = t.bursts[0::2], t.bursts[1::2]
                    self.assertTrue(all(x >= 1 for x in cpu + io))
                    if t.kind == "interactive":
                        self.assertTrue(all(1 <= x <= 4 for x in cpu))
                        lo, hi = (4, 12) if kind == "starve" else (8, 30)
                        self.assertTrue(all(lo <= x <= hi for x in io))
                        self.assertEqual(len(cpu), 200 if kind == "starve" else len(cpu))
                        if kind != "starve":
                            self.assertTrue(40 <= len(cpu) <= 80)
                    else:
                        self.assertEqual(t.kind, "batch")
                        self.assertTrue(all(x <= 300 for x in cpu))
                        self.assertTrue(all(1 <= x <= 6 for x in io))
                        if kind == "starve":
                            self.assertEqual(t.cpu_total, 600)
                        else:
                            self.assertTrue(400 <= t.cpu_total <= 1200)

    def test_arrival_patterns(self):
        for seed in range(5):
            for t in make_workload("mixed", 4, seed):
                self.assertTrue(0 <= t.arrival <= 2000)
            for t in make_workload("bursty", 4, seed):
                self.assertTrue(any(w <= t.arrival <= w + 20 for w in (0, 600, 1200)), t.arrival)
            for t in make_workload("starve", 4, seed):
                if t.kind == "interactive":
                    self.assertTrue(0 <= t.arrival <= 200)
                else:
                    self.assertTrue(100 <= t.arrival <= 300)

    def test_interactive_share(self):
        # 10개 seed를 합친 task 중 interactive 비율이 설명한 값(interactive 0.8, mixed/bursty 0.5, batch 0.2) 근처인지.
        for kind, want in (("interactive", 0.8), ("mixed", 0.5), ("bursty", 0.5), ("batch", 0.2)):
            ts = [t for s in range(10) for t in make_workload(kind, 8, s, per_cpu=12)]
            share = sum(t.kind == "interactive" for t in ts) / len(ts)
            self.assertAlmostEqual(share, want, delta=0.05, msg=kind)

    def test_seed_changes_workload(self):
        a = make_workload("mixed", 4, 3, per_cpu=6)
        b = make_workload("mixed", 4, 4, per_cpu=6)
        self.assertNotEqual([t.bursts for t in a], [t.bursts for t in b])


if __name__ == "__main__":
    unittest.main()
