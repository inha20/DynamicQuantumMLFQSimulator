# cpuQueue — 동적 Time Quantum MLFQ와 Per-CPU Runqueue 비교 연구

두 가지 질문을 **시뮬레이터로 직접 측정**한 프로젝트입니다.

1. MLFQ의 queue별 time quantum을 workload와 queue state에 따라 동적으로 조절하면 평균 대기시간 · 응답시간 · context switch · starvation 사이의 trade-off가 어떻게 변하는가?
2. 이를 멀티코어의 per-CPU runqueue + CPU affinity + load balancing에 적용하면 단일 전역 MLFQ 대비 어떤 성능 차이가 나는가?

순수 Python(표준 라이브러리만, 그래프는 matplotlib 선택)으로 틱 단위 스케줄러 시뮬레이터를 만들고, seed 10개 평균으로 6개 실험을 돌렸습니다. 전체 표는 [results/summary.md](results/summary.md), 해석은 [docs/analysis.md](docs/analysis.md)에 있습니다.

## 실행

```bash
python -m unittest discover -s tests   # 테스트
python experiments.py                  # 전체 실험 (약 1~2분) -> results/
```

## 구조

| 경로 | 내용 |
|---|---|
| `mlfqsim/policies.py` | MLFQ, 정적/load/feedback/hybrid quantum 정책, priority boost |
| `mlfqsim/sim.py` | 시뮬레이터 (global MLFQ / per-CPU, affinity, load balancing, 비용 모델) |
| `mlfqsim/workload.py` | interactive / batch / mixed / bursty / starve workload 생성기 |
| `experiments.py` | E1~E6 실험 + 그래프 |
| `results/` | CSV, `summary.md`, PNG |

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
| batch 최대 대기 | 265 | 697 | 860 | 762 |

- 평균 대기는 **U자**: q=1은 switch 오버헤드, q=64는 convoy 효과로 나빠지고 q≈4~16이 최적.
- q가 커질수록 switch는 줄지만 interactive 지연이 급증(p95 3.8 → 100).
- q가 작을수록 `max_wait`(batch 최대 대기)가 오히려 **짧아짐**: 하위 큐가 자주 돌아오기 때문.

**동적 정책 (E2, 1코어)**

- `feedback`은 정적 대비 소폭 이득이거나 동률: mixed 평균 대기 1798→1780, 응답 0.9→0.5, batch workload에서 switch 65.4→56.8/kt.
- `load`는 **응답시간·starvation을 개선하는 대신 평균 대기와 switch가 악화**: mixed switch 105→126/kt, batch switch 65→111/kt, 평균 대기 +6~7%. `starve` workload에서 응답 11.9→1.4, starved task 2.4→0.
- `hybrid`는 `max_wait`를 크게 낮추지만(starve 1004→193) **평균 대기 +30~40%, interactive p95 지연이 크게 악화**(mixed 3.8→103). 원인: 붐빌 때 quantum이 1~2틱으로 줄면 원래 레벨 0의 짧은 CPU burst(1~4틱)가 quantum을 못 끝내고 **demote**되어 interactive task가 낮은 큐로 밀림. 즉 "쪼개기"는 starvation에 유리하지만 MLFQ의 분류(heuristic)를 오염시킴.

→ 결론: 단순한 queue-state 기반 축소는 공짜가 아님. **quantum을 줄이는 것과 demotion 기준을 분리**(예: demotion은 원래 quantum 기준으로 누적 사용량 측정)해야 이득을 얻을 수 있음 (후속 과제).

**Starvation vs boost (E3, starve workload)**: 정적 정책에서 boost를 끄면 batch가 최대 6849틱 대기. boost 주기 P면 `max_wait ≈ P`로 정확히 상한이 걸리지만, P=100이면 평균 대기 +18%(5381→6343), interactive p95 +42%(30→43), switch +9%. 동적 hybrid는 boost 없이도 `max_wait`가 90틱으로 억제되나 평균 대기 +24%를 지불.

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

**민감도 (E6, 8코어)**: 이 결과는 **비용 가정에 크게 의존**합니다.

- 락 경합·캐시 비용이 모두 0이면 두 구조의 평균 대기는 같고(1946 vs 1961), **global이 interactive 지연 p95에서 오히려 우세**(0.7 vs 12.3): 전역 우선순위 순서가 완벽해서 idle 코어가 없고 높은 우선순위가 즉시 실행됨.
- 마이그레이션 페널티가 3 → 10틱으로 커지면 global 평균 대기가 3192 → 10591로 폭증, per-CPU는 2168 → 2885. 즉 **per-CPU의 이득은 대부분 cache locality(affinity)에서 나옴**.

**Affinity × load balancing (E5, 8코어)**

- `strict` affinity: 마이그레이션 거의 없음(0~190), interactive p95 지연이 가장 낮음(10.5). 하지만 steal/balance가 없으면 imbalance 0.3, makespan 7637로 느림. **idle steal만으로도 makespan 7637→6816(-11%)**.
- `none`(항상 최소 부하 CPU): 마이그레이션 ~2500, 평균 대기 2569로 최악. 균형은 좋지만 locality를 버림.
- `soft`는 중간. **로드밸런싱 주기를 짧게 할수록(400→25) 평균 대기와 interactive 지연이 오히려 소폭 악화**(strict: 1959→2023, p95 12.4→18.3): 과도한 밸런싱은 locality를 해침. 가장 효율적인 조합은 *strict + idle steal* 또는 *soft + 느린 balance*.

## 한계 (정직한 주의사항)

- 합성 workload와 단순화된 비용 모델이며, 실제 커널(CFS/EEVDF, ULE 등)과 직접 비교한 수치가 아닙니다. 절대값이 아니라 **경향과 민감도**로 읽어야 합니다.
- global에서의 락 경합은 `lock_cost·(N-1)` 선형 모델입니다. 락 구현(예: 분할 락, lock-free)에 따라 크게 달라집니다.
- dynamic 정책의 파라미터(`2/(1+p)`, EMA 임계값)는 튜닝하지 않았습니다. hybrid의 악화는 이 파라미터 + demotion 규칙의 결합 결과이며 일반 결론이 아닙니다.
- seed 10회 평균이며 신뢰구간은 계산하지 않았습니다.

## License

MIT
