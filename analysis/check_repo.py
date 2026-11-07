"""저장소 점검기: 문서의 수치와 결과 CSV의 대조, 주장 대장, 검색 기록, 경로, 위생.

    python analysis/check_repo.py                 # 모든 규칙
    python analysis/check_repo.py --only 수치,위생   # 일부 규칙만
    python analysis/check_repo.py --root 경로        # 다른 위치의 사본을 점검

저장소의 파일은 읽기만 한다. 실패가 하나라도 있으면 종료 코드 1이다.

규칙
- 경로: README와 docs/, refs/의 마크다운 링크와 `경로` 표기가 가리키는 파일이 있다('예정'이 적힌 줄은 건너뛴다).
- 검색기록: refs/g4-search-log.md의 번호가 빠짐없이 이어지고 docs/01-prior-art.md의 호출 코드, 문헌 ID, 판정 집계, 횟수 표기가 맞는다.
- 대장: refs/claims-ledger.md의 형식, 점검 번호, 문헌 수준 규칙(대장의 수준은 인용한 문헌 중 가장 약한 수준보다 강할 수 없고, C 이하는 단서로만 쓴다).
- 수치: refs/claims-checks.csv의 점검 하나하나를 결과 CSV와 소스에 대해 계산해 문서의 숫자와 맞춘다.
- 위생: 이메일, 개인 경로, 비밀값 모양의 문자열, PDF가 저장소 파일에 없다.

이 점검기는 문서의 숫자가 결과 파일과 같은지만 본다. 결과 파일이 코드에서 다시 만들어지는지는 analysis/check_reproduce.py가 본다.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MINUS = "−"
LEVELS = ("A", "A" + MINUS, "B", "C", "D", "X", "V")
# 작을수록 강한 근거다. V는 문헌 수준이 아니므로 순서에 넣지 않는다.
STRENGTH = {"A": 0, "A" + MINUS: 1, "B": 2, "C": 3, "D": 4, "X": 5}
EPS = 1e-9
KINDS = ("value", "rel", "ratio", "colrel", "colratio", "min", "max", "argmin", "argmax", "series", "spread", "gt", "maxabs", "count", "grep")
# 문서에 숫자를 그대로 옮겨 적은 점검 종류. 이 종류의 claimed는 snippet 안에 있어야 한다(범위와 부등호 표기는 제외).
QUOTED_KINDS = ("value", "rel", "ratio", "colrel", "colratio", "min", "max", "maxabs", "count", "grep")
CHECK_COLUMNS = ("check_id", "kind", "file", "filter", "filter_b", "column", "order", "claimed", "tol", "doc", "snippet")

# 개인 경로나 비밀값 모양의 문자열은 이 파일 자신이 걸리지 않도록 조각을 이어서 만든다.
_U = "Us" + "ers"
_BEGIN = "-----" + "BEGIN "
HYGIENE_PATTERNS = (
    ("이메일", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")),
    ("윈도 사용자 경로", re.compile(r"[A-Za-z]:\\+" + _U + r"\\+", re.I)),
    ("사용자 홈 경로", re.compile(r"/" + _U + r"/[A-Za-z0-9._\-]+/|/home/[A-Za-z0-9._\-]+/")),
    ("비밀값 모양", re.compile(r"(?i)\b(?:api[_-]?key|secret|passwd|password|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}")),
    ("비밀값 모양", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b|\bsk-[A-Za-z0-9]{20,}\b|\bAKIA[0-9A-Z]{16}\b")),
    ("비밀 키 머리말", re.compile(re.escape(_BEGIN) + r"[A-Z ]*PRIVATE KEY")),
)
TEXT_SUFFIXES = {".md", ".py", ".csv", ".txt", ".json", ".toml", ".yml", ".yaml", ".cfg", ".ini"}
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}


# ---------------------------------------------------------------- 공통

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def table_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def norm_level(s: str) -> str:
    return s.strip().replace("-", MINUS)


def expand_ids(cell: str) -> list[str]:
    """'C-E1T-01..16, C-E2F-01'처럼 적은 점검 번호를 하나씩 펼친다. 범위는 접두사가 같고 두 자리 번호여야 한다."""
    out: list[str] = []
    for part in [p.strip() for p in cell.split(",") if p.strip()]:
        m = re.fullmatch(r"(.+?)(\d\d)\.\.(\d\d)", part)
        if m:
            lo, hi = int(m.group(2)), int(m.group(3))
            if lo > hi:
                raise ValueError(f"범위가 거꾸로다: {part}")
            out.extend(f"{m.group(1)}{i:02d}" for i in range(lo, hi + 1))
        else:
            out.append(part)
    return out


def md_files(root: Path) -> list[Path]:
    files = [root / "README.md"]
    for sub in ("docs", "refs"):
        d = root / sub
        if d.is_dir():
            files.extend(sorted(d.glob("*.md")))
    return [f for f in files if f.exists()]


def s_ids_used(text: str) -> set[str]:
    """본문에 쓰인 문헌 ID. 'S23~S27'은 사이의 번호를 모두 쓴 것으로 센다."""
    used = set(re.findall(r"(?<![A-Za-z0-9])S(\d\d)(?!\d)", text))
    for m in re.finditer(r"S(\d\d)~S(\d\d)", text):
        used.update(f"{i:02d}" for i in range(int(m.group(1)), int(m.group(2)) + 1))
    return {"S" + n for n in used}


# ---------------------------------------------------------------- 규칙: 경로

PATH_TOKEN = re.compile(r"`((?:analysis|docs|refs|results|tests|mlfqsim)/[A-Za-z0-9_./\-]*)`")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"```.*?```", re.S)
PY_CALL = re.compile(r"python\S*\s+(?:-\S+\s+)*([A-Za-z0-9_./\-]+\.py)")


def rule_paths(root: Path) -> tuple[list[str], int]:
    fails: list[str] = []
    n = 0
    for f in md_files(root):
        raw = read_text(f)
        rel = f.relative_to(root).as_posix()
        for block in FENCE.findall(raw):
            for m in PY_CALL.finditer(block):
                n += 1
                if not (root / m.group(1)).exists():
                    fails.append(f"{rel}: 실행 예에 적힌 스크립트가 없다 {m.group(1)}")
        text = FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), raw)
        for no, line in enumerate(text.split("\n"), 1):
            planned = "예정" in line
            for m in LINK.finditer(line):
                target = m.group(1)
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                target = target.split("#")[0]
                if not target or planned:
                    continue
                n += 1
                if not (f.parent / target).exists():
                    fails.append(f"{rel}:{no}: 링크가 가리키는 파일이 없다 {target}")
            for m in PATH_TOKEN.finditer(line):
                if planned:
                    continue
                n += 1
                if not (root / m.group(1)).exists():
                    fails.append(f"{rel}:{no}: 경로가 없다 {m.group(1)}")
    return fails, n


# ---------------------------------------------------------------- 규칙: 검색기록

def parse_log(root: Path) -> list[list[str]]:
    path = root / "refs" / "g4-search-log.md"
    rows = []
    for line in read_text(path).split("\n"):
        if line.startswith("| G4-"):
            rows.append(table_cells(line))
    return rows


def parse_prior_art_s_table(text: str) -> list[list[str]]:
    if "## 6." not in text:
        return []
    sec = text.split("## 6.", 1)[1]
    return [table_cells(l) for l in sec.split("\n") if re.match(r"\| S\d\d \|", l)]


def verdict_counts(text: str) -> dict[str, list[int]]:
    """docs/01의 하위 주장 판정표(2.2, 3.2, 4.2)에서 RQ별 [동일, 부분, 찾지 못함] 개수를 센다."""
    counts = {"A": [0, 0, 0], "B": [0, 0, 0], "C": [0, 0, 0]}
    for sec_head in ("### 2.2", "### 3.2", "### 4.2"):
        if sec_head not in text:
            continue
        body = text.split(sec_head, 1)[1].split("\n### ", 1)[0]
        for line in body.split("\n"):
            m = re.match(r"\| ([ABC])\d \|", line)
            if not m:
                continue
            cells = table_cells(line)
            if len(cells) < 4:
                continue
            verdict = cells[3]
            idx = 0 if verdict.startswith("동일") else 1 if verdict.startswith("부분") else 2 if verdict.startswith("찾지 못함") else None
            if idx is not None:
                counts[m.group(1)][idx] += 1
    return counts


def rule_search_log(root: Path) -> tuple[list[str], int]:
    fails: list[str] = []
    log_path = root / "refs" / "g4-search-log.md"
    doc_path = root / "docs" / "01-prior-art.md"
    if not log_path.exists():
        return ["refs/g4-search-log.md가 없다"], 0
    rows = parse_log(root)
    n = len(rows)
    codes = [r[0] for r in rows]
    expected = [f"G4-{i:03d}" for i in range(1, n + 1)]
    if codes != expected:
        dup = sorted({c for c in codes if codes.count(c) > 1})
        missing = [c for c in expected if c not in codes]
        fails.append(f"호출 번호가 G4-001부터 빠짐없이 이어지지 않는다(중복 {dup}, 빠짐 {missing})")
    searches = fetches = extended = 0
    for r in rows:
        if len(r) != 6:
            fails.append(f"{r[0]}: 열이 6개가 아니다({len(r)}개)")
            continue
        code, path, tool, query, result, rq = r
        if path not in ("K", "C", "A", "R"):
            fails.append(f"{code}: 경로 표기가 K, C, A, R이 아니다 '{path}'")
        if tool not in ("WebSearch", "WebFetch"):
            fails.append(f"{code}: 도구가 WebSearch, WebFetch가 아니다 '{tool}'")
        elif (path in ("K", "A")) != (tool == "WebSearch"):
            fails.append(f"{code}: 경로 {path}와 도구 {tool}이 맞지 않는다")
        if not query or not result:
            fails.append(f"{code}: 질의나 결과가 비어 있다")
        if not re.fullmatch(r"[ABC](, [ABC])*", rq):
            fails.append(f"{code}: RQ 열이 A, B, C의 목록이 아니다 '{rq}'")
        if tool == "WebSearch":
            searches += 1
            extended += "extended 모드" in query
        elif tool == "WebFetch":
            fetches += 1
    standard = searches - extended
    tot = None
    declared = {
        "refs/g4-search-log.md": (read_text(log_path), [(r"표준 모드 (\d+)회, 확장 모드 (\d+)회", (standard, extended))]),
    }
    if doc_path.exists():
        doc = read_text(doc_path)
        declared["docs/01-prior-art.md"] = (doc, [
            (r"검색과 열람은 (\d+)회다", (n,)),
            (r"표준 모드 (\d+)회, 확장 모드 (\d+)회", (standard, extended)),
        ])
        # 호출 코드
        for c in sorted(set(re.findall(r"G4-\d{3}", doc))):
            if c not in codes:
                fails.append(f"docs/01-prior-art.md: 검색 기록에 없는 호출 코드 {c}")
        # 문헌 ID
        rows_s = parse_prior_art_s_table(doc)
        ids = [r[0] for r in rows_s]
        if ids != [f"S{i:02d}" for i in range(1, len(ids) + 1)] or not ids:
            fails.append(f"docs/01-prior-art.md §6: 문헌 ID가 S01부터 빠짐없이 이어지지 않는다 {ids}")
        for r in rows_s:
            if len(r) != 4:
                fails.append(f"docs/01-prior-art.md §6: {r[0]} 행의 열이 4개가 아니다")
                continue
            if norm_level(r[2]) not in LEVELS or norm_level(r[2]) == "V":
                fails.append(f"docs/01-prior-art.md §6: {r[0]}의 수준이 문헌 수준이 아니다 '{r[2]}'")
            for c in re.findall(r"G4-\d{3}", r[3]):
                if c not in codes:
                    fails.append(f"docs/01-prior-art.md §6: {r[0]}의 호출 {c}가 검색 기록에 없다")
        body = doc.split("## 6.", 1)[0]
        used = s_ids_used(body)
        for u in sorted(used - set(ids)):
            fails.append(f"docs/01-prior-art.md: §6에 없는 문헌 ID {u}")
        for u in sorted(set(ids) - used):
            fails.append(f"docs/01-prior-art.md: 본문에서 한 번도 쓰이지 않은 문헌 ID {u}")
        # 판정 집계(§5.1)와 §0 문장
        counts = verdict_counts(doc)
        tot = (sum(sum(v) for v in counts.values()), sum(v[0] for v in counts.values()),
               sum(v[1] for v in counts.values()), sum(v[2] for v in counts.values()))
        declared["docs/01-prior-art.md"][1].append((r"\| 합계 \| (\d+) \| (\d+) \| (\d+) \| (\d+) \|", tot))
        for rq, name in (("A", "RQ-A"), ("B", "RQ-B"), ("C", "RQ-C")):
            total = sum(counts[rq])
            m = re.search(r"\| " + name + r" \| (\d+) \| (\d+)\(.*?\) \| (\d+)(?:\(.*?\))? \| (\d+)(?:\(.*?\))? \|", doc)
            if not m:
                fails.append(f"docs/01-prior-art.md §5.1: {name} 행을 읽지 못했다")
            elif [int(m.group(i)) for i in range(1, 5)] != [total] + counts[rq]:
                fails.append(f"docs/01-prior-art.md §5.1: {name} 집계 {[int(m.group(i)) for i in range(1, 5)]}가 판정표에서 센 값 {[total] + counts[rq]}과 다르다")
            row0 = re.search(r"^\| " + name + r" [^|\n]+\|.*$", doc, re.M)
            m_tot = re.search(r"하위 주장 (\d+)개 중", row0.group(0)) if row0 else None
            if not row0 or not m_tot:
                fails.append(f"docs/01-prior-art.md §0: {name} 요약 문장을 읽지 못했다")
            else:
                def _n(word: str) -> int:
                    mm = re.search(r"(\d+)개 " + word, row0.group(0))
                    return int(mm.group(1)) if mm else 0
                got = [int(m_tot.group(1)), _n("동일"), _n("부분"), _n("찾지 못함")]
                if got != [total] + counts[rq]:
                    fails.append(f"docs/01-prior-art.md §0: {name} 요약 {got}가 판정표에서 센 값 {[total] + counts[rq]}과 다르다")
    verdict = [(r"하위 주장 (\d+)개를 판정했다: 동일 (\d+), 부분 (\d+), 찾지 못함 (\d+)", tot)] if tot else []
    for rel, pats in (("README.md", [(r"검색·열람 (\d+)회", (n,))] + verdict),
                      ("docs/00-progress-log.md", [(r"검색·열람 (\d+)회", (n,)), (r"웹 검색 (\d+)회, 열람 (\d+)회", (searches, fetches))] + verdict)):
        p = root / rel
        if p.exists():
            declared[rel] = (read_text(p), pats)
    for rel, (text, pats) in declared.items():
        for pat, want in pats:
            found = list(re.finditer(pat, text))
            if not found:
                fails.append(f"{rel}: 횟수나 판정 집계를 적은 문장({pat})을 찾지 못했다")
            for m in found:
                got = tuple(int(g) for g in m.groups())
                if got != tuple(want):
                    fails.append(f"{rel}: '{m.group(0)}'가 검색 기록에서 센 값 {tuple(want)}과 다르다")
    return fails, n


# ---------------------------------------------------------------- 규칙: 수치

TERM = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(>=|<=|!=|\^=|=|>|<)\s*(.*?)\s*$")
RANGE = re.compile(r"^(-?\d+(?:\.\d+)?)\.\.(-?\d+(?:\.\d+)?)$")
COMPARE = re.compile(r"^(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)$")


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _cmp(a, b, op: str) -> bool:
    """필터의 비교. 둘 다 숫자면 숫자로, 아니면 문자열로 비교한다(문자열은 =, !=, ^=만 가능)."""
    if op == "^=":
        return str(a).startswith(str(b))
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        x, y = na, nb
    elif op in ("=", "!="):
        x, y = str(a), str(b)
    else:
        return False
    return {"=": x == y, "!=": x != y, ">": x > y, "<": x < y, ">=": x >= y, "<=": x <= y}[op]


def parse_filter(s: str) -> list[tuple[str, str, str]]:
    terms = []
    for t in s.split(";"):
        if not t.strip():
            continue
        m = TERM.match(t)
        if not m:
            raise ValueError(f"필터 형식 오류 {t!r}")
        terms.append(m.groups())
    return terms


def select(rows: list[dict], filt: str) -> list[dict]:
    terms = parse_filter(filt)
    if rows:
        for col, _, _ in terms:
            if col not in rows[0]:
                raise ValueError(f"결과 파일에 열이 없다 '{col}'")
    return [r for r in rows if all(_cmp(r.get(c, ""), v, op) for c, op, v in terms)]


def judge(v, claimed: str, tol: float) -> tuple[bool, str]:
    c = claimed.strip()
    vn = _num(v)
    shown = f"{vn:.6g}" if vn is not None else str(v)
    m = RANGE.match(c)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (vn is not None and lo - EPS <= vn <= hi + EPS), f"계산값 {shown}, 주장 범위 {lo:g}~{hi:g}"
    m = COMPARE.match(c)
    if m:
        x = float(m.group(2))
        ok = vn is not None and {">": vn > x, "<": vn < x, ">=": vn >= x - EPS, "<=": vn <= x + EPS}[m.group(1)]
        return ok, f"계산값 {shown}, 주장 {c}"
    cn = _num(c)
    if cn is not None and vn is not None:
        return abs(vn - cn) <= tol + EPS, f"계산값 {shown}, 주장 {c}(허용 오차 {tol:g})"
    return str(v) == c, f"계산값 {shown}, 주장 {c}"


def load_csv(root: Path, rel: str, cache: dict) -> list[dict]:
    if rel not in cache:
        p = root / rel
        if not p.exists():
            raise ValueError(f"파일이 없다 {rel}")
        with p.open(encoding="utf-8", newline="") as f:
            cache[rel] = list(csv.DictReader(f))
    return cache[rel]


def _col(rows: list[dict], col: str) -> list[float]:
    vals = []
    for r in rows:
        if col not in r:
            raise ValueError(f"결과 파일에 열이 없다 '{col}'")
        x = _num(r[col])
        if x is not None:
            vals.append(x)
    return vals


def _one(rows: list[dict], what: str) -> dict:
    if len(rows) != 1:
        raise ValueError(f"{what}에 맞는 행이 {len(rows)}개다(1개여야 한다)")
    return rows[0]


def run_check(root: Path, c: dict, cache: dict) -> tuple[bool, str]:
    kind, claimed = c["kind"], c["claimed"]
    tol = _num(c["tol"]) if c["tol"] != "" else 0.0
    if tol is None:
        raise ValueError(f"tol이 숫자가 아니다 '{c['tol']}'")
    if kind == "grep":
        text = read_text(root / c["file"])
        v = len(re.findall(c["filter"], text, re.M))
        return judge(v, claimed, 0.0)
    rows = load_csv(root, c["file"], cache)
    a = select(rows, c["filter"])
    col = c["column"]
    if kind == "count":
        return judge(len(a), claimed, tol)
    if kind in ("min", "max"):
        vals = _col(a, col)
        if not vals:
            raise ValueError("맞는 행이 없다")
        return judge(min(vals) if kind == "min" else max(vals), claimed, tol)
    if kind == "maxabs":
        vals = _col(a, col)
        if not vals:
            raise ValueError("맞는 행이 없다")
        return judge(max(abs(x) for x in vals), claimed, tol)
    if kind == "spread":
        vals = _col(a, col)
        if len(vals) < 2 or min(vals) == 0:
            raise ValueError("행이 2개 미만이거나 최솟값이 0이다")
        return judge((max(vals) - min(vals)) / abs(min(vals)), claimed, tol)
    if kind in ("argmin", "argmax"):
        vals = [(float(r[col]), r) for r in a if _num(r.get(col)) is not None]
        if not vals:
            raise ValueError("맞는 행이 없다")
        best = min(v for v, _ in vals) if kind == "argmin" else max(v for v, _ in vals)
        winners = [r for v, r in vals if v == best]
        if len(winners) != 1:
            raise ValueError(f"극값 행이 {len(winners)}개다(1개여야 한다)")
        return judge(winners[0][c["order"]], claimed, tol)
    if kind == "series":
        if claimed not in ("inc", "dec"):
            raise ValueError("series의 claimed는 inc 또는 dec이다")
        if len(a) < 2:
            raise ValueError("행이 2개 미만이다")
        seq = sorted(a, key=lambda r: float(r[c["order"]]))
        vals = [float(r[col]) for r in seq]
        pairs = list(zip(vals, vals[1:]))
        ok = all(y >= x - EPS for x, y in pairs) if claimed == "inc" else all(y <= x + EPS for x, y in pairs)
        return ok, "값 " + ", ".join(f"{x:.6g}" for x in vals) + f"(정렬 열 {c['order']}), 주장 {claimed}"
    if kind == "gt":
        b = select(rows, c["filter_b"])
        va, vb = _col(a, col), _col(b, col)
        if not va or not vb:
            raise ValueError("두 집합 모두 행이 있어야 한다")
        return min(va) > max(vb), f"앞 집합 최솟값 {min(va):.6g}, 뒤 집합 최댓값 {max(vb):.6g}"
    if kind in ("colrel", "colratio"):
        num, den = col.split("/")
        r = _one(a, "필터")
        q = float(r[num]) / float(r[den])
        return judge((q - 1) * 100 if kind == "colrel" else q, claimed, tol)
    ra = _one(a, "필터")
    rb = _one(select(rows, c["filter_b"]), "필터 b") if kind in ("rel", "ratio") else None
    if kind == "value":
        return judge(float(ra[col]), claimed, tol)
    base = float(rb[col]) if kind == "ratio" else float(ra[col])
    top = float(ra[col]) if kind == "ratio" else float(rb[col])
    if base == 0:
        raise ValueError("나누는 값이 0이다")
    return judge(top / base if kind == "ratio" else (top / base - 1) * 100, claimed, tol)


def load_checks(root: Path) -> list[dict]:
    p = root / "refs" / "claims-checks.csv"
    if not p.exists():
        raise ValueError("refs/claims-checks.csv가 없다")
    with p.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if tuple(reader.fieldnames or ()) != CHECK_COLUMNS:
            raise ValueError(f"claims-checks.csv의 열이 {CHECK_COLUMNS}가 아니다")
        return list(reader)


def rule_numbers(root: Path) -> tuple[list[str], int]:
    fails: list[str] = []
    try:
        checks = load_checks(root)
    except ValueError as e:
        return [str(e)], 0
    cache: dict = {}
    seen: set[str] = set()
    doc_cache: dict[str, str] = {}
    for c in checks:
        cid = c["check_id"]
        if cid in seen:
            fails.append(f"{cid}: 점검 번호가 겹친다")
        seen.add(cid)
        if c["kind"] not in KINDS:
            fails.append(f"{cid}: 알 수 없는 종류 '{c['kind']}'")
            continue
        try:
            ok, msg = run_check(root, c, cache)
        except (ValueError, KeyError, ZeroDivisionError, re.error, OSError) as e:
            fails.append(f"{cid}: 점검을 계산하지 못했다 ({e})")
            continue
        if not ok:
            fails.append(f"{cid}: 문서의 숫자와 결과가 다르다 [{c['doc']}: {c['snippet']}] {msg}")
        # 문서와의 연결: 문서에 그 문구가 있고, 숫자 주장은 그 문구 안에 있다.
        doc = c["doc"]
        if doc not in doc_cache:
            p = root / doc
            doc_cache[doc] = read_text(p) if p.exists() else ""
        snippet, claimed = c["snippet"], c["claimed"].strip()
        if not snippet or snippet not in doc_cache[doc]:
            fails.append(f"{cid}: 문서 {doc}에 이 문구가 없다 '{snippet}'")
        elif c["kind"] in QUOTED_KINDS and _num(claimed) is not None:
            cells = table_cells(snippet) if snippet.startswith("|") else None
            if (cells is not None and claimed not in cells) or (cells is None and claimed not in snippet):
                fails.append(f"{cid}: 주장한 숫자 {claimed}가 문구 안에 없다 '{snippet}'")
    return fails, len(checks)


# ---------------------------------------------------------------- 규칙: 대장

R_ROW = re.compile(r"^\| (R\d\d) \|")
L_ROW = re.compile(r"^\| (L\d\d) \|")


def rule_ledger(root: Path) -> tuple[list[str], int]:
    fails: list[str] = []
    p = root / "refs" / "claims-ledger.md"
    if not p.exists():
        return ["refs/claims-ledger.md가 없다"], 0
    text = read_text(p)
    try:
        check_ids = [c["check_id"] for c in load_checks(root)]
    except ValueError as e:
        return [str(e)], 0
    doc_path = root / "docs" / "01-prior-art.md"
    doc = read_text(doc_path) if doc_path.exists() else ""
    s_rows = {r[0]: r for r in parse_prior_art_s_table(doc)}
    log_codes = {r[0] for r in parse_log(root)} if (root / "refs" / "g4-search-log.md").exists() else set()
    sub_claims = set(re.findall(r"^\| ([ABC]\d) \|", doc, re.M))
    r_rows = [table_cells(l) for l in text.split("\n") if R_ROW.match(l)]
    l_rows = [table_cells(l) for l in text.split("\n") if L_ROW.match(l)]
    n = len(r_rows) + len(l_rows)
    if not r_rows:
        fails.append("자체 결과 주장(R) 행이 없다")
    if not l_rows:
        fails.append("문헌 주장(L) 행이 없다")
    ids = [r[0] for r in r_rows] + [r[0] for r in l_rows]
    for d in sorted({i for i in ids if ids.count(i) > 1}):
        fails.append(f"{d}: 대장 번호가 겹친다")
    referenced: set[str] = set()
    covered: set[str] = set()
    for r in r_rows:
        rid = r[0]
        if len(r) != 8:
            fails.append(f"{rid}: 열이 8개가 아니다({len(r)}개)")
            continue
        _, claim, where, scope, checks, status, level, note = r
        if not all([claim, where, scope, status, level]):
            fails.append(f"{rid}: 비어 있는 칸이 있다")
        if status not in ("일치", "고침", "해석", "미검증"):
            fails.append(f"{rid}: 상태가 일치, 고침, 해석, 미검증이 아니다 '{status}'")
        if status in ("고침", "해석") and not note:
            fails.append(f"{rid}: 상태가 {status}이면 메모(고치기 전 문장이나 해석 기준)가 있어야 한다")
        if level not in ("V", "X"):
            fails.append(f"{rid}: 자체 결과 주장의 수준이 V나 X가 아니다 '{level}'")
        has_checks = checks not in ("", "없음")
        if level == "V" and not has_checks:
            fails.append(f"{rid}: 수준 V인데 점검이 없다")
        if level == "X" and (has_checks or status != "미검증"):
            fails.append(f"{rid}: 수준 X는 점검이 없고 상태가 미검증이어야 한다")
        if status == "미검증" and level != "X":
            fails.append(f"{rid}: 미검증은 수준이 X여야 한다")
        if has_checks:
            try:
                for cid in expand_ids(checks):
                    referenced.add(cid)
                    if cid not in check_ids:
                        fails.append(f"{rid}: 점검 {cid}가 claims-checks.csv에 없다")
            except ValueError as e:
                fails.append(f"{rid}: {e}")
        if re.search(r"(?<![A-Za-z0-9])S\d\d(?!\d)|G4-\d{3}", " ".join(r[1:])):
            fails.append(f"{rid}: 자체 결과 주장에 문헌 ID나 호출 코드가 섞였다")
    for cid in check_ids:
        if cid not in referenced:
            fails.append(f"{cid}: 어느 대장 행도 이 점검을 가리키지 않는다")
    for r in l_rows:
        lid = r[0]
        if len(r) != 7:
            fails.append(f"{lid}: 열이 7개가 아니다({len(r)}개)")
            continue
        _, claim, sids, calls, level, use, subs = r
        if not all([claim, sids, calls, level, use, subs]):
            fails.append(f"{lid}: 비어 있는 칸이 있다")
        cited = [s.strip() for s in sids.split(",") if s.strip()]
        for s in cited:
            if s not in s_rows:
                fails.append(f"{lid}: 문헌 {s}가 docs/01-prior-art.md §6에 없다")
        known = [s for s in cited if s in s_rows]
        covered.update(known)
        if known:
            weakest = max((norm_level(s_rows[s][2]) for s in known), key=lambda x: STRENGTH.get(x, 99))
            if STRENGTH.get(norm_level(level), 99) < STRENGTH.get(weakest, 99):
                fails.append(f"{lid}: 수준 {level}이 인용한 문헌 중 가장 약한 수준 {weakest}보다 강하다(더 약하게 적는 것은 된다)")
            want_calls = set()
            for s in known:
                want_calls.update(re.findall(r"G4-\d{3}", s_rows[s][3]))
            got_calls = set(re.findall(r"G4-\d{3}", calls))
            if got_calls != want_calls:
                fails.append(f"{lid}: 호출 {sorted(got_calls)}가 인용한 문헌의 호출 {sorted(want_calls)}와 다르다")
        for c in re.findall(r"G4-\d{3}", calls):
            if c not in log_codes:
                fails.append(f"{lid}: 호출 {c}가 검색 기록에 없다")
        if norm_level(level) not in LEVELS or norm_level(level) == "V":
            fails.append(f"{lid}: 문헌 주장의 수준이 문헌 수준이 아니다 '{level}'")
        if use not in ("전제", "단서"):
            fails.append(f"{lid}: 쓰임이 전제, 단서가 아니다 '{use}'")
        if norm_level(level) in ("C", "D", "X") and use != "단서":
            fails.append(f"{lid}: 수준 {level}은 단서로만 쓴다(전제로 쓸 수 없다)")
        if subs == "없음":
            if use != "단서":
                fails.append(f"{lid}: 하위 주장이 없음이면 쓰임은 단서여야 한다")
        else:
            for sc in [x.strip() for x in subs.split(",") if x.strip()]:
                if sc not in sub_claims:
                    fails.append(f"{lid}: 하위 주장 {sc}가 docs/01-prior-art.md에 없다")
        if re.search(r"(?<![A-Za-z0-9])R\d\d(?!\d)|C-[A-Z0-9]+-\d\d", " ".join(r[1:])):
            fails.append(f"{lid}: 문헌 주장에 자체 결과 번호나 점검 번호가 섞였다")
    for s in sorted(set(s_rows) - covered):
        fails.append(f"{s}: 문헌 ID 표(§6)에 있으나 어느 문헌 주장(L) 행도 인용하지 않는다")
    return fails, n


# ---------------------------------------------------------------- 규칙: 위생

def rule_hygiene(root: Path) -> tuple[list[str], int]:
    fails: list[str] = []
    n = 0
    for p in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts) or not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if p.suffix.lower() == ".pdf":
            fails.append(f"{rel}: PDF 파일이 저장소에 있다")
            continue
        if p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        n += 1
        try:
            text = read_text(p)
        except UnicodeDecodeError:
            fails.append(f"{rel}: UTF-8로 읽히지 않는다")
            continue
        for no, line in enumerate(text.split("\n"), 1):
            for name, pat in HYGIENE_PATTERNS:
                if pat.search(line):
                    fails.append(f"{rel}:{no}: {name}")
    return fails, n


RULES = {
    "경로": rule_paths,
    "검색기록": rule_search_log,
    "대장": rule_ledger,
    "수치": rule_numbers,
    "위생": rule_hygiene,
}


def run_all(root: Path, only: list[str] | None = None) -> dict[str, tuple[list[str], int]]:
    return {name: fn(root) for name, fn in RULES.items() if not only or name in only}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--only", default="", help="쉼표로 구분한 규칙 이름: " + ", ".join(RULES))
    a = ap.parse_args(argv)
    only = [x.strip() for x in a.only.split(",") if x.strip()]
    bad = [x for x in only if x not in RULES]
    if bad:
        print(f"알 수 없는 규칙: {bad}. 규칙은 {list(RULES)}", file=sys.stderr)
        return 2
    results = run_all(a.root.resolve(), only)
    total = 0
    for name, (fails, n) in results.items():
        print(f"[{'통과' if not fails else '실패'}] {name}: 확인 {n}개, 실패 {len(fails)}개")
        for f in fails[:50]:
            print("   -", f)
        if len(fails) > 50:
            print(f"   ... 외 {len(fails) - 50}개")
        total += len(fails)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
