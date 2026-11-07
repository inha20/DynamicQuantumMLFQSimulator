"""시험이 실제로 결함을 잡아내는지 확인한다(변이 점검).

시뮬레이터 코드를 임시 사본에서 한 곳씩 일부러 틀리게 고치고(변이), 시뮬레이터를 검증하는 시험(tests/test_sim.py,
tests/test_invariants.py)을 돌려 시험이 실패하는지 본다. 시작할 때 변이를 넣지 않은 사본이 시험을 통과하는지 먼저 확인한다.
시험이 실패하면 '잡힘', 모두 통과하면 '살아남음'이다. 살아남은 변이는 (a) 시험이 비어 있는 곳이거나 (b) 결과에 영향이 없는
동등한 변이다. 저장소의 원본 파일은 건드리지 않는다.

    python3 -I -B analysis/mutation_check.py            # 전체 (약 1~3분)
    python3 -I -B analysis/mutation_check.py --only M05,P01

결과는 results/validation/mutation_check.csv에 쓴다. 살아남은 변이가 있으면 종료 코드 1.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "validation" / "mutation_check.csv"
# 변이를 넣은 사본에서 돌리는 시험: 시뮬레이터를 검증하는 시험만 쓴다(analysis/ 도구 시험은 사본에 도구가 없다)
SIM_TESTS = ("test_sim.py", "test_invariants.py")

# (id, 파일, 바꿀 원문(파일에 정확히 한 번 나와야 함), 바뀐 글, 설명)
MUTANTS = [
    ("M01", "mlfqsim/sim.py", "tk.ready_since = t + 1\n                if cfg.defer_requeue:",
     "tk.ready_since = t\n                if cfg.defer_requeue:", "quantum 소진 task의 ready_since를 t+1 대신 t로"),
    ("M02", "mlfqsim/sim.py", "stall += cfg.cs_cost", "stall += 0", "문맥 전환 비용 무시"),
    ("M03", "mlfqsim/sim.py", "c.carry += cfg.lock_cost * (n - 1)", "c.carry += cfg.lock_cost * n",
     "락 비용에 (n-1) 대신 n"),
    ("M04", "mlfqsim/sim.py", "tk.remaining += cfg.mig_penalty", "tk.remaining += 0", "이주 페널티 무시"),
    ("M05", "mlfqsim/sim.py", "if c.run and m.top_level() < c.run.level:", "if c.run and m.top_level() <= c.run.level:",
     "같은 레벨도 선점"),
    ("M06", "mlfqsim/sim.py", "t % cfg.boost_period == 0", "t % cfg.boost_period == 1", "boost 시각을 한 틱 어긋나게"),
    ("M07", "mlfqsim/sim.py", "                    c.run.level = 0", "                    pass",
     "boost가 실행 중인 task의 레벨을 올리지 않음"),
    ("M08", "mlfqsim/sim.py", "tk.level = min(tk.level + 1, cfg.levels - 1)", "tk.level = min(tk.level + 1, cfg.levels - 2)",
     "마지막 레벨까지 내려가지 않음"),
    ("M09", "mlfqsim/sim.py", "if tk is None and not glob and cfg.idle_steal:", "if tk is None and not glob and False:",
     "idle 훔치기 끔"),
    ("M10", "mlfqsim/sim.py", "loads[lc] - loads[least] < cfg.soft_slack", "loads[lc] - loads[least] <= cfg.soft_slack",
     "soft affinity 경계(<를 <=로)"),
    ("M11", "mlfqsim/sim.py", "if ls[b] - ls[l] < 2 or not len(mlfqs[b]):", "if ls[b] - ls[l] < 3 or not len(mlfqs[b]):",
     "밸런싱 문턱을 2에서 3으로"),
    ("M12", "mlfqsim/sim.py", "len(mlfqs[v]) > bl", "len(mlfqs[v]) >= bl", "훔칠 CPU를 고를 때 동률이면 높은 번호"),
    ("M13", "mlfqsim/sim.py", "heapq.heappush(wake, (t + 1 + io, tk.tid, tk))", "heapq.heappush(wake, (t + io, tk.tid, tk))",
     "I/O 길이를 한 틱 짧게"),
    ("M14", "mlfqsim/sim.py", "if tk.last_cpu not in (-1, ci):", "if tk.last_cpu != ci:", "첫 실행도 이주로 셈"),
    ("M15", "mlfqsim/sim.py", "if tk.tid != c.prev_tid:", "if True:", "디스패치마다 문맥 전환으로 셈"),
    ("M16", "mlfqsim/sim.py", "st = t - tk.ready_since", "st = t - tk.ready_since - 1", "대기 계산을 한 틱 짧게"),
    ("M17", "mlfqsim/sim.py", '"util": useful / cap,', '"util": (useful + migrations * cfg.mig_penalty) / cap,',
     "유효 이용률에 이주 페널티 작업량을 포함"),
    ("M18", "mlfqsim/sim.py", "int(0.95 * len(xs))", "int(0.9 * len(xs))", "p95 위치를 p90으로"),
    ("M19", "mlfqsim/sim.py", "(max(busy) - min(busy)) / max(1, max(busy))", "(max(busy) - min(busy)) / max(1, sum(busy))",
     "불균형 분모를 합으로"),
    ("M20", "mlfqsim/sim.py", "t.max_wait > cfg.starve_thresh", "t.max_wait >= cfg.starve_thresh", "starved 경계(>를 >=로)"),
    ("M21", "mlfqsim/sim.py", 'if lc < 0 or cfg.affinity == "none":', 'if cfg.affinity == "none":',
     "한 번도 실행하지 않은 task의 배치"),
    ("M22", "mlfqsim/sim.py", "defer_requeue: bool = False", "defer_requeue: bool = True",
     "기본 모델을 보정 모델로 바꿈(저장된 결과가 달라지는 변경)"),
    ("P01", "mlfqsim/policies.py", "2.0 / (1.0 + p)", "1.0 / (1.0 + p)", "load 정책의 scale 식"),
    ("P02", "mlfqsim/policies.py", "0.9 * self.ema[level] + 0.1 *", "0.8 * self.ema[level] + 0.2 *", "feedback EMA 가중치"),
    ("P03", "mlfqsim/policies.py", "if self.ema[level] > 0.75:", "if self.ema[level] > 0.8:", "feedback 확대 문턱"),
    ("P04", "mlfqsim/policies.py", "self.s[level] * 1.05", "self.s[level] * 1.1", "feedback 확대 비율"),
    ("P05", "mlfqsim/policies.py", "return q.pop()", "return q.popleft()", "훔칠 때 꼬리 대신 머리를"),
    ("P06", "mlfqsim/policies.py", "for q in reversed(self.queues):", "for q in self.queues:",
     "훔칠 때 가장 낮은 우선순위 대신 가장 높은 우선순위 큐에서"),
    ("P07", "mlfqsim/policies.py", "                t.level = 0", "                t.level = t.level", "큐의 boost가 레벨을 올리지 않음"),
    ("P08", "mlfqsim/policies.py", "self.s[level] * self._load.scale(level, mlfq)", "self.s[level] + self._load.scale(level, mlfq)",
     "hybrid를 곱 대신 합으로"),
    ("P09", "mlfqsim/policies.py", "int(round(self.policy.scale(level, self) * self.policy.base_q(level)))",
     "int(self.policy.scale(level, self) * self.policy.base_q(level))", "quantum을 반올림 대신 버림으로"),
    ("P10", "mlfqsim/policies.py", "p = mlfq.n / mlfq.cpus_served", "p = mlfq.n", "load 정책이 CPU 수로 나누지 않음"),
    ("P11", "mlfqsim/policies.py", "q.appendleft(task) if front else q.append(task)", "q.append(task)", "선점된 task를 앞에 넣지 않음"),
    ("W01", "mlfqsim/workload.py", "bursts.append(rng.randint(1, 4))", "bursts.append(rng.randint(1, 5))",
     "interactive CPU 버스트 범위"),
    ("W02", "mlfqsim/workload.py", "total=600", "total=700", "starve workload의 batch 작업량"),
    ("W03", "mlfqsim/workload.py", '"interactive": 0.8', '"interactive": 0.7', "interactive workload의 interactive 비율"),
]


def make_tree(tmp: Path) -> None:
    shutil.copytree(ROOT / "mlfqsim", tmp / "mlfqsim", ignore=shutil.ignore_patterns("__pycache__"))
    (tmp / "tests").mkdir()
    for name in SIM_TESTS:
        shutil.copy(ROOT / "tests" / name, tmp / "tests" / name)


def run_tests(tmp: Path, timeout):
    return subprocess.run([sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", "tests"],
                          cwd=tmp, capture_output=True, text=True, timeout=timeout)


def baseline(timeout) -> bool:
    """변이를 넣지 않은 사본이 시험을 통과하는지 먼저 본다. 통과하지 못하면 모든 변이가 '잡힘'으로 보이는 헛점검이 된다."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        make_tree(tmp)
        p = run_tests(tmp, timeout)
        if p.returncode != 0:
            print("변이를 넣지 않은 사본이 시험을 통과하지 못한다. 점검을 중단한다.\n" + p.stderr[-1500:])
        return p.returncode == 0


