"""전체 실험 실행: python experiments.py  ->  results/*.csv, results/*.md, results/*.png"""
from __future__ import annotations

import csv
import statistics
import sys
from dataclasses import replace
from pathlib import Path

from mlfqsim import Config, make_workload, run

OUT = Path(__file__).parent / "results"
OUT.mkdir(exist_ok=True)
SEEDS = range(10)
KEYS = ["avg_wait", "avg_resp", "int_lat_mean", "int_lat_p95", "batch_slowdown", "max_wait",
        "starved", "switches", "switch_per_kt", "migrations", "util", "overhead_frac",
        "idle_frac", "imbalance", "makespan", "jain_batch"]


def avg_runs(kind: str, cfg: Config, per_cpu: int = 12) -> dict:
    rows = [run(make_workload(kind, cfg.ncpu, s, per_cpu=per_cpu), cfg) for s in SEEDS]
    assert all(r["unfinished"] == 0 for r in rows), "시뮬레이션 미종료"
    return {k: statistics.fmean(r[k] for r in rows) for k in KEYS}


def write(name: str, rows: list[dict]) -> None:
    cols = list(rows[0].keys())
    with open(OUT / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()})


def md_table(rows, cols, fmt="{:.1f}") -> str:
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(fmt.format(r[c]) if isinstance(r[c], float) else str(r[c])
                                     for c in cols) + " |")
    return "\n".join(out)


def e1_quantum_sweep():
    """정적 base quantum 스윕 (단일 코어): 대기/응답/context switch trade-off."""
    rows = []
    for kind in ("interactive", "mixed", "batch"):
        for q in (1, 2, 4, 8, 16, 32, 64):
            cfg = Config(ncpu=1, policy="static", base_q=q, boost_period=1000)
            rows.append({"workload": kind, "base_q": q, **avg_runs(kind, cfg, per_cpu=10)})
    write("e1_static_quantum_sweep", rows)
    return rows


def e2_dynamic_policies():
    """정적 vs 동적 quantum 정책 (단일 코어, 모든 workload)."""
    rows = []
    for kind in ("interactive", "mixed", "batch", "bursty", "starve"):
        for pol in ("static", "load", "feedback", "hybrid"):
            cfg = Config(ncpu=1, policy=pol, base_q=4, boost_period=1000)
            rows.append({"workload": kind, "policy": pol,
                         **avg_runs(kind, cfg, per_cpu=10)})
    write("e2_dynamic_policies", rows)
    return rows


def e3_starvation():
    """priority boost 주기에 따른 starvation vs interactive 지연 (starve workload)."""
    rows = []
    for pol in ("static", "load", "hybrid"):
        for bp in (None, 5000, 2000, 1000, 500, 200, 100):
            cfg = Config(ncpu=1, policy=pol, base_q=4, boost_period=bp, starve_thresh=500)
            rows.append({"policy": pol, "boost": bp or 0, **avg_runs("starve", cfg)})
    write("e3_starvation_boost", rows)
    return rows


def e4_multicore():
    """global MLFQ vs per-CPU runqueue (코어 수 스케일링)."""
    rows = []
    for n in (2, 4, 8, 16):
        for kind in ("mixed", "interactive"):
            variants = [("global", Config(ncpu=n, mode="global", policy="static"))]
            variants.append(("global+hybrid", Config(ncpu=n, mode="global", policy="hybrid")))
            variants.append(("percpu(soft,lb100)", Config(ncpu=n, mode="percpu", policy="static")))
            variants.append(("percpu+hybrid", Config(ncpu=n, mode="percpu", policy="hybrid")))
            for name, cfg in variants:
                rows.append({"ncpu": n, "workload": kind, "config": name, **avg_runs(kind, cfg)})
    write("e4_multicore_scaling", rows)
    return rows


def e5_affinity_lb():
    """per-CPU: affinity x load-balancing 정책 (8 코어)."""
    rows = []
    for kind in ("mixed", "bursty"):
        for aff in ("strict", "soft", "none"):
            for lb, steal in ((None, False), (None, True), (400, True), (100, True), (25, True)):
                cfg = Config(ncpu=8, mode="percpu", policy="static", affinity=aff,
                             lb_period=lb, idle_steal=steal)
                rows.append({"workload": kind, "affinity": aff,
                             "lb_period": lb or 0, "idle_steal": int(steal),
                             **avg_runs(kind, cfg)})
    write("e5_affinity_loadbalance", rows)
    return rows


def e6_lock_sensitivity():
    """global 큐 락 경합 비용 / 캐시 migration 비용 민감도 (8 코어, mixed)."""
    rows = []
    for lock in (0.0, 0.05, 0.1, 0.25, 0.5):
        for mig in (0, 3, 10):
            g = avg_runs("mixed", Config(ncpu=8, mode="global", lock_cost=lock, mig_penalty=mig))
            p = avg_runs("mixed", Config(ncpu=8, mode="percpu", lock_cost=lock, mig_penalty=mig))
            rows.append({"lock_cost": lock, "mig_penalty": mig,
                         "g_wait": g["avg_wait"], "p_wait": p["avg_wait"],
                         "g_lat95": g["int_lat_p95"], "p_lat95": p["int_lat_p95"],
                         "g_makespan": g["makespan"], "p_makespan": p["makespan"],
                         "g_util": g["util"], "p_util": p["util"],
                         "wait_ratio_p_over_g": p["avg_wait"] / g["avg_wait"]})
    write("e6_cost_sensitivity", rows)
    return rows


