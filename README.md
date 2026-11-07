# cpuQueue — 동적 Time Quantum MLFQ와 Per-CPU Runqueue 비교 연구

> 저장소 이름: DynamicQuantumMLFQSimulator · 프로젝트 이름: cpuQueue — 동적 Time Quantum MLFQ와 Per-CPU Runqueue 비교 연구

> **연구 상태 안내(2026-11-08):** 시뮬레이터와 결과는 그대로 두고, 그 위에 연구 상태 문서를 AI(Claude)가 초안으로 더하는 중이다. 초안이며 사람의 검토 전이다. 이 README의 결과는 가설을 미리 적어 두지 않고 얻은 **탐색적 결과**이고, 이 저장소는 새롭다는 주장을 하지 않는다. 진행 기록은 [docs/00-progress-log.md](docs/00-progress-log.md).

두 가지 질문을 **시뮬레이터로 직접 측정**한 프로젝트입니다.

1. MLFQ의 queue별 time quantum을 workload와 queue state에 따라 동적으로 조절하면 평균 대기시간 · 응답시간 · context switch · starvation 사이의 trade-off가 어떻게 변하는가?
2. 이를 멀티코어의 per-CPU runqueue + CPU affinity + load balancing에 적용하면 단일 전역 MLFQ 대비 어떤 성능 차이가 나는가?

순수 Python(표준 라이브러리만, 그래프는 matplotlib 선택)으로 틱 단위 스케줄러 시뮬레이터를 만들고, seed 10개 평균으로 6개 실험을 돌렸습니다. 전체 표는 [results/summary.md](results/summary.md), 해석은 [docs/analysis.md](docs/analysis.md)에 있습니다.

## 현재 상태 (2026-11-08)

| 단계 | 상태 |
|---|---|
| 시뮬레이터, 실험 E1–E6, 결과 | 있음(탐색적 결과). 2026-11-08에 처음부터 다시 돌려 결과 CSV 6개가 저장본과 같음을 확인했다(줄바꿈 표기 CRLF/LF를 맞춘 뒤 바이트 단위로 비교) |
| 측정 도구 검증(손계산 사례, 불변식, 변이 점검, 재현 점검, 신뢰구간) | 초안 완료, 사람의 검토 전: [docs/03-measurement-validation.md](docs/03-measurement-validation.md). 손계산 사례 14개 일치, 변이 36개를 모두 잡음. **결함 1건 발견(F1: 멀티코어에서 같은 틱 이중 사용)**. 영향은 작지만 아래 "한계"의 E4~E6 수치 주의를 보라 |
| 선행연구 위치 잡기(G4) | 초안 완료, 조건부, 사람의 검토 전: [docs/01-prior-art.md](docs/01-prior-art.md), 검색 기록 [refs/g4-search-log.md](refs/g4-search-log.md)(검색·열람 74회). 질문을 셋(RQ-A 동적 quantum, RQ-B global 대 per-CPU, RQ-C 강등 판정과 quantum의 분리)으로 나눠 하위 주장 20개를 판정했다: 동일 12, 부분 4, 찾지 못함 4. 새롭다는 주장을 하지 않는다. 가장 가까운 선행은 중앙 큐와 CPU별 큐를 이주 비용에 따라 시뮬레이션으로 비교한 1990년 연구다. 인용 추적을 하지 못해 '없음' 판정은 쓰지 않았다 |
| 주장 대장과 문서-결과 수치 대조 | 초안 완료, 사람의 검토 전: [refs/claims-ledger.md](refs/claims-ledger.md). README의 결과 문장(자체 결과 주장 53개)과 선행연구 판정(문헌 주장 28개)이 무엇에 기대는지 한 줄씩 적었다. 수치 점검 286개([refs/claims-checks.csv](refs/claims-checks.csv))를 `analysis/check_repo.py`가 결과 CSV와 소스에서 다시 계산해 README의 숫자와 맞춘다. 그 과정에서 README 문장 19개가 결과와 어긋나 고쳤다. 원인과 가설을 단정한 문장 5개는 점검하지 못해 '미검증'으로 두었다. 점검기의 시험은 점검기를 일부러 틀리게 고친 50곳을 모두 잡는다 |
| 질문형 연구 질문과 사전 분석 계획(G5 초안) | 예정. 확인용 실행과 새 정책 구현은 계획을 동결(사람의 승인)하기 전에는 하지 않는다 |

단계별 기록과 막힌 점은 [docs/00-progress-log.md](docs/00-progress-log.md)에 있다.

## 실행

