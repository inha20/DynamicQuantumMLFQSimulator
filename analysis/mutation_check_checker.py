"""점검기(analysis/check_repo.py)의 시험이 실제로 점검기의 결함을 잡아내는지 확인한다(변이 점검).

통과만 보는 점검기는 헛돌아도 통과한다. 그래서 저장소 사본에서 점검기 소스의 한 곳을 일부러 틀리게 고치고(변이),
점검기를 검증하는 시험(tests/test_check_repo.py)을 돌려 시험이 실패하는지 본다. 시작할 때 변이를 넣지 않은 사본이
시험을 통과하는지 먼저 확인한다. 시험이 실패하면 '잡힘', 모두 통과하면 '살아남음'이다. 살아남은 변이는 (a) 시험이 비어 있는 곳이거나
(b) 결과에 영향이 없는 동등한 변이다. 저장소의 원본 파일은 건드리지 않는다.

    python3 -I -B analysis/mutation_check_checker.py            # 전체 (약 1~2분)
    python3 -I -B analysis/mutation_check_checker.py --only K11,K36

결과는 results/validation/mutation_check_checker.csv에 쓴다. 살아남거나 적용되지 않은 변이가 있으면 종료 코드 1.
점검기 소스를 고치면 아래 변이의 '바꿀 원문'이 달라질 수 있다. 그 변이는 '오류'로 나오니 원문을 맞춰 고친다.

이 점검은 시뮬레이터가 아니라 문서 점검기의 시험을 시험한다. 시뮬레이터의 시험은 analysis/mutation_check.py가 본다.
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
OUT = ROOT / "results" / "validation" / "mutation_check_checker.csv"
SRC = "analysis/check_repo.py"
TEST_FILE = "test_check_repo.py"

# (id, 바꿀 원문(점검기 소스에 정확히 한 번 나와야 함), 바뀐 글, 설명)
MUTANTS = [
    ("K01", "return abs(vn - cn) <= tol + EPS,", "return True,", "판정: 숫자 허용 오차 무시"),
    ("K02", "return (vn is not None and lo - EPS <= vn <= hi + EPS),", "return True,", "판정: 범위 무시"),
    ("K03", '{">": vn > x, "<": vn < x,', '{">": vn < x, "<": vn < x,', "판정: 부등호 방향"),
    ("K04", "return str(v) == c,", "return True,", "판정: 문자열 같음"),
    ("K05", '">": x > y, "<": x < y,', '">": x < y, "<": x > y,', "필터: > 와 < 바꿈"),
    ("K06", "if na is not None and nb is not None:\n        x, y = na, nb", "if False:\n        x, y = na, nb", "필터: 숫자 비교를 문자열로"),
    ("K07", 'if claimed == "inc" else all(y <= x + EPS', 'if claimed == "dec" else all(y <= x + EPS', "series: inc와 dec 바꿈"),
    ("K08", "seq = sorted(a, key=lambda r: float(r[c[\"order\"]]))", "seq = list(a)", "series: 정렬 열 무시"),
    ("K09", "return min(va) > max(vb),", "return max(va) > min(vb),", "gt: 집합 비교를 느슨하게"),
    ("K10", "if len(winners) != 1:", "if False:", "argmin: 동률 허용"),
    ("K11", "if len(vals) < 2 or min(vals) == 0:", "if len(vals) < 2:", "spread: 최솟값 0 허용"),
    ("K12", "return judge(top / base if kind == \"ratio\" else (top / base - 1) * 100, claimed, tol)",
     "return judge(base / top if kind == \"ratio\" else (base / top - 1) * 100, claimed, tol)", "rel: 기준과 비교를 바꿈"),
    ("K13", "if not snippet or snippet not in doc_cache[doc]:", "if False:", "수치: snippet 존재 검사 제거"),
    ("K14", "if (cells is not None and claimed not in cells) or (cells is None and claimed not in snippet):",
     "if claimed not in snippet:", "수치: 표 칸 검사를 부분 문자열로"),
    ("K15", 'fails.append(f"{cid}: 점검을 계산하지 못했다 ({e})")', "pass", "수치: 계산 실패를 삼킴"),
    ("K16", "if cid in seen:", "if False:", "수치: 번호 겹침 검사 제거"),
    ("K17", "if not ok:", "if False:", "수치: 숫자 불일치를 통과"),
    ("K18", "if cid not in referenced:", "if False:", "대장: 고아 점검 검사 제거"),
    ("K19", "if STRENGTH.get(norm_level(level), 99) < STRENGTH.get(weakest, 99):",
     "if STRENGTH.get(norm_level(level), 99) > STRENGTH.get(weakest, 99):", "대장: 수준 강약 방향"),
    ("K20", 'if norm_level(level) in ("C", "D", "X") and use != "단서":', "if False:", "대장: C 이하 단서 규칙 제거"),
    ("K21", "for s in sorted(set(s_rows) - covered):", "for s in []:", "대장: 문헌 미인용 검사 제거"),
    ("K22", "if got_calls != want_calls:", "if False:", "대장: 호출 대조 제거"),
    ("K23", "if cid not in check_ids:", "if False:", "대장: 없는 점검 번호 허용"),
    ("K24", 'if level == "V" and not has_checks:', "if False:", "대장: V에 점검 필수 제거"),
    ("K25", 'if level == "X" and (has_checks or status != "미검증"):', "if False:", "대장: X 규칙 제거"),
    ("K26", 'if status not in ("일치", "고침", "해석", "미검증"):', "if False:", "대장: 상태 값 검사 제거"),
    ("K27", 'if status in ("고침", "해석") and not note:', "if False:", "대장: 메모 필수 제거"),
    ("K28", "for d in sorted({i for i in ids if ids.count(i) > 1}):", "for d in []:", "대장: 번호 겹침 검사 제거"),
    ("K29", "if sc not in sub_claims:", "if False:", "대장: 하위 주장 검사 제거"),
    ("K30", "if s not in s_rows:", "if False:", "대장: 문헌 ID 표 대조 제거"),
    ("K31", "if codes != expected:", "if False:", "검색기록: 번호 연속 검사 제거"),
    ("K32", "if got != tuple(want):", "if False:", "검색기록: 횟수 불일치를 통과"),
    ("K33", "if not found:", "if False:", "검색기록: 문장이 사라져도 통과"),
    ("K34", 'idx = 0 if verdict.startswith("동일") else 1 if verdict.startswith("부분")',
     'idx = 1 if verdict.startswith("동일") else 0 if verdict.startswith("부분")', "검색기록: 판정 집계 세기 바꿈"),
    ("K35", "elif (path in (\"K\", \"A\")) != (tool == \"WebSearch\"):", "elif False:", "검색기록: 경로와 도구 대응 제거"),
    ("K36", "if c not in codes:\n                fails.append(f\"docs/01-prior-art.md: 검색 기록에 없는 호출 코드 {c}\")",
     "if False:\n                fails.append(f\"docs/01-prior-art.md: 검색 기록에 없는 호출 코드 {c}\")", "검색기록: 본문의 로그에 없는 호출 코드 허용"),
    ("K37", "for u in sorted(set(ids) - used):", "for u in []:", "검색기록: 쓰이지 않은 문헌 ID 허용"),
    ("K38", "for u in sorted(used - set(ids)):", "for u in []:", "검색기록: 표에 없는 문헌 ID 허용"),
    ("K39", 'planned = "예정" in line', "planned = False", "경로: 예정 줄을 건너뛰지 않음"),
    ("K40", "if not (f.parent / target).exists():", "if False:", "경로: 링크 검사 제거"),
    ("K41", "if not (root / m.group(1)).exists():\n                    fails.append(f\"{rel}: 실행 예에 적힌 스크립트가 없다",
     "if False:\n                    fails.append(f\"{rel}: 실행 예에 적힌 스크립트가 없다", "경로: 실행 예 검사 제거"),
    ("K42", 'SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}', 'SKIP_DIRS = {"__pycache__"}', "위생: .git 디렉터리도 검사"),
    ("K43", r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+", r"[A-Za-z0-9._%+\-]+#[A-Za-z0-9\-]+", "위생: 이메일 패턴 깨뜨림"),
    ("K44", 'if p.suffix.lower() == ".pdf":', "if False:", "위생: PDF 검사 제거"),
    ("K45", 'r"/" + _U + r"/[A-Za-z0-9._\\-]+/|/home/[A-Za-z0-9._\\-]+/"', 'r"/" + _U + r"/#[A-Za-z0-9._\\-]+/|/home/#[A-Za-z0-9._\\-]+/"', "위생: 개인 경로 패턴 깨뜨림"),
    ("K46", 'out.extend(f"{m.group(1)}{i:02d}" for i in range(lo, hi + 1))', 'out.extend(f"{m.group(1)}{i:02d}" for i in range(lo, hi))', "점검 번호: 범위 끝 빼먹음"),
    ("K47", "if lo > hi:", "if False:", "점검 번호: 거꾸로 된 범위 허용"),
    ("K48", 'for m in re.finditer(r"S(\\d\\d)~S(\\d\\d)", text):', "for m in []:", "문헌 ID: 범위 표기 무시"),
    ("K49", "if tuple(reader.fieldnames or ()) != CHECK_COLUMNS:", "if False:", "점검표 열 검사 제거"),
    ("K50", 'if c["kind"] not in KINDS:', "if False:", "점검 종류 검사 제거"),
]


def make_tree(tmp: Path) -> None:
    shutil.copytree(ROOT, tmp, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))


def run_tests(tmp: Path, timeout):
    return subprocess.run([sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", "tests", "-p", TEST_FILE],
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
    mid, old, new, desc = m
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        make_tree(tmp)
        f = tmp / SRC
        text = f.read_text(encoding="utf-8")
        if text.count(old) != 1:
            return mid, SRC, desc, "오류", f"원문이 {text.count(old)}번 나온다", ""
        f.write_text(text.replace(old, new), encoding="utf-8")
        try:
            p = run_tests(tmp, timeout)
        except subprocess.TimeoutExpired:
            return mid, SRC, desc, "잡힘", "시간 초과", ""
        if p.returncode == 0:
            return mid, SRC, desc, "살아남음", "", ""
        names = [line.split(" (")[0].split(": ", 1)[1] for line in p.stderr.splitlines()
                 if line.startswith(("FAIL:", "ERROR:"))]
        return mid, SRC, desc, "잡힘", (names[0] if names else ""), ";".join(names)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="쉼표로 구분한 변이 번호")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--out", default=None, help="CSV 경로. --only를 쓰면 이 값을 줄 때만 파일을 쓴다")
    ap.add_argument("--jobs", type=int, default=2)
    a = ap.parse_args(argv)
    want = {x for x in a.only.split(",") if x}
    unknown = want - {m[0] for m in MUTANTS}
    if unknown:
        print("알 수 없는 변이 번호:", ", ".join(sorted(unknown)), file=sys.stderr)
        return 2
    muts = [m for m in MUTANTS if not want or m[0] in want]
    if not baseline(a.timeout):
        return 2
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        rows = list(ex.map(lambda m: run_one(m, a.timeout), muts))
    for mid, rel, desc, status, note, _all in rows:
        print(f"{mid} {status:<4} {desc}" + (f"  [{note}]" if note else ""), flush=True)
    if a.out or not want:
        out = Path(a.out) if a.out else OUT
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["id", "file", "description", "status", "first_failing_test", "failing_tests"])
            w.writerows(rows)
    killed = sum(r[3] == "잡힘" for r in rows)
    bad = [r for r in rows if r[3] != "잡힘"]
    print(f"변이 {len(rows)}개 중 잡힘 {killed}, 살아남음/오류 {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
