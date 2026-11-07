"""analysis/check_repo.py(문서-결과 점검기)의 시험.

통과만 보는 점검기는 헛돌아도 통과한다. 그래서 (1) 실제 저장소가 통과하는 기준선을 먼저 보고,
(2) 저장소 사본에서 결과 파일, 문서, 점검표, 대장, 검색 기록을 하나씩 일부러 깨뜨려 점검기가 그 깨뜨림을 가리키는 메시지로 실패하는지 본다.
이메일, 개인 경로, 비밀값 모양의 문자열은 이 파일이 위생 점검에 걸리지 않도록 조각을 이어서 만든다.
"""
import contextlib
import csv
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))

import check_repo as cr  # noqa: E402

MINUS = "−"


class RepoCopy(unittest.TestCase):
    """저장소 사본 하나를 클래스 안에서 공유하고, 깨뜨린 파일은 시험마다 원래대로 되돌린다."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "repo"
        shutil.copytree(ROOT, cls.root, ignore=shutil.ignore_patterns(".git", "__pycache__"))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @contextlib.contextmanager
    def text_edit(self, rel, old, new, count=1):
        path = self.root / rel
        original = path.read_bytes()
        text = original.decode("utf-8")
        self.assertEqual(text.count(old), count, f"{rel}에 바꿀 문구가 {count}번 있어야 한다: {old!r}")
        path.write_bytes(text.replace(old, new).encode("utf-8"))
        try:
            yield
        finally:
            path.write_bytes(original)

    @contextlib.contextmanager
    def csv_edit(self, rel, match, **changes):
        """match의 열 값이 모두 같은 행(정확히 1개)의 열 값을 바꾼다."""
        path = self.root / rel
        original = path.read_bytes()
        with path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            names = reader.fieldnames
            rows = list(reader)
        hit = [r for r in rows if all(r[k] == v for k, v in match.items())]
        self.assertEqual(len(hit), 1, f"{rel}에서 {match}에 맞는 행이 1개여야 한다({len(hit)}개)")
        hit[0].update(changes)
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=names, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        try:
            yield
        finally:
            path.write_bytes(original)

    @contextlib.contextmanager
    def added_file(self, rel, content=b""):
        path = self.root / rel
        self.assertFalse(path.exists())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        try:
            yield
        finally:
            path.unlink()

    def fails(self, rule):
        return cr.RULES[rule](self.root)[0]

    def assert_fails_with(self, rule, needle):
        fails = self.fails(rule)
        self.assertTrue(any(needle in f for f in fails), f"{needle!r}를 가리키는 실패가 없다. 실패 {len(fails)}개: {fails[:4]}")

    def assert_clean(self, rule):
        self.assertEqual(self.fails(rule), [], f"{rule} 규칙의 기준선이 깨끗하지 않다")


class Baseline(RepoCopy):
    def test_real_repo_passes_every_rule(self):
        results = cr.run_all(self.root)
        self.assertEqual(set(results), set(cr.RULES))
        for name, (fails, n) in results.items():
            self.assertEqual(fails, [], f"{name}: {fails[:3]}")
            self.assertGreater(n, 0, f"{name}: 확인한 것이 하나도 없다(헛도는 점검)")
        self.assertGreaterEqual(results["수치"][1], 250)
        self.assertGreaterEqual(results["대장"][1], 70)
        self.assertGreaterEqual(results["검색기록"][1], 74)

    def test_cli_exit_codes(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cr.main(["--root", str(self.root)]), 0)
            self.assertIn("[통과] 수치", out.getvalue())
            with self.text_edit("docs/01-prior-art.md", "검색과 열람은 74회다", "검색과 열람은 73회다"):
                self.assertEqual(cr.main(["--root", str(self.root)]), 1)
            self.assertEqual(cr.main(["--root", str(self.root), "--only", "없는규칙"]), 2)
            self.assertEqual(cr.main(["--root", str(self.root), "--only", "경로,위생"]), 0)
        self.assertIn("[실패] 검색기록", out.getvalue())
        self.assertIn("알 수 없는 규칙", err.getvalue())


class NumberRule(RepoCopy):
    """결과 파일과 코드를 하나씩 깨뜨리면 그 값에 기댄 점검이 실패한다."""

    E1 = "results/e1_static_quantum_sweep.csv"
    E2 = "results/e2_dynamic_policies.csv"
    E4 = "results/e4_multicore_scaling.csv"
    E5 = "results/e5_affinity_loadbalance.csv"
    E6 = "results/e6_cost_sensitivity.csv"
    CI = "results/validation/ci_summary.csv"

    def test_each_kind_of_check_notices_a_changed_result(self):
        pcpu = "percpu(soft,lb100)"
        cases = [
            ("value", self.E1, {"workload": "mixed", "base_q": "1"}, {"switch_per_kt": "160"}, "C-E1T-01"),
            ("rel", self.E2, {"workload": "batch", "policy": "feedback"}, {"avg_wait": "3000"}, "C-E2F-03"),
            ("ratio", self.E4, {"ncpu": "8", "workload": "mixed", "config": "global"}, {"migrations": "9000"}, "C-E4B-09"),
            ("colrel", self.E6, {"lock_cost": "0.0", "mig_penalty": "0"}, {"p_wait": "2500"}, "C-E6-06"),
            ("colratio", self.E6, {"lock_cost": "0.25", "mig_penalty": "3"}, {"p_wait": "3000"}, "C-E6-25"),
            ("min", self.E5, {"workload": "mixed", "affinity": "strict", "lb_period": "0", "idle_steal": "0"}, {"migrations": "5"}, "C-E5-01"),
            ("max", self.E5, {"workload": "mixed", "affinity": "strict", "lb_period": "25", "idle_steal": "1"}, {"migrations": "300"}, "C-E5-02"),
            ("argmin", self.E5, {"workload": "mixed", "affinity": "strict", "lb_period": "0", "idle_steal": "1"}, {"avg_wait": "9999"}, "C-E5-36"),
            ("series", self.E1, {"workload": "mixed", "base_q": "8"}, {"switch_per_kt": "200"}, "C-E1B-05"),
            ("spread", self.E4, {"ncpu": "16", "workload": "mixed", "config": pcpu}, {"util": "0.5"}, "C-E4B-06"),
            ("gt", self.E5, {"workload": "mixed", "affinity": "none", "lb_period": "0", "idle_steal": "1"}, {"avg_wait": "1000"}, "C-E5-15"),
            ("maxabs", self.CI, {"cid": "F1-E4-global-02", "metric": "avg_wait"}, {"rel_pct": "50"}, "C-F1-02"),
        ]
        for kind, rel, match, change, expected in cases:
            with self.subTest(kind=kind):
                self.assert_clean("수치")
                with self.csv_edit(rel, match, **change):
                    self.assert_fails_with("수치", expected)
                self.assert_clean("수치")

    def test_count_and_grep_checks_notice_changes(self):
        with self.subTest("count"):
            path = self.root / "results/validation/mutation_check.csv"
            original = path.read_bytes()
            lines = original.decode("utf-8").rstrip("\n").split("\n")
            path.write_bytes(("\n".join(lines[:-1]) + "\n").encode("utf-8"))
            try:
                self.assert_fails_with("수치", "C-ST-02")
            finally:
                path.write_bytes(original)
        with self.subTest("grep 개수"):
            with self.text_edit("tests/test_invariants.py", "def test_h8_same_tick_handoff_reproducer", "def test_x8_same_tick_handoff_reproducer"):
                self.assert_fails_with("수치", "C-ST-01")
        with self.subTest("grep 존재"):
            with self.text_edit("mlfqsim/sim.py", "cs_cost: int = 1 ", "cs_cost: int = 2 "):
                self.assert_fails_with("수치", "C-CODE-06")
        self.assert_clean("수치")

    def test_check_that_cannot_be_computed_fails_instead_of_passing(self):
        checks = "refs/claims-checks.csv"
        with self.subTest("열 이름 오타"):
            with self.csv_edit(checks, {"check_id": "C-E1T-01"}, column="switch_per_kx"):
                self.assert_fails_with("수치", "C-E1T-01: 점검을 계산하지 못했다")
        with self.subTest("맞는 행 없음"):
            with self.csv_edit(checks, {"check_id": "C-E1T-01"}, filter="workload=mixed;base_q=3"):
                self.assert_fails_with("수치", "C-E1T-01: 점검을 계산하지 못했다")
        with self.subTest("없는 파일"):
            with self.csv_edit(checks, {"check_id": "C-E1T-01"}, file="results/no_such_file.csv"):
                self.assert_fails_with("수치", "C-E1T-01: 점검을 계산하지 못했다")
        with self.subTest("알 수 없는 종류"):
            with self.csv_edit(checks, {"check_id": "C-E1T-01"}, kind="valu"):
                self.assert_fails_with("수치", "C-E1T-01: 알 수 없는 종류")
        with self.subTest("점검표가 없음"):
            path = self.root / checks
            original = path.read_bytes()
            path.unlink()
            try:
                self.assertIn("refs/claims-checks.csv가 없다", self.fails("수치")[0])
            finally:
                path.write_bytes(original)
        with self.subTest("점검표의 열이 다름"):
            with self.text_edit(checks, "check_id,kind,file,", "check_id,kind,path,"):
                self.assertIn("claims-checks.csv의 열이", self.fails("수치")[0])
        self.assert_clean("수치")

    def test_document_and_check_must_agree(self):
        checks = "refs/claims-checks.csv"
        with self.subTest("README 표의 숫자만 바꿈"):
            with self.text_edit("README.md", "| 2 | 2840 | 2139 |", "| 2 | 2841 | 2139 |"):
                self.assert_fails_with("수치", "C-E4T-01: 문서 README.md에 이 문구가 없다")
        with self.subTest("README 문장의 숫자만 바꿈"):
            with self.text_edit("README.md", "+0.7% [-0.9, +2.4]", "+0.8% [-0.9, +2.4]"):
                self.assert_fails_with("수치", "C-E6-04: 문서 README.md에 이 문구가 없다")
        with self.subTest("점검표의 주장만 바꿈(허용 오차 밖)"):
            with self.csv_edit(checks, {"check_id": "C-E3-01"}, claimed="6900"):
                self.assert_fails_with("수치", "C-E3-01: 문서의 숫자와 결과가 다르다")
        with self.subTest("점검표의 주장만 바꿈(허용 오차 안, 문구에는 없는 숫자)"):
            with self.csv_edit(checks, {"check_id": "C-E3-01"}, claimed="6849.3"):
                self.assert_fails_with("수치", "C-E3-01: 주장한 숫자 6849.3가 문구 안에 없다")
        with self.subTest("표 행에서는 칸 전체가 같아야 한다"):
            # 주장 '6'은 같은 행의 다른 칸('66')의 일부이고 허용 오차를 크게 해 값 비교는 통과시킨다
            with self.csv_edit(checks, {"check_id": "C-E4T-06"}, claimed="6", tol="100"):
                self.assert_fails_with("수치", "C-E4T-06: 주장한 숫자 6가 문구 안에 없다")
        with self.subTest("snippet 비움"):
            with self.csv_edit(checks, {"check_id": "C-E1T-01"}, snippet=""):
                self.assert_fails_with("수치", "C-E1T-01: 문서 README.md에 이 문구가 없다")
        with self.subTest("점검 번호 겹침"):
            with self.csv_edit(checks, {"check_id": "C-E1T-02"}, check_id="C-E1T-01"):
                self.assert_fails_with("수치", "C-E1T-01: 점검 번호가 겹친다")
        self.assert_clean("수치")


class LedgerRule(RepoCopy):
    LEDGER = "refs/claims-ledger.md"

    def test_ledger_rows_are_checked_against_checks_and_literature_table(self):
        L = self.LEDGER
        cases = [
            ("없는 점검 번호", "C-E1T-01..12", "C-E1T-01..99", "점검 C-E1T-17가 claims-checks.csv에 없다"),
            ("어느 행도 가리키지 않는 점검", "C-CODE-13..15", "C-CODE-13..14", "C-CODE-15: 어느 대장 행도 이 점검을 가리키지 않는다"),
            ("거꾸로 된 범위", "C-E1T-01..12", "C-E1T-12..01", "범위가 거꾸로다"),
            ("수준 V인데 점검 없음", "| C-E1T-01..12 | 일치 | V |", "| 없음 | 일치 | V |", "R01: 수준 V인데 점검이 없다"),
            ("수준 X인데 점검 있음", "| 없음 | 미검증 | X | 원래 문장은 원인을 단정했고", "| C-E1T-01 | 미검증 | X | 원래 문장은 원인을 단정했고", "R04: 수준 X는 점검이 없고"),
            ("알 수 없는 상태", "| C-E1T-13..16 | 고침 | V |", "| C-E1T-13..16 | 바꿈 | V |", "R02: 상태가 일치, 고침, 해석, 미검증이 아니다"),
            ("고침인데 메모 없음", "| 고침 | V | 행 이름이 \"batch 최대 대기\"였으나 값은 mixed에서 모든 task 중 최대 대기(`max_wait`)다. |", "| 고침 | V |  |", "R02: 상태가 고침이면 메모"),
            ("자체 결과 행에 문헌 ID", "| 일치 | V | 7개 q 모두 단조 감소다. |", "| 일치 | V | 7개 q 모두 단조 감소다(S01). |", "R05: 자체 결과 주장에 문헌 ID나 호출 코드가 섞였다"),
            ("번호 겹침", "| L02 |", "| L01 |", "L01: 대장 번호가 겹친다"),
            ("자체 결과 행의 수준", "| C-E1T-01..12 | 일치 | V |", "| C-E1T-01..12 | 일치 | A |", "R01: 자체 결과 주장의 수준이 V나 X가 아니다"),
            ("미검증인데 수준 V", "| 없음 | 미검증 | X | 원래 문장은 원인을 단정했고", "| 없음 | 미검증 | V | 원래 문장은 원인을 단정했고", "R04: 미검증은 수준이 X여야 한다"),
        ]
        for name, old, new, needle in cases:
            with self.subTest(name):
                self.assert_clean("대장")
                with self.text_edit(L, old, new):
                    self.assert_fails_with("대장", needle)
        self.assert_clean("대장")

    def test_literature_rows_follow_the_level_rules(self):
        L = self.LEDGER
        cases = [
            ("없는 문헌", "| L01 | 정적 MLFQ(레벨마다 quantum이 두 배, quantum을 다 쓰면 강등)가 기준 설계다. 설계 서술이고 측정은 없다 | S01 |", "| L01 | 정적 MLFQ(레벨마다 quantum이 두 배, quantum을 다 쓰면 강등)가 기준 설계다. 설계 서술이고 측정은 없다 | S99 |", "L01: 문헌 S99가 docs/01-prior-art.md §6에 없다"),
            ("문헌보다 강한 수준", "| S17 | G4-044 | B | 단서 | B6 |", "| S17 | G4-044 | A" + MINUS + " | 단서 | B6 |", "L20: 수준 A" + MINUS + "이 인용한 문헌 중 가장 약한 수준 B보다 강하다"),
            ("C 수준을 전제로", "| S03 | G4-040 | C | 단서 | A1 |", "| S03 | G4-040 | C | 전제 | A1 |", "L02: 수준 C은 단서로만 쓴다"),
            ("호출 불일치", "| S01 | G4-009 | A" + MINUS + " | 전제 | A1 |", "| S01 | G4-010 | A" + MINUS + " | 전제 | A1 |", "L01: 호출"),
            ("없는 하위 주장", "| S01 | G4-009 | A" + MINUS + " | 전제 | A1 |", "| S01 | G4-009 | A" + MINUS + " | 전제 | A9 |", "L01: 하위 주장 A9가 docs/01-prior-art.md에 없다"),
            ("하위 주장 없음인데 전제", "| S22 | G4-048 | B | 단서 | 없음 |", "| S22 | G4-048 | A" + MINUS + " | 전제 | 없음 |", "L27: 수준 A" + MINUS + "이 인용한 문헌 중 가장 약한 수준 B보다 강하다"),
            ("문헌 행에 점검 번호", "| L01 | 정적 MLFQ(레벨마다 quantum이 두 배", "| L01 | C-E1T-01 정적 MLFQ(레벨마다 quantum이 두 배", "L01: 문헌 주장에 자체 결과 번호나 점검 번호가 섞였다"),
            ("문헌 주장의 수준이 문헌 수준이 아님", "| S01 | G4-009 | A" + MINUS + " | 전제 | A1 |", "| S01 | G4-009 | V | 전제 | A1 |", "L01: 문헌 주장의 수준이 문헌 수준이 아니다"),
            ("쓰임이 전제나 단서가 아님", "| S01 | G4-009 | A" + MINUS + " | 전제 | A1 |", "| S01 | G4-009 | A" + MINUS + " | 참고 | A1 |", "L01: 쓰임이 전제, 단서가 아니다"),
        ]
        for name, old, new, needle in cases:
            with self.subTest(name):
                self.assert_clean("대장")
                with self.text_edit(L, old, new):
                    self.assert_fails_with("대장", needle)
        self.assert_clean("대장")

    def test_weaker_level_is_allowed_and_every_source_must_be_cited(self):
        L = self.LEDGER
        # 문헌 수준보다 약하게 적는 것은 된다(보수적인 쪽)
        with self.text_edit(L, "| S01 | G4-009 | A" + MINUS + " | 전제 | A1 |", "| S01 | G4-009 | B | 단서 | A1 |"):
            self.assert_clean("대장")
        # 문헌 ID 표의 문헌을 어느 행도 인용하지 않으면 실패
        row = next(l for l in (self.root / L).read_text(encoding="utf-8").split("\n") if l.startswith("| L28 |"))
        with self.text_edit(L, row + "\n", ""):
            self.assert_fails_with("대장", "S35: 문헌 ID 표(§6)에 있으나 어느 문헌 주장(L) 행도 인용하지 않는다")
        self.assert_clean("대장")


class SearchLogRule(RepoCopy):
    LOG = "refs/g4-search-log.md"
    DOC = "docs/01-prior-art.md"

    def test_log_numbering_and_counts(self):
        row = next(l for l in (self.root / self.LOG).read_text(encoding="utf-8").split("\n") if l.startswith("| G4-040 |"))
        with self.subTest("호출 번호가 빠짐"):
            with self.text_edit(self.LOG, row + "\n", ""):
                self.assert_fails_with("검색기록", "호출 번호가 G4-001부터 빠짐없이 이어지지 않는다")
        with self.subTest("문서의 횟수가 다름"):
            with self.text_edit(self.DOC, "검색과 열람은 74회다", "검색과 열람은 73회다"):
                self.assert_fails_with("검색기록", "검색과 열람은 73회다")
        with self.subTest("README의 횟수가 다름"):
            with self.text_edit("README.md", "검색·열람 74회", "검색·열람 75회"):
                self.assert_fails_with("검색기록", "검색·열람 75회")
        with self.subTest("횟수를 적은 문장이 사라짐(조용히 통과하지 않는다)"):
            with self.text_edit("README.md", "검색·열람 74회", "검색·열람 횟수"):
                self.assert_fails_with("검색기록", "횟수나 판정 집계를 적은 문장")
        with self.subTest("경로와 도구가 맞지 않음"):
            with self.text_edit(self.LOG, "| G4-001 | K | WebSearch |", "| G4-001 | K | WebFetch |"):
                self.assert_fails_with("검색기록", "G4-001: 경로 K와 도구 WebFetch이 맞지 않는다")
        with self.subTest("RQ 열이 비정상"):
            with self.text_edit(self.LOG, "| G4-002 | K | WebSearch |", "| G4-002 | Q | WebSearch |"):
                self.assert_fails_with("검색기록", "G4-002: 경로 표기가 K, C, A, R이 아니다")
        self.assert_clean("검색기록")

    def test_prior_art_document_references(self):
        s37 = "| S37 | HMC 강의 페이지(MLFQ) | C | G4-006 |"
        cases = [
            ("표의 로그에 없는 호출 코드", "| S01 | OSTEP 8장, 다단계 피드백 큐(위스콘신 CS537 2021 사본) | A" + MINUS + " | G4-009 |", "| S01 | OSTEP 8장, 다단계 피드백 큐(위스콘신 CS537 2021 사본) | A" + MINUS + " | G4-999 |", "S01의 호출 G4-999가 검색 기록에 없다"),
            ("본문(§6 밖)의 로그에 없는 호출 코드", "검색과 열람은 74회다", "검색과 열람은 74회다(G4-999)", "docs/01-prior-art.md: 검색 기록에 없는 호출 코드 G4-999"),
            ("본문에서 한 번도 쓰이지 않은 문헌 ID", s37, s37 + "\n| S38 | 시험용 문헌 | C | G4-006 |", "본문에서 한 번도 쓰이지 않은 문헌 ID S38"),
            ("표에 없는 문헌 ID", "S11이 서술한 방향을 G5의 사전 예측으로", "S99가 서술한 방향을 G5의 사전 예측으로", "§6에 없는 문헌 ID S99"),
            ("판정을 바꾸고 집계는 그대로", "| A5 | 찾지 못함 | | 찾지 못함 |", "| A5 | 찾지 못함 | | 동일 |", "RQ-A 집계"),
            ("문헌 수준이 문헌 수준이 아님", "| S36 | 위키백과, Multilevel feedback queue | C |", "| S36 | 위키백과, Multilevel feedback queue | Q |", "S36의 수준이 문헌 수준이 아니다"),
        ]
        for name, old, new, needle in cases:
            with self.subTest(name):
                self.assert_clean("검색기록")
                with self.text_edit(self.DOC, old, new):
                    self.assert_fails_with("검색기록", needle)
        with self.subTest("README의 판정 집계가 문서와 다름"):
            with self.text_edit("README.md", "동일 12, 부분 4, 찾지 못함 4", "동일 13, 부분 3, 찾지 못함 4"):
                self.assert_fails_with("검색기록", "동일 13, 부분 3, 찾지 못함 4")
        self.assert_clean("검색기록")


class PathAndHygieneRules(RepoCopy):
    def test_broken_links_and_scripts_are_found_but_planned_ones_are_skipped(self):
        with self.subTest("없는 링크"):
            with self.text_edit("README.md", "[results/summary.md](results/summary.md)", "[results/summary.md](results/no_such.md)"):
                self.assert_fails_with("경로", "링크가 가리키는 파일이 없다 results/no_such.md")
        with self.subTest("없는 경로 표기"):
            with self.text_edit("README.md", "| `mlfqsim/policies.py` |", "| `mlfqsim/no_such.py` |"):
                self.assert_fails_with("경로", "경로가 없다 mlfqsim/no_such.py")
        with self.subTest("실행 예의 스크립트"):
            with self.text_edit("README.md", "python analysis/bootstrap_ci.py ", "python analysis/no_such_tool.py ", count=1):
                self.assert_fails_with("경로", "실행 예에 적힌 스크립트가 없다 analysis/no_such_tool.py")
        with self.subTest("예정인 줄은 건너뜀"):
            plan = "# 계획\n\n다음 문서는 `docs/no_such_plan.md`와 [계획](no_such_plan.md)이다(예정).\n"
            with self.added_file("docs/zz_plan.md", plan.encode("utf-8")):
                self.assert_clean("경로")
            with self.added_file("docs/zz_plan.md", plan.replace("(예정)", "").encode("utf-8")):    # 예정이 없으면 같은 줄이 걸린다
                self.assert_fails_with("경로", "경로가 없다 docs/no_such_plan.md")
                self.assert_fails_with("경로", "링크가 가리키는 파일이 없다 no_such_plan.md")
        self.assert_clean("경로")

    def test_hygiene_finds_private_strings_and_pdfs(self):
        email = "someone" + "@" + "example.org"
        win = "C:" + "\\" + "Us" + "ers" + "\\" + "someone" + "\\" + "x"
        unix = "/" + "Us" + "ers" + "/someone/work"
        key = "api" + "_key = " + "A1b2C3d4E5f6G7h8"
        for name, bad, needle in (("이메일", email, "이메일"), ("윈도 경로", win, "윈도 사용자 경로"), ("홈 경로", unix, "사용자 홈 경로"), ("비밀값", key, "비밀값 모양")):
            with self.subTest(name):
                self.assert_clean("위생")
                with self.added_file("docs/zz_scratch.md", ("# 메모\n\n" + bad + "\n").encode("utf-8")):
                    self.assert_fails_with("위생", "docs/zz_scratch.md:3: " + needle)
        with self.subTest("PDF"):
            with self.added_file("docs/zz_paper.pdf", b"%PDF-1.4\n"):
                self.assert_fails_with("위생", "docs/zz_paper.pdf: PDF 파일이 저장소에 있다")
        with self.subTest(".git 디렉터리는 보지 않음"):
            line = ("x " + email + "\n").encode("utf-8")
            with self.added_file("docs/zz_log.md", line):      # 같은 내용도 다른 곳에서는 걸린다(.md라서 보는 확장자다)
                self.assert_fails_with("위생", "docs/zz_log.md:1: 이메일")
            with self.added_file(".git/zz_log.md", line):
                self.assert_clean("위생")
        self.assert_clean("위생")


class Kinds(unittest.TestCase):
    """점검 종류를 작은 표로 하나씩 직접 시험한다."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        (cls.root / "t.csv").write_text("g,x,v,w\na,1,10,5\na,2,20,6\na,3,30,7\nb,1,5,100\nb,2,6,200\n", encoding="utf-8")
        (cls.root / "s.txt").write_text("alpha\nbeta\nalpha beta\n", encoding="utf-8")
        cls.cache = {}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_kind(self, kind, filt="", filt_b="", col="", order="", claimed="", tol="", file="t.csv"):
        c = {"check_id": "C-T-01", "kind": kind, "file": file, "filter": filt, "filter_b": filt_b, "column": col,
             "order": order, "claimed": claimed, "tol": tol, "doc": "README.md", "snippet": ""}
        return cr.run_check(self.root, c, self.cache)[0]

    def test_scalar_kinds(self):
        self.assertTrue(self.run_kind("value", "g=a;x=2", col="v", claimed="20", tol="0"))
        self.assertFalse(self.run_kind("value", "g=a;x=2", col="v", claimed="21", tol="0"))
        self.assertTrue(self.run_kind("value", "g=a;x=2", col="v", claimed="21", tol="1"))
        self.assertTrue(self.run_kind("rel", "g=a;x=1", "g=a;x=3", col="v", claimed="200", tol="0.001"))
        self.assertFalse(self.run_kind("rel", "g=a;x=3", "g=a;x=1", col="v", claimed="200", tol="0.001"))
        self.assertTrue(self.run_kind("ratio", "g=a;x=3", "g=a;x=1", col="v", claimed="3", tol="0"))
        self.assertTrue(self.run_kind("colrel", "g=a;x=2", col="w/v", claimed="-70", tol="0.001"))
        self.assertTrue(self.run_kind("colratio", "g=a;x=2", col="w/v", claimed="0.3", tol="0.001"))
        self.assertTrue(self.run_kind("min", "g=a", col="v", claimed="10", tol="0"))
        self.assertTrue(self.run_kind("max", "g=a", col="v", claimed="30", tol="0"))
        self.assertTrue(self.run_kind("maxabs", "g=b", col="w", claimed="200", tol="0"))
        self.assertTrue(self.run_kind("count", "g=a", claimed="3", tol="0"))
        self.assertTrue(self.run_kind("count", "", claimed="5", tol="0"))
        self.assertTrue(self.run_kind("spread", "g=a", col="v", claimed="2", tol="0.001"))
        self.assertTrue(self.run_kind("grep", "^alpha", claimed="2", tol="0", file="s.txt"))
        self.assertFalse(self.run_kind("grep", "^alpha", claimed="3", tol="0", file="s.txt"))

    def test_identity_and_order_kinds(self):
        self.assertTrue(self.run_kind("argmin", "g=a", col="v", order="x", claimed="1"))
        self.assertTrue(self.run_kind("argmax", "g=a", col="v", order="x", claimed="3"))
        self.assertFalse(self.run_kind("argmax", "g=a", col="v", order="x", claimed="1"))
        self.assertTrue(self.run_kind("series", "g=a", col="v", order="x", claimed="inc"))
        self.assertFalse(self.run_kind("series", "g=a", col="v", order="x", claimed="dec"))
        self.assertTrue(self.run_kind("series", "g=a", col="w", order="x", claimed="inc"))
        # 정렬 열 기준으로 정렬한 뒤 본다(행 순서가 아니다). 파일의 행 순서와 정렬 열의 순서가 다른 표로 확인한다.
        (self.root / "o.csv").write_text("k,v\n3,30\n1,10\n2,20\n", encoding="utf-8")
        self.assertTrue(self.run_kind("series", "", col="v", order="k", claimed="inc", file="o.csv"))
        self.assertFalse(self.run_kind("series", "", col="v", order="k", claimed="dec", file="o.csv"))
        self.assertTrue(self.run_kind("gt", "g=a", "g=b", col="v"))
        self.assertFalse(self.run_kind("gt", "g=b", "g=a", col="v"))
        self.assertFalse(self.run_kind("gt", "g=a", "g=a", col="v"))

    def test_errors_are_raised_not_swallowed(self):
        for kind, kw in (
            ("value", {"filt": "g=a", "col": "v"}),                       # 행이 3개
            ("value", {"filt": "g=c", "col": "v"}),                       # 행이 없음
            ("value", {"filt": "zz=a", "col": "v"}),                      # 필터의 열이 없음
            ("value", {"filt": "g=a;x=1", "col": "zz"}),                  # 값의 열이 없음
            ("min", {"filt": "g=c", "col": "v"}),
            ("series", {"filt": "g=a", "col": "v", "order": "x", "claimed": "up"}),
            ("series", {"filt": "g=a;x=1", "col": "v", "order": "x", "claimed": "inc"}),
            ("rel", {"filt": "g=a;x=1", "filt_b": "g=a", "col": "v"}),
            ("argmin", {"filt": "g=a", "col": "v", "order": "zz"}),
            ("spread", {"filt": "g=a;x=1", "col": "v"}),
            ("gt", {"filt": "g=a", "filt_b": "g=c", "col": "v"}),
            ("value", {"filt": "g a", "col": "v"}),                       # 필터 형식 오류
        ):
            with self.subTest(kind=kind, kw=kw):
                with self.assertRaises((ValueError, KeyError)):
                    self.run_kind(kind, **kw)
        with self.assertRaises(ValueError):
            self.run_kind("value", "g=a;x=1", col="v", claimed="1", tol="abc")
        with self.assertRaises(ValueError):
            self.run_kind("value", "g=a;x=1", col="v", claimed="1", file="missing.csv")

    def test_spread_with_zero_minimum_is_reported_with_its_reason(self):
        (self.root / "z.csv").write_text("k,v\na,0\nb,5\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "최솟값이 0"):
            self.run_kind("spread", "", col="v", claimed="1", file="z.csv")

    def test_ties_for_argmin_are_an_error(self):
        (self.root / "tie.csv").write_text("k,v\na,1\nb,1\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_kind("argmin", "", col="v", order="k", claimed="a", file="tie.csv")

    def test_judge_forms(self):
        self.assertTrue(cr.judge(5.0, "5", 0)[0])
        self.assertTrue(cr.judge(5.04, "5", 0.05)[0])
        self.assertFalse(cr.judge(5.06, "5", 0.05)[0])
        self.assertTrue(cr.judge(9.0, "9..12", 0)[0])
        self.assertTrue(cr.judge(12.0, "9..12", 0)[0])
        self.assertFalse(cr.judge(12.01, "9..12", 0)[0])
        self.assertTrue(cr.judge(-3.0, "-5..10", 0)[0])
        self.assertTrue(cr.judge(0.5, "<=0.5", 0)[0])
        self.assertFalse(cr.judge(0.6, "<=0.5", 0)[0])
        self.assertTrue(cr.judge(-1, "<0", 0)[0])
        self.assertFalse(cr.judge(0, "<0", 0)[0])
        self.assertTrue(cr.judge(1004, ">1000", 0)[0])
        self.assertFalse(cr.judge(1000, ">1000", 0)[0])
        self.assertTrue(cr.judge("strict", "strict", 0)[0])
        self.assertFalse(cr.judge("soft", "strict", 0)[0])
        self.assertTrue(cr.judge("0", "0", 0)[0])
        self.assertFalse(cr.judge("x", "<0", 0)[0])

    def test_filter_and_id_helpers(self):
        rows = [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}, {"a": "10", "b": "xy"}]
        self.assertEqual([r["a"] for r in cr.select(rows, "a>1")], ["2", "10"])          # 문자열이 아니라 숫자로 비교
        self.assertEqual([r["a"] for r in cr.select(rows, "b^=x")], ["1", "10"])
        self.assertEqual([r["a"] for r in cr.select(rows, "b!=x;a<=2")], ["2"])
        self.assertEqual([r["a"] for r in cr.select(rows, "a=1.0")], ["1"])
        self.assertEqual(len(cr.select(rows, "")), 3)
        with self.assertRaises(ValueError):
            cr.select(rows, "c=1")
        self.assertEqual(cr.expand_ids("C-A-01..03, C-B-05"), ["C-A-01", "C-A-02", "C-A-03", "C-B-05"])
        with self.assertRaises(ValueError):
            cr.expand_ids("C-A-03..01")
        self.assertEqual(cr.table_cells("| a | b `c` | 1 |"), ["a", "b `c`", "1"])
        self.assertEqual(cr.s_ids_used("S01, S23~S25, S1"), {"S01", "S23", "S24", "S25"})


if __name__ == "__main__":
    unittest.main()