```bash
python -m unittest discover -s tests   # 테스트 (약 10초)
python experiments.py                  # 전체 실험 (약 1~2분) -> results/
python analysis/check_reproduce.py     # 저장된 결과를 처음부터 다시 만들어 비교 (약 1분, 저장소의 results/는 건드리지 않음)
python analysis/bootstrap_ci.py        # 신뢰구간 -> results/validation/ci_summary.csv (약 30초)
python analysis/mutation_check.py      # 시험이 일부러 넣은 결함을 잡는지 확인 (약 3분)
python analysis/diag_f1.py             # 결함 F1의 빈도 -> results/validation/f1_negative_waits.csv (약 30초)
python analysis/check_repo.py          # README와 docs의 숫자·표기를 결과 파일과 맞춰 보는 점검기 (1초 안팎, 파일은 읽기만 함)
python analysis/mutation_check_checker.py   # 점검기의 시험이 점검기의 결함을 잡는지 확인 (약 1분)
```

## 구조

| 경로 | 내용 |
|---|---|
| `mlfqsim/policies.py` | MLFQ, 정적/load/feedback/hybrid quantum 정책, priority boost |
| `mlfqsim/sim.py` | 시뮬레이터 (global MLFQ / per-CPU, affinity, load balancing, 비용 모델) |
| `mlfqsim/workload.py` | interactive / batch / mixed / bursty / starve workload 생성기 |
| `experiments.py` | E1~E6 실험 + 그래프 |
| `tests/` | 단위 시험. `test_sim.py`(기존), `test_invariants.py`(손계산 사례와 불변식), `test_analysis_tools.py`(분석 도구), `test_check_repo.py`(문서 점검기) |
| `analysis/` | 신뢰구간(`bootstrap_ci.py`), 재현 점검(`check_reproduce.py`), 변이 점검(`mutation_check.py`), F1 빈도(`diag_f1.py`), 문서 점검기(`check_repo.py`), 점검기의 변이 점검(`mutation_check_checker.py`) |
| `results/` | CSV, `summary.md`, PNG |
| `results/validation/` | `analysis/` 도구의 결과 CSV(신뢰구간, 변이 점검 둘, F1 빈도) |
| `docs/00-progress-log.md` | 진행 기록(AI 초안) |
| `docs/01-prior-art.md` | 선행연구 위치 잡기(G4, AI 초안, 조건부) |
| `docs/03-measurement-validation.md` | 측정 도구 검증(AI 초안) |
| `refs/g4-search-log.md` | G4 검색·열람 기록(호출 코드 G4-001~074) |
| `refs/claims-ledger.md` | 주장 대장(AI 초안). 자체 결과 주장(R행)과 문헌 주장(L행)을 나눠 적고 수준과 점검 번호를 단다 |
| `refs/claims-checks.csv` | 수치 점검표. 각 행이 README의 문장 하나를 결과 CSV나 소스에서 다시 계산하는 방법 |

## 모델링한 정책

- **static**: `q[l] = base · 2^l`
- **load (queue state 기반)**: 대기 압력 `p = 대기 task 수 / CPU 수`, `scale = clamp(2/(1+p), 0.25, 2)`. 한가하면 quantum을 키우고 붐비면 줄임.
- **feedback (workload 기반)**: 레벨별로 "quantum을 끝까지 쓴 비율"의 EMA를 추적해, CPU-bound면 키우고 I/O-bound면 줄임.
- **hybrid**: feedback × load.
- **멀티코어**: `global`(락 경합 + cache-cold 비용), `percpu`(affinity strict/soft/none, 주기적 push 밸런싱, idle steal).

비용 가정(조정 가능, E6에서 민감도 분석): context switch 1틱, 다른 CPU로 옮겨 실행 시 작업량 +3틱, global 큐 dispatch당 `(N-1)·0.25`틱 락 경합.

## 핵심 결과 (측정값)

### Q1. 동적 quantum의 trade-off

**정적 quantum 스윕 (E1, 1코어)** — quantum은 "context switch ↔ 응답성" 한 축의 교환입니다.

| mixed workload, base q | 1 | 4 | 16 | 64 |
|---|---|---|---|---|
| context switch / 1000tick | 152.8 | 105.5 | 93.5 | 86.2 |
| interactive 지연 p95 | 5.4 | 3.8 | 7.6 | 100.0 |
| 평균 대기 | 1972 | 1798 | 1759 | 2078 |
| 최대 대기(`max_wait`) | 265 | 697 | 860 | 762 |

