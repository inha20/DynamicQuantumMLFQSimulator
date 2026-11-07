"""저장된 실험 결과를 처음부터 다시 만들어 비교한다(재현 점검).

    python3 -I -B analysis/check_reproduce.py                 # 전체 E1~E6 (약 1분)
    python3 -I -B analysis/check_reproduce.py --only e1,e6    # 일부만

저장소의 results/는 건드리지 않고 임시 폴더에 다시 만든다. 비교 규칙은 다음과 같다.

- results/*.csv, results/summary.md: 줄바꿈(CRLF와 LF)만 맞춘 뒤 바이트까지 같아야 한다. CSV 모듈은 CRLF로 쓰는데
  저장소(깃)에는 LF로 들어 있고 Windows 작업 폴더에는 CRLF로 들어 있을 수 있어서 줄바꿈은 같게 본다. summary.md는 전체를 돌릴 때만 비교한다.
- results/*.png: 비교하지 않는다. 그리기 라이브러리(matplotlib) 버전에 따라 바이트가 달라진다.

모든 파일이 같으면 종료 코드 0, 하나라도 다르면 1.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import experiments  # noqa: E402

STEPS = {
    "e1": ("e1_static_quantum_sweep.csv", experiments.e1_quantum_sweep),
    "e2": ("e2_dynamic_policies.csv", experiments.e2_dynamic_policies),
    "e3": ("e3_starvation_boost.csv", experiments.e3_starvation),
    "e4": ("e4_multicore_scaling.csv", experiments.e4_multicore),
    "e5": ("e5_affinity_loadbalance.csv", experiments.e5_affinity_lb),
    "e6": ("e6_cost_sensitivity.csv", experiments.e6_lock_sensitivity),
}


def first_difference(a: bytes, b: bytes) -> str:
    la, lb = a.split(b"\n"), b.split(b"\n")
    diff = [i for i in range(max(len(la), len(lb)))
            if (la[i] if i < len(la) else None) != (lb[i] if i < len(lb) else None)]
    i = diff[0]
    sa = la[i].decode("utf-8", "replace")[:100] if i < len(la) else "(없음)"
    sb = lb[i].decode("utf-8", "replace")[:100] if i < len(lb) else "(없음)"
    return f"{len(diff)}줄이 다르다. 처음 다른 줄 {i + 1}: 저장본 [{sa}] / 다시 만든 것 [{sb}]"


def compare(saved: Path, fresh: Path, normalize_eol: bool = True):
    if not saved.exists():
        return False, "저장본이 없다"
    if not fresh.exists():
        return False, "다시 만든 파일이 없다"
    a, b = saved.read_bytes(), fresh.read_bytes()
    if normalize_eol:
        a, b = a.replace(b"\r\n", b"\n"), b.replace(b"\r\n", b"\n")
    return (True, "같다") if a == b else (False, first_difference(a, b))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="쉼표로 구분한 단계(e1..e6). 비우면 전체")
    a = ap.parse_args(argv)
    names = [x.strip().lower() for x in a.only.split(",") if x.strip()]
    for n in names:
        if n not in STEPS:
            print(f"알 수 없는 단계: {n}")
            return 2
    full = not names
    t0 = time.time()
    saved_dir = ROOT / "results"
    ok_all = True
    with tempfile.TemporaryDirectory() as td:
        experiments.OUT = Path(td)
        with contextlib.redirect_stdout(io.StringIO()):
            if full:
                experiments.main()
            else:
                for n in names:
                    STEPS[n][1]()
        for n, (fname, _) in STEPS.items():
            if not full and n not in names:
                continue
            ok, msg = compare(saved_dir / fname, Path(td) / fname)
            ok_all &= ok
            print(f"{'같음' if ok else '다름'}  {fname}" + ("" if ok else f"  -> {msg}"))
        if full:
            ok, msg = compare(saved_dir / "summary.md", Path(td) / "summary.md")
            ok_all &= ok
            print(f"{'같음' if ok else '다름'}  summary.md" + ("" if ok else f"  -> {msg}"))
            print("건너뜀  *.png (그리기 라이브러리 버전에 따라 바이트가 다르다)")
    print(f"{'재현됨' if ok_all else '재현되지 않음'}  ({time.time() - t0:.0f}초, Python {sys.version.split()[0]})")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