def run_one(m, timeout):
    mid, rel, old, new, desc = m
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        make_tree(tmp)
        f = tmp / rel
        text = f.read_text(encoding="utf-8")
        if text.count(old) != 1:
            return mid, rel, desc, "오류", f"원문이 {text.count(old)}번 나온다"
        f.write_text(text.replace(old, new), encoding="utf-8")
        try:
            p = run_tests(tmp, timeout)
        except subprocess.TimeoutExpired:
            return mid, rel, desc, "잡힘", "시간 초과"
        if p.returncode == 0:
            return mid, rel, desc, "살아남음", ""
        first = ""
        for line in p.stderr.splitlines():
            if line.startswith(("FAIL:", "ERROR:")):
                first = line.split(" (")[0].split(": ", 1)[1]
                break
        return mid, rel, desc, "잡힘", first


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="쉼표로 구분한 변이 번호")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--out", default=None, help="CSV 경로. --only를 쓰면 이 값을 줄 때만 파일을 쓴다")
    ap.add_argument("--jobs", type=int, default=2)
    a = ap.parse_args(argv)
    want = {x for x in a.only.split(",") if x}
    muts = [m for m in MUTANTS if not want or m[0] in want]
    if not baseline(a.timeout):
        return 2
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        rows = list(ex.map(lambda m: run_one(m, a.timeout), muts))
    for mid, rel, desc, status, note in rows:
        print(f"{mid} {status:<4} {rel:<22} {desc}" + (f"  [{note}]" if note else ""), flush=True)
    if a.out or not want:
        out = Path(a.out) if a.out else OUT
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["id", "file", "description", "status", "first_failing_test"])
            w.writerows(rows)
    killed = sum(r[3] == "잡힘" for r in rows)
    bad = [r for r in rows if r[3] != "잡힘"]
    print(f"변이 {len(rows)}개 중 잡힘 {killed}, 살아남음/오류 {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