- 평균 대기는 **U자**: q=1과 q=64에서 나빠지고 q≈4~16이 최적(q=1은 switch 오버헤드, q=64는 convoy 효과로 보이지만 원인은 확인하지 않았습니다).
- q가 커질수록 switch는 줄고(152.8 → 86.2), q≥4에서는 interactive 지연이 급증(p95 3.8 → 100).
- `max_wait`(task 중 최대 대기)는 q=1에서 16까지는 q가 클수록 **길어지고**(265 → 860) q=32, 64에서는 다시 짧아집니다(795, 762). q가 작을 때 짧은 것은 하위 큐가 자주 돌아오기 때문으로 보이지만 원인은 확인하지 않았습니다.

**동적 정책 (E2, 1코어)**

- `feedback`은 정적 대비 평균 대기가 workload에 따라 소폭 줄거나(mixed 1798→1780, batch -2.8%, bursty -0.8%) 소폭 늘고(interactive +2.5%, starve +2.3%), 응답은 5개 workload 모두 줄었습니다(mixed 0.9→0.5). batch workload에서 switch 65.4→56.8/kt. 다만 interactive p95 지연은 5개 workload 모두 늘었습니다(mixed 3.8→4.7).
- `load`는 **응답시간·starvation을 개선하는 대신 평균 대기, switch, interactive p95 지연이 악화**: mixed switch 105.5→125.7/kt, batch switch 65.4→110.8/kt. 평균 대기는 workload에 따라 +2.6%(bursty)~+16.5%(interactive)이고(mixed +6.4%, batch +6.7%), interactive p95는 5개 workload 모두 늘었습니다(mixed 3.8→7.0). `starve` workload에서 응답 11.9→1.4, starved task 2.4→0.
- `hybrid`는 `max_wait`를 5개 workload 모두에서 낮추지만(starve 1004→193) **평균 대기가 늘고**(batch +8.7%, starve +28.0%, bursty +29.8%, mixed +42.9%, interactive +73.4%) **interactive p95 지연이 5개 workload 모두 늘고 mixed와 batch에서는 크게 악화**(mixed 3.8→103, batch 1.5→127). 원인(추정, 확인하지 않음): 붐빌 때 quantum이 1~2틱으로 줄면 원래 레벨 0의 짧은 CPU burst(1~4틱)가 quantum을 못 끝내고 **demote**되어 interactive task가 낮은 큐로 밀림. 즉 "쪼개기"는 starvation에 유리하지만 MLFQ의 분류(heuristic)를 오염시킴.

→ 결론: 단순한 queue-state 기반 축소는 공짜가 아님. quantum을 줄이는 것과 demotion 기준을 분리(예: demotion은 원래 quantum 기준으로 누적 사용량 측정)하면 나아질 수 있다는 것은 **검증하지 않은 가설**입니다. 레벨별 시간 할당으로 강등하는 규칙 자체는 문헌에 있는 설계입니다([docs/01-prior-art.md](docs/01-prior-art.md) §4).

**Starvation vs boost (E3, starve workload)**: 정적 정책에서 boost를 끄면 최대 대기(`max_wait`)가 6849틱. 정적 정책에 boost 주기 P를 두면 `max_wait`는 P 근처로 묶입니다. P가 1000 이하이면 P를 약간 넘고(P=1000에서 1004, P=100에서 110) P=2000에서는 1999.7, P=5000에서는 4850입니다. P=100이면 평균 대기 +18%(5381→6343), interactive p95 +42%(30→43), switch +9%. 동적 hybrid는 boost 없이도 `max_wait`가 90틱으로 억제되나 평균 대기는 정적·boost 없음 대비 +24%(5381→6669)를 지불.

### Q2. 멀티코어: global MLFQ vs per-CPU runqueue

**스케일링 (E4, mixed, 기본 비용)**

| 코어 | global 평균 대기 | per-CPU 평균 대기 | global util | per-CPU util | global 마이그레이션 | per-CPU 마이그레이션 |
|---|---|---|---|---|---|---|
| 2 | 2840 | 2139 | 0.7 | 0.8 | 787 | 66 |
| 4 | 3459 | 2194 | 0.6 | 0.8 | 2403 | 216 |
| 8 | 4028 | 2168 | 0.6 | 0.8 | 5544 | 616 |
| 16 | 5725 | 2157 | 0.4 | 0.8 | 13302 | 1469 |

- global은 코어가 늘수록 평균 대기가 악화(2840→5725)하고 유효 이용률이 하락. per-CPU는 거의 평탄.
- 마이그레이션이 global에서 약 9~12배 많음 (cache-cold 비용의 원인).

**민감도 (E6, 8코어, mixed)**: 이 결과는 **비용 가정에 크게 의존**합니다. 락 비용 5개 x 마이그레이션 페널티 3개의 격자이고 기본값은 락 비용 0.25, 페널티 3입니다.

