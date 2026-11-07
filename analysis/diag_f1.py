"""결함 F1(같은 틱 이중 사용)이 얼마나 자주 일어나는지 센다.

기본 모델에서는 quantum이 끝난 task가 ready_since=t+1로 큐에 들어가는데, 같은 틱 t에 뒤 순서 CPU가 그 task를 가져갈 수 있다.
그 디스패치의 대기가 t - (t+1) = -1로 기록되고, 가져간 CPU는 마지막으로 실행한 CPU와 다르므로 이주(+페널티)가 생긴다.
그래서 '대기가 음수인 디스패치'가 곧 F1 사건의 수이고, 그 수를 전체 디스패치와 이주에 견주어 센다.
zero_stall_events는 그 사건 중 가져간 CPU에 문맥 전환/락 정지가 없어서 같은 task를 두 CPU가 같은 틱에 실행한 경우의 수다
(sim.py의 사본에 한 줄을 더해 센다. 기본 비용(cs_cost=1)에서도 그 task가 그 CPU에서 마지막으로 실행한 task였으면 전환이 없다).

    python3 -I -B analysis/diag_f1.py        # 약 30초 -> results/validation/f1_negative_waits.csv (서로 다른 구성 31개)

seed 0~9, per_cpu 12, 기본 비용. migrations_defer는 같은 구성을 보정 모델(defer_requeue=True)로 돌린 이주 횟수다.
"""
from __future__ import annotations

import csv
import sys
import types
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mlfqsim import Config, make_workload, run  # noqa: E402

OUT = ROOT / "results" / "validation" / "f1_negative_waits.csv"
SEEDS = range(10)
COLUMNS = ["workload", "ncpu", "mode", "policy", "affinity", "lb_period", "idle_steal",
           "dispatches", "neg_waits", "neg_share_pct", "zero_stall_events", "migrations_default", "migrations_defer",
           "neg_over_migrations_pct"]


def load_probe():
    """sim.py의 사본에 한 줄만 더해, 대기가 음수인 디스패치의 정지 틱(문맥 전환 + 락)을 기록하게 한다.
    정지가 0이면 그 task를 두 CPU가 같은 틱에 실행한 것이다. 저장소의 sim.py는 바꾸지 않는다."""
    src = (ROOT / "mlfqsim" / "sim.py").read_text(encoding="utf-8")
    anchor = "                c.run = tk\n                c.prev_tid = tk.tid\n"
    assert src.count(anchor) == 1, "sim.py가 바뀌어 진단용 한 줄을 넣을 자리를 찾지 못했다"
    src = src.replace(anchor, anchor + "                if st < 0:\n                    EVENTS.append(stall)\n")
    mod = types.ModuleType("mlfqsim.sim_probe")
    mod.__package__ = "mlfqsim"
    mod.EVENTS = []
    sys.modules["mlfqsim.sim_probe"] = mod
    exec(compile(src, "sim_probe", "exec"), mod.__dict__)
    return mod


def count(kind, cfg, probe=None):
    """디스패치 수, 음수 대기 수, 이주 수(, 정지 0인 음수 대기 수)를 seed 0~9에서 합친다."""
    disp = neg = mig = 0
    if probe is not None:
        probe.EVENTS.clear()
    runner = probe.run if probe is not None else run
    for s in SEEDS:
        tasks = make_workload(kind, cfg.ncpu, s, per_cpu=12)
        r = runner(tasks, cfg)
        mig += r["migrations"]
        for t in tasks:
            disp += len(t.stretches)
            neg += sum(1 for x in t.stretches if x < 0)
    if probe is None:
        return disp, neg, mig
    assert len(probe.EVENTS) == neg, "진단용 사본이 센 사건 수와 대기 기록이 다르다"
    return disp, neg, mig, sum(1 for st in probe.EVENTS if st == 0)


def configs():
    for n in (1, 2, 4, 8, 16):
        for mode in (("global",) if n == 1 else ("global", "percpu")):
            yield "mixed", Config(ncpu=n, mode=mode, policy="static")
    for pol in ("load", "feedback", "hybrid"):
        for mode in ("global", "percpu"):
            yield "mixed", Config(ncpu=8, mode=mode, policy=pol)
    for kind in ("interactive", "batch", "bursty", "starve"):
        for mode in ("global", "percpu"):
            yield kind, Config(ncpu=8, mode=mode, policy="static")
    for aff in ("strict", "soft", "none"):
        for lb, steal in ((None, False), (None, True), (100, True)):
            yield "mixed", Config(ncpu=8, mode="percpu", policy="static", affinity=aff, lb_period=lb, idle_steal=steal)


def main() -> int:
    rows = []
    probe = load_probe()
    seen = set()
    for kind, cfg in configs():
        key = repr((kind, cfg))  # Config는 해시할 수 없어 글로 비교한다. E4의 기본 per-CPU와 E5의 soft+밸런싱 100+훔치기는 같은 구성이다
        if key in seen:
            continue
        seen.add(key)
        disp, neg, mig, zero = count(kind, cfg, probe)
        _, neg2, mig2 = count(kind, replace(cfg, defer_requeue=True))
        assert neg2 == 0, "보정 모델에서 음수 대기가 남았다"
        rows.append({
            "workload": kind, "ncpu": cfg.ncpu, "mode": cfg.mode, "policy": cfg.policy,
            "affinity": cfg.affinity if cfg.mode == "percpu" else "", "lb_period": (cfg.lb_period or 0) if cfg.mode == "percpu" else "",
            "idle_steal": int(cfg.idle_steal) if cfg.mode == "percpu" else "",
            "dispatches": disp, "neg_waits": neg, "neg_share_pct": round(100 * neg / disp, 3), "zero_stall_events": zero,
            "migrations_default": mig, "migrations_defer": mig2,
            "neg_over_migrations_pct": round(100 * neg / mig, 2) if mig else "",
        })
        print(rows[-1], flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print("saved", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
