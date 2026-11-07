"""사전 분석 계획 초안(docs/02)의 seed 수 산정표를 저장된 신뢰구간 요약에서 계산한다. 시뮬레이션을 새로 돌리지 않는다.

    python3 -I -B analysis/plan_numbers.py     # -> results/validation/plan_seed_counts.csv (1초 미만)

results/validation/ci_summary.csv의 기본 모델(default) 행에는 seed 10개의 짝지은 차이(b - a)에 대한 t 구간이 있다.
그 폭에서 seed별 차이의 표준편차를 거꾸로 구한다.

    SD = (t_hi - t_lo) / (2 * 2.262) * sqrt(10)        # 2.262는 자유도 9의 양쪽 95% t 분위수

- 상대 척도(measure=rel): sd = SD / mean_a * 100 (%). 평균 대기, makespan처럼 시간의 합이나 평균인 지표에 쓴다.
- 절대 척도(measure=abs): sd = SD (틱). interactive p95처럼 기준값이 작거나 0에 가까워 비율이 불안정한 지표에 쓴다.

필요한 seed 수는 반폭 h(95% 구간의 반)를 정규 근사로 맞추는 수에 작은 표본 보정으로 2를 더한 값이다.

    n = ceil((1.96 * sd / h) ** 2) + 2

h의 후보는 상대 척도 5, 2.5, 1(%)과 절대 척도 4, 2, 1(틱)이다(열 n_a, n_b, n_c).

이 수는 보정하지 않은 95% 구간 기준이다. 사전 분석 계획은 비교를 가족(비교 k개)으로 묶어 구간 수준을 1 - 0.05/k로 올린다.
그래서 확인용 seed를 N_PLAN(100)개 쓸 때 가족 수준의 구간 반폭도 함께 낸다(열 k, h100).

    h100 = z_k * sd / sqrt(100),  z_k = 표준정규분포의 (1 - 0.05 / (2k)) 분위수(k = 1이면 1.96)

h100의 단위는 sd와 같다(상대 척도는 기준 평균에 대한 %, 절대 척도는 틱). 정규 근사다.

한계: 이것은 탐색용 seed 10개에서 구한 표준편차이고(표본이 작아 불확실하다), 비교를 고른 뒤에 본 값이다.
계획용 값이며 근거가 있는 값이 아니다. 파일럿이나 동결 때 다시 계산한다. 새 seed(확인용)의 결과는 쓰지 않았다.
"""
from __future__ import annotations

import csv
import math
import sys
from pathlib import Path
from statistics import NormalDist

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "results" / "validation" / "ci_summary.csv"
OUT = ROOT / "results" / "validation" / "plan_seed_counts.csv"
T_DF9 = 2.262
Z = 1.96
N_PLAN = 100
TARGETS = {"rel": (5.0, 2.5, 1.0), "abs": (4.0, 2.0, 1.0)}
COLUMNS = ["cid", "metric", "measure", "mean_a", "sd", "n_a", "n_b", "n_c", "k", "h100"]

# (비교 번호, 지표, 척도, 가족 크기 k). 비교 번호는 ci_summary.csv의 cid다.
# k: 평균 대기와 p95의 mixed 비교는 RQ-A 가족(11), 락 0·이주 0과 락 0.25·이주 3은 RQ-B 명제 가족(6),
# 나머지 E6 칸은 격자 지도(90). 가족은 docs/02-questions-and-preanalysis.md §7.1의 SM-4다.
ROWS = [
    ("E2-mixed-load", "avg_wait", "rel", 11),
    ("E2-mixed-feedback", "avg_wait", "rel", 11),
    ("E2-mixed-hybrid", "avg_wait", "rel", 11),
    ("E2-mixed-load", "int_lat_p95", "abs", 11),
    ("E2-mixed-feedback", "int_lat_p95", "abs", 11),
    ("E2-mixed-hybrid", "int_lat_p95", "abs", 11),
    ("E6-00", "avg_wait", "rel", 6),
    ("E6-01", "avg_wait", "rel", 90),
    ("E6-03", "avg_wait", "rel", 6),
    ("E6-04", "avg_wait", "rel", 90),
    ("E6-00", "int_lat_p95", "abs", 6),
    ("E6-01", "int_lat_p95", "abs", 90),
    ("E6-04", "int_lat_p95", "abs", 90),
]


def seeds_needed(sd: float, half_width: float) -> int:
    return math.ceil((Z * sd / half_width) ** 2) + 2


def family_z(k: int) -> float:
    """가족 크기 k의 양쪽 수준 1 - 0.05/k에 해당하는 표준정규 분위수. k = 1이면 1.96이다."""
    if not isinstance(k, int) or k < 1:
        raise ValueError(f"가족 크기 k는 1 이상의 정수여야 한다({k!r})")
    return NormalDist().inv_cdf(1 - 0.05 / (2 * k))


def half_width(sd: float, n: int, k: int) -> float:
    """seed n개일 때 가족 크기 k의 구간 반폭(정규 근사)."""
    if n < 2:
        raise ValueError("seed 수 n은 2 이상이어야 한다")
    return family_z(k) * sd / math.sqrt(n)


def compute(ci_rows: list[dict], rows=ROWS) -> list[dict]:
    out = []
    for row in rows:
        cid, metric, measure = row[:3]
        k = row[3] if len(row) > 3 else 1
        hit = [r for r in ci_rows if r["model"] == "default" and r["cid"] == cid and r["metric"] == metric]
        if len(hit) != 1:
            raise ValueError(f"{cid} {metric}: 기본 모델의 행이 1개여야 한다({len(hit)}개)")
        r = hit[0]
        if int(r["n"]) != 10 or r["paired"] != "1":
            raise ValueError(f"{cid} {metric}: seed 10개의 짝지은 비교가 아니다")
        mean_a = float(r["mean_a"])
        sd_diff = (float(r["t_hi"]) - float(r["t_lo"])) / (2 * T_DF9) * math.sqrt(10)
        if measure == "rel":
            if mean_a == 0:
                raise ValueError(f"{cid} {metric}: 기준 평균이 0이라 상대 척도를 쓸 수 없다")
            sd = sd_diff / mean_a * 100
        else:
            sd = sd_diff
        ns = [seeds_needed(sd, h) for h in TARGETS[measure]]
        out.append({"cid": cid, "metric": metric, "measure": measure, "mean_a": f"{mean_a:g}",
                    "sd": f"{sd:.2f}", "n_a": ns[0], "n_b": ns[1], "n_c": ns[2],
                    "k": k, "h100": f"{half_width(sd, N_PLAN, k):.2f}"})
    return out


def main() -> int:
    with SRC.open(encoding="utf-8", newline="") as f:
        ci_rows = list(csv.DictReader(f))
    rows = compute(ci_rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        print(f"{r['cid']:<20} {r['metric']:<12} {r['measure']}  sd={r['sd']:>8}  n={r['n_a']}/{r['n_b']}/{r['n_c']}  k={r['k']:>2}  h100={r['h100']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