- 락 비용 0, 마이그레이션 페널티 0이면 두 구조의 평균 대기는 구분되지 않고(1946 vs 1961, +0.7% [-0.9, +2.4]), **global이 interactive 지연 p95에서 오히려 우세**(0.7 vs 12.3): 전역 우선순위 순서가 완벽해서 idle 코어가 없고 높은 우선순위가 즉시 실행됨.
- 마이그레이션 페널티가 3 → 10틱으로 커지면 global 평균 대기가 락 비용 0에서 3192 → 10591, 기본 락 비용 0.25에서 4028 → 12589로 폭증하고 per-CPU는 2168 → 2885로 늘어납니다(per-CPU는 락 비용의 영향을 받지 않음).
- per-CPU 평균 대기 / global 평균 대기 비는 기본 비용(락 0.25, 페널티 3)에서 0.54, 락 비용만 있을 때(페널티 0)는 0.75, 페널티만 있을 때(락 0, 페널티 3)는 0.68, 페널티 10(락 0)에서는 0.27입니다. 즉 **per-CPU의 이득은 락 비용과 마이그레이션 페널티(cache locality) 양쪽에서 나오고, 페널티가 클수록 후자가 지배적**입니다.

**Affinity × load balancing (E5, 8코어, mixed)**

- `strict` affinity: 마이그레이션이 적고(0~244), interactive p95 지연이 가장 낮음(10.5). 하지만 steal/balance가 없으면 imbalance 0.3, makespan 7637로 느림. **idle steal만으로도 makespan 7637→6816(-11%)**.
- `none`(항상 최소 부하 CPU): 마이그레이션 2441~2547, 평균 대기 2551~2632로 세 affinity 중 가장 나쁨(훔치기와 밸런싱이 없는 구성은 2569). 균형은 좋지만 locality를 버림.
- `soft`는 평균 대기와 마이그레이션 횟수가 strict와 none의 중간입니다. **로드밸런싱 주기를 짧게 할수록(400→25) 평균 대기와 interactive 지연이 늘어남**(strict: 평균 대기 1959→2023(+3.3%), p95 12.4→18.3(+48%)): 과도한 밸런싱은 locality를 해침. 평균 대기는 세 affinity 모두 주기가 짧을수록 단조롭게 늘고, p95는 soft와 none에서 단조이지만 strict는 주기 100에서 20.0으로 단조가 아닙니다. 평균 대기가 가장 낮은 조합은 *strict + idle steal*(밸런싱 없음, 1932).

## 한계 (정직한 주의사항)

- 합성 workload와 단순화된 비용 모델이며, 실제 커널(CFS/EEVDF, ULE 등)과 직접 비교한 수치가 아닙니다. 절대값이 아니라 **경향과 민감도**로 읽어야 합니다.
- global에서의 락 경합은 `lock_cost·(N-1)` 선형 모델입니다. 락 구현(예: 분할 락, lock-free)에 따라 크게 달라집니다.
- dynamic 정책의 파라미터(`2/(1+p)`, EMA 임계값)는 튜닝하지 않았습니다. hybrid의 악화는 이 파라미터 + demotion 규칙의 결합 결과이며 일반 결론이 아닙니다.
- seed 10회 평균입니다. 탐색용 신뢰구간(부트스트랩)은 `results/validation/ci_summary.csv`에 있지만, seed가 10개뿐이라 구간이 실제보다 좁게 나오는 경향이 있고 다중 비교 보정을 하지 않았습니다.
- **E4~E6(멀티코어)의 수치는 시뮬레이터 기본 모델의 값입니다.** 이 모델에는 quantum이 끝난 task가 같은 틱 안에 큐로 돌아가 번호가 더 큰 CPU가 곧바로 가져갈 수 있는 결함(F1, [검증 문서](docs/03-measurement-validation.md) §4)이 있습니다. 보정 옵션(`Config.defer_requeue=True`)으로 같은 구성을 돌려 비교한 20개 구성에서 평균 대기는 최대 3.9%, interactive p95는 최대 9.2% 바뀌었고, 문서 §4.4에서 확인한 비교의 방향은 바뀌지 않았습니다. 이주 횟수는 같은 20개 구성에서 최대 20.6% 바뀌고(strict, 주기 100, 훔치기), 별도 진단(`results/validation/f1_negative_waits.csv`)의 strict + 훔치기(8코어, mixed, 밸런싱 없음)에서는 -28%(1390→1004)입니다. 어느 모델을 기본으로 삼고 E4~E6을 다시 낼지는 사람이 정할 일이라 결과 파일은 바꾸지 않았습니다.

## License

MIT