def plots(e1, e3, e4):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, ax = plt.subplots(1, 3, figsize=(15, 4))
    for kind, mk in (("interactive", "o"), ("mixed", "s"), ("batch", "^")):
        r = [x for x in e1 if x["workload"] == kind]
        q = [x["base_q"] for x in r]
        ax[0].plot(q, [x["avg_wait"] for x in r], mk + "-", label=kind)
        ax[1].plot(q, [x["int_lat_p95"] for x in r], mk + "-", label=kind)
        ax[2].plot(q, [x["switch_per_kt"] for x in r], mk + "-", label=kind)
    for a, t in zip(ax, ("avg wait (ticks)", "interactive wakeup latency p95", "context switches / 1000 ticks")):
        a.set_xscale("log", base=2); a.set_xlabel("base quantum (level 0)"); a.set_title(t); a.legend(); a.grid(alpha=.3)
    fig.tight_layout(); fig.savefig(OUT / "e1_quantum_tradeoff.png", dpi=130); plt.close(fig)

    fig, ax = plt.subplots(1, 2, figsize=(10, 4))
    for pol in ("static", "load", "hybrid"):
        r = [x for x in e3 if x["policy"] == pol and x["boost"] > 0]
        b = [x["boost"] for x in r]
        ax[0].plot(b, [x["max_wait"] for x in r], "o-", label=pol)
        ax[1].plot(b, [x["int_lat_p95"] for x in r], "o-", label=pol)
    ax[0].set_title("max wait (starvation)"); ax[1].set_title("interactive latency p95")
    for a in ax:
        a.set_xscale("log"); a.set_xlabel("boost period"); a.legend(); a.grid(alpha=.3)
    fig.tight_layout(); fig.savefig(OUT / "e3_boost_tradeoff.png", dpi=130); plt.close(fig)

    fig, ax = plt.subplots(1, 3, figsize=(15, 4))
    for cfgname in ("global", "global+hybrid", "percpu(soft,lb100)", "percpu+hybrid"):
        r = [x for x in e4 if x["config"] == cfgname and x["workload"] == "mixed"]
        n = [x["ncpu"] for x in r]
        ax[0].plot(n, [x["avg_wait"] for x in r], "o-", label=cfgname)
        ax[1].plot(n, [x["util"] for x in r], "o-", label=cfgname)
        ax[2].plot(n, [x["int_lat_p95"] for x in r], "o-", label=cfgname)
    for a, t in zip(ax, ("avg wait", "useful CPU utilization", "interactive latency p95")):
        a.set_xscale("log", base=2); a.set_xlabel("cores"); a.set_title(t + " (mixed)"); a.legend(fontsize=8); a.grid(alpha=.3)
    fig.tight_layout(); fig.savefig(OUT / "e4_multicore_scaling.png", dpi=130); plt.close(fig)


def main():
    e1 = e1_quantum_sweep(); print("e1 done", flush=True)
    e2 = e2_dynamic_policies(); print("e2 done", flush=True)
    e3 = e3_starvation(); print("e3 done", flush=True)
    e4 = e4_multicore(); print("e4 done", flush=True)
    e5 = e5_affinity_lb(); print("e5 done", flush=True)
    e6 = e6_lock_sensitivity(); print("e6 done", flush=True)
    plots(e1, e3, e4)
    c = ["avg_wait", "avg_resp", "int_lat_p95", "batch_slowdown", "max_wait", "starved",
         "switch_per_kt", "util"]
    with open(OUT / "summary.md", "w", encoding="utf-8") as f:
        f.write("# 실험 결과 요약 (seed 10회 평균, 단위: tick)\n\n")
        f.write("## E1. 정적 base quantum 스윕 (1 core)\n\n")
        f.write(md_table(e1, ["workload", "base_q"] + c) + "\n\n")
        f.write("## E2. 정적 vs 동적 quantum (1 core)\n\n")
        f.write(md_table(e2, ["workload", "policy"] + c) + "\n\n")
        f.write("## E3. priority boost 주기 (starve workload, 1 core)\n\n")
        f.write(md_table(e3, ["policy", "boost"] + c) + "\n\n")
        f.write("## E4. global vs per-CPU 스케일링\n\n")
        f.write(md_table(e4, ["ncpu", "workload", "config"] + c + ["migrations", "overhead_frac"]) + "\n\n")
        f.write("## E5. affinity x load balancing (8 core)\n\n")
        f.write(md_table(e5, ["workload", "affinity", "lb_period", "idle_steal"] + c +
                         ["migrations", "imbalance", "makespan"]) + "\n\n")
        f.write("## E6. 락 경합/마이그레이션 비용 민감도 (8 core, mixed)\n\n")
        f.write(md_table(e6, list(e6[0].keys()), fmt="{:.2f}") + "\n")
    print("saved to", OUT)


if __name__ == "__main__":
    sys.exit(main())
