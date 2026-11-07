"""쌍체 부트스트랩 신뢰구간 (seed 0~9, 탐색용).

README의 비교(정적 대 동적 정책, global 대 per-CPU 등)마다 같은 seed(같은 workload)에서 두 구성의 지표를 구해
seed별 차이(b - a)를 만들고, seed를 복원추출로 10000번 다시 뽑아 평균 차이의 95% 백분위 구간을 계산한다.
상대 변화(%)는 README와 같은 정의인 (b 평균 / a 평균 - 1)이고 같은 재표본으로 구간을 만든다.

    python3 -I -B analysis/bootstrap_ci.py            # 전체 (1~2분) -> results/validation/ci_summary.csv
    python3 -I -B analysis/bootstrap_ci.py --only E4  # 이름이 E4로 시작하는 비교만

읽는 법과 한계(분석 계획이 아니라 저장된 탐색적 결과에 구간을 붙이는 도구다):
- 표본이 seed 10개뿐이라 백분위 부트스트랩 구간은 실제보다 좁게 나오는 경향이 있다. t 구간(t_lo, t_hi)을 함께 적는다.
- 비교를 고르는 데 쓴 seed와 같은 seed라 확증이 아니다. 여러 비교를 한꺼번에 보므로 다중 비교 보정도 하지 않았다.
- model 열: default = 기본 모델(저장된 결과와 같은 모델), defer_requeue = 같은 틱 이중 사용 결함 F1을 보정한 모델,
  default->defer = 같은 구성을 두 모델로 돌려 모델 차이 자체를 잰 행(exp가 F1).
- csv_check 열: default 모델 행의 두 평균을 results/*.csv에 저장된 값과 맞춰 본 결과(저장 값은 소수 셋째 자리 반올림이라 0.0006까지 허용).
  이 도구가 experiments.py와 같은 실험을 하고 있다는 확인이다. 어긋나면 종료 코드 1.
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import sys
import time
from dataclasses import astuple, dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mlfqsim import Config, make_workload, run  # noqa: E402

RESULTS = ROOT / "results"
OUT = RESULTS / "validation" / "ci_summary.csv"
SEEDS = tuple(range(10))
REPS = 10000
# 양쪽 95% t 분위수 (자유도 -> 값)
T975 = {9: 2.262, 19: 2.093, 29: 2.045, 49: 2.010, 99: 1.984}
CSV_TOL = 6e-4

COLUMNS = ["cid", "exp", "workload", "a", "b", "model", "paired", "n", "metric",
           "mean_a", "mean_b", "diff", "diff_lo", "diff_hi", "t_lo", "t_hi",
           "rel_pct", "rel_lo", "rel_hi", "b_gt_a", "b_lt_a", "b_eq_a", "csv_check"]


# ---------------------------------------------------------------- 통계

def percentile(sorted_xs, q):
    return sorted_xs[int(q * (len(sorted_xs) - 1) + 0.5)]


def bootstrap(a, b, paired, rng, reps=REPS):
    """a, b: 두 구성의 seed별 값. paired면 같은 seed 위치를 함께 뽑는다.
    반환: (평균 차이 구간, 상대 변화(%) 구간). 상대 변화를 정의할 수 없으면 None."""
    na, nb = len(a), len(b)
    diffs, rels = [], []
    for _ in range(reps):
        ia = [int(rng.random() * na) for _ in range(na)]
        ib = ia if paired else [int(rng.random() * nb) for _ in range(nb)]
        ma = sum(a[i] for i in ia) / na
        mb = sum(b[i] for i in ib) / nb
        diffs.append(mb - ma)
        if ma != 0:
            rels.append(100.0 * (mb / ma - 1.0))
    diffs.sort()
    d = (percentile(diffs, 0.025), percentile(diffs, 0.975))
    if len(rels) < reps:  # 어떤 재표본에서 a 평균이 0이면 상대 변화를 정의하지 않는다
        return d, None
    rels.sort()
    return d, (percentile(rels, 0.025), percentile(rels, 0.975))


def t_interval(diffs):
    n = len(diffs)
    if n - 1 not in T975 or n < 2:
        return None
    m = sum(diffs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in diffs) / (n - 1))
    h = T975[n - 1] * sd / math.sqrt(n)
    return m - h, m + h


# ---------------------------------------------------------------- 실행

_cache: dict = {}


def per_seed(kind, cfg, per_cpu, seeds=SEEDS):
    key = (kind, per_cpu, tuple(seeds), astuple(cfg))
    if key not in _cache:
        rows = [run(make_workload(kind, cfg.ncpu, s, per_cpu=per_cpu), cfg) for s in seeds]
        if any(r["unfinished"] for r in rows):
            raise RuntimeError(f"시뮬레이션 미종료: {kind} {cfg}")
        _cache[key] = rows
    return _cache[key]


def saved_value(ref, metric):
    """results/*.csv에서 ref=(파일, 필터, 지표->열 이름) 행의 값을 읽는다. 열이 없으면 None."""
    fname, filt, colmap = ref
    col = colmap.get(metric, metric) if colmap else metric
    with open(RESULTS / fname, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if all(float(row[k]) == float(v) if _isnum(v) else row[k] == v for k, v in filt.items()):
                return float(row[col]) if col in row else None
    return None


def _isnum(v):
    return isinstance(v, (int, float))


@dataclass
class Contrast:
    cid: str
    exp: str
    workload: str
    a_label: str
    b_label: str
    cfg_a: Config
    cfg_b: Config
    per_cpu: int
    metrics: tuple
    paired: bool = True
    models: tuple = ("default",)
    ref_a: tuple | None = None
    ref_b: tuple | None = None
    model_effect: bool = False


def _fmt(x):
    return "" if x is None else round(x, 4)


def evaluate(c: Contrast, model: str):
    """한 비교를 한 모델에서 계산해 CSV 행 목록과 csv_check 불일치 목록을 돌려준다."""
    if c.model_effect:
        ca, cb, label = c.cfg_a, c.cfg_b, "default->defer"
    else:
        flag = model == "defer_requeue"
        ca, cb, label = replace(c.cfg_a, defer_requeue=flag), replace(c.cfg_b, defer_requeue=flag), model
    ra = per_seed(c.workload, ca, c.per_cpu)
    rb = per_seed(c.workload, cb, c.per_cpu)
    rows, bad = [], []
    for m in c.metrics:
        a = [r[m] for r in ra]
        b = [r[m] for r in rb]
        mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
        rng = random.Random(f"{c.cid}|{m}|{label}")
        (dlo, dhi), rel = bootstrap(a, b, c.paired, rng)
        t = t_interval([y - x for x, y in zip(a, b)]) if c.paired else None
        check = "n/a"
        if label == "default" and c.ref_a and c.ref_b:
            va, vb = saved_value(c.ref_a, m), saved_value(c.ref_b, m)
            if va is not None and vb is not None:
                ok = abs(mean_a - va) <= CSV_TOL and abs(mean_b - vb) <= CSV_TOL
                check = "ok" if ok else "mismatch"
                if not ok:
                    bad.append((c.cid, m, mean_a, va, mean_b, vb))
        gt = sum(y > x for x, y in zip(a, b)) if c.paired else None
        lt = sum(y < x for x, y in zip(a, b)) if c.paired else None
        rows.append({
            "cid": c.cid, "exp": c.exp, "workload": c.workload, "a": c.a_label, "b": c.b_label,
            "model": label, "paired": int(c.paired), "n": len(a), "metric": m,
            "mean_a": _fmt(mean_a), "mean_b": _fmt(mean_b), "diff": _fmt(mean_b - mean_a),
            "diff_lo": _fmt(dlo), "diff_hi": _fmt(dhi),
            "t_lo": _fmt(t[0]) if t else "", "t_hi": _fmt(t[1]) if t else "",
            "rel_pct": _fmt(100.0 * (mean_b / mean_a - 1.0)) if mean_a != 0 else "",
            "rel_lo": _fmt(rel[0]) if rel else "", "rel_hi": _fmt(rel[1]) if rel else "",
            "b_gt_a": "" if gt is None else gt, "b_lt_a": "" if lt is None else lt,
            "b_eq_a": "" if gt is None else len(a) - gt - lt, "csv_check": check,
        })
    return rows, bad


# ---------------------------------------------------------------- 비교 목록

BOTH = ("default", "defer_requeue")


def contrasts():
    cs: list[Contrast] = []

    # E1: 정적 quantum (mixed, 1코어, per_cpu 10)
    def e1(q):
        return Config(ncpu=1, policy="static", base_q=q, boost_period=1000)

    def e1ref(q):
        return ("e1_static_quantum_sweep.csv", {"workload": "mixed", "base_q": q}, None)
    for cid, qa, qb in (("E1-01", 1, 4), ("E1-02", 4, 16), ("E1-03", 16, 64), ("E1-04", 4, 64)):
        cs.append(Contrast(cid, "E1", "mixed", f"base_q={qa}", f"base_q={qb}", e1(qa), e1(qb), 10,
                           ("avg_wait", "switch_per_kt", "int_lat_p95", "max_wait"),
                           ref_a=e1ref(qa), ref_b=e1ref(qb)))

    # E2: 정적 대 동적 (모든 workload, 1코어, per_cpu 10)
    def e2(pol):
        return Config(ncpu=1, policy=pol, base_q=4, boost_period=1000)
    for wl in ("interactive", "mixed", "batch", "bursty", "starve"):
        for pol in ("load", "feedback", "hybrid"):
            ref = lambda p, wl=wl: ("e2_dynamic_policies.csv", {"workload": wl, "policy": p}, None)  # noqa: E731
            cs.append(Contrast(f"E2-{wl}-{pol}", "E2", wl, "static", pol, e2("static"), e2(pol), 10,
                               ("avg_wait", "avg_resp", "int_lat_p95", "switch_per_kt", "max_wait", "starved"),
                               ref_a=ref("static"), ref_b=ref(pol)))

    # E3: boost (starve workload, 1코어)
    def e3(pol, bp):
        return Config(ncpu=1, policy=pol, base_q=4, boost_period=bp, starve_thresh=500)

    def e3ref(pol, bp):
        return ("e3_starvation_boost.csv", {"policy": pol, "boost": bp or 0}, None)
    for cid, (pa, ba), (pb, bb) in (("E3-01", ("static", None), ("static", 100)),
                                    ("E3-02", ("static", None), ("static", 1000)),
                                    ("E3-03", ("static", None), ("hybrid", None))):
        cs.append(Contrast(cid, "E3", "starve", f"{pa},boost={ba}", f"{pb},boost={bb}", e3(pa, ba), e3(pb, bb), 12,
                           ("avg_wait", "max_wait", "int_lat_p95", "switch_per_kt", "starved"),
                           ref_a=e3ref(pa, ba), ref_b=e3ref(pb, bb)))

    # E4: global 대 per-CPU (mixed)
    def e4cfg(n, mode):
        return Config(ncpu=n, mode=mode, policy="static")

    def e4ref(n, name):
        return ("e4_multicore_scaling.csv", {"ncpu": n, "workload": "mixed", "config": name}, None)
    pname = "percpu(soft,lb100)"
    for n in (2, 4, 8, 16):
        cs.append(Contrast(f"E4-{n:02d}", "E4", "mixed", "global", "percpu", e4cfg(n, "global"), e4cfg(n, "percpu"), 12,
                           ("avg_wait", "int_lat_p95", "util", "migrations", "overhead_frac"), models=BOTH,
                           ref_a=e4ref(n, "global"), ref_b=e4ref(n, pname)))
    cs.append(Contrast("E4-scale-global", "E4", "mixed", "global n=2", "global n=16", e4cfg(2, "global"),
                       e4cfg(16, "global"), 12, ("avg_wait", "util", "migrations"), paired=False, models=BOTH,
                       ref_a=e4ref(2, "global"), ref_b=e4ref(16, "global")))
    cs.append(Contrast("E4-scale-percpu", "E4", "mixed", "percpu n=2", "percpu n=16", e4cfg(2, "percpu"),
                       e4cfg(16, "percpu"), 12, ("avg_wait", "util", "migrations"), paired=False, models=BOTH,
                       ref_a=e4ref(2, pname), ref_b=e4ref(16, pname)))

    # E5: affinity x 밸런싱 (mixed, 8코어)
    def e5(aff, lb, steal):
        return Config(ncpu=8, mode="percpu", policy="static", affinity=aff, lb_period=lb, idle_steal=steal)

    def e5ref(aff, lb, steal):
        return ("e5_affinity_loadbalance.csv",
                {"workload": "mixed", "affinity": aff, "lb_period": lb or 0, "idle_steal": int(steal)}, None)
    m5 = ("avg_wait", "int_lat_p95", "migrations", "makespan", "imbalance")
    for cid, A, B in (("E5-01", ("strict", None, False), ("strict", None, True)),
                      ("E5-02", ("strict", 400, True), ("strict", 25, True)),
                      ("E5-03", ("strict", None, False), ("none", None, False)),
                      ("E5-04", ("strict", 100, True), ("soft", 100, True)),
                      ("E5-05", ("strict", 100, True), ("none", 100, True))):
        lab = lambda t: f"{t[0]},lb={t[1]},steal={t[2]}"  # noqa: E731
        cs.append(Contrast(cid, "E5", "mixed", lab(A), lab(B), e5(*A), e5(*B), 12, m5, models=BOTH,
                           ref_a=e5ref(*A), ref_b=e5ref(*B)))

    # E6: 비용 민감도 (mixed, 8코어). E6 CSV는 global=g_*, per-CPU=p_* 열을 쓴다.
    def e6(mode, lock, mig):
        return Config(ncpu=8, mode=mode, lock_cost=lock, mig_penalty=mig)
    cmap = {"g": {"avg_wait": "g_wait", "int_lat_p95": "g_lat95", "makespan": "g_makespan", "util": "g_util"},
            "p": {"avg_wait": "p_wait", "int_lat_p95": "p_lat95", "makespan": "p_makespan", "util": "p_util"}}

    def e6ref(mode, lock, mig):
        return ("e6_cost_sensitivity.csv", {"lock_cost": lock, "mig_penalty": mig}, cmap["g" if mode == "global" else "p"])
    m6 = ("avg_wait", "int_lat_p95", "makespan", "util")
    for cid, A, B in (("E6-00", ("global", 0.0, 0), ("percpu", 0.0, 0)),
                      ("E6-01", ("global", 0.0, 3), ("percpu", 0.0, 3)),
                      ("E6-02", ("global", 0.0, 10), ("percpu", 0.0, 10)),
                      ("E6-03", ("global", 0.25, 3), ("percpu", 0.25, 3)),
                      ("E6-04", ("global", 0.25, 10), ("percpu", 0.25, 10)),
                      ("E6-05", ("global", 0.0, 3), ("global", 0.0, 10)),
                      ("E6-06", ("percpu", 0.0, 3), ("percpu", 0.0, 10)),
                      ("E6-07", ("global", 0.0, 3), ("global", 0.25, 3))):
        lab = lambda t: f"{t[0]},lock={t[1]},mig={t[2]}"  # noqa: E731
        cs.append(Contrast(cid, "E6", "mixed", lab(A), lab(B), e6(*A), e6(*B), 12, m6, models=BOTH,
                           ref_a=e6ref(*A), ref_b=e6ref(*B)))

    # F1: 같은 구성을 기본 모델과 보정 모델로 돌려 모델 차이 자체를 잰다 (a=기본, b=보정)
    mf = ("avg_wait", "int_lat_p95", "util", "migrations", "makespan", "overhead_frac")
    for n in (2, 4, 8, 16):
        for mode in ("global", "percpu"):
            cfg = e4cfg(n, mode)
            cs.append(Contrast(f"F1-E4-{mode}-{n:02d}", "F1", "mixed", "default", "defer_requeue",
                               cfg, replace(cfg, defer_requeue=True), 12, mf, model_effect=True))
    for lock, mig in ((0.0, 3), (0.0, 10), (0.25, 3), (0.25, 10)):
        for mode in ("global", "percpu"):
            cfg = e6(mode, lock, mig)
            cs.append(Contrast(f"F1-E6-{mode}-lock{lock}-mig{mig}", "F1", "mixed", "default", "defer_requeue",
                               cfg, replace(cfg, defer_requeue=True), 12, mf, model_effect=True))
    for aff, lb, steal in (("strict", None, False), ("strict", 100, True), ("soft", 100, True), ("none", 100, True)):
        cfg = e5(aff, lb, steal)
        cs.append(Contrast(f"F1-E5-{aff}-lb{lb or 0}-steal{int(steal)}", "F1", "mixed", "default", "defer_requeue",
                           cfg, replace(cfg, defer_requeue=True), 12, mf, model_effect=True))
    return cs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="비교 이름이 이 접두어로 시작하는 것만(쉼표로 여러 개)")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    prefixes = tuple(x for x in a.only.split(",") if x)
    t0 = time.time()
    rows, bad, n_ok = [], [], 0
    for c in contrasts():
        if prefixes and not c.cid.startswith(prefixes):
            continue
        for model in (("default",) if c.model_effect else c.models):
            r, b = evaluate(c, model)
            rows.extend(r)
            bad.extend(b)
    n_ok = sum(r["csv_check"] == "ok" for r in rows)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"행 {len(rows)}개 -> {out} ({time.time() - t0:.0f}초). 저장된 결과와 맞춘 행 {n_ok}개, 어긋난 행 {len(bad)}개")
    for cid, m, ma, va, mb, vb in bad[:10]:
        print(f"  어긋남 {cid} {m}: a {ma:.4f} (저장 {va}) / b {mb:.4f} (저장 {vb})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
