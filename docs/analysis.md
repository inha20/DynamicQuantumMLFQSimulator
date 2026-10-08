# 분석 노트

수치는 모두 [results/summary.md](../results/summary.md)(seed 10회 평균)에서 가져왔습니다.

## 1. 지표 간 관계 (이론 → 측정)

| 변화 | 대기시간 | 응답/interactive 지연 | context switch | starvation |
|---|---|---|---|---|
| quantum ↑ | U자 (q=16 근처 최저) | 악화 (p95 3.8→100) | 감소 (105→86/kt) | 단일 task 대기 증가 경향 |
| quantum ↓ | switch 비용으로 악화 | 개선 | 증가 (105→153/kt) | `max_wait` 감소 (697→265) |
| boost 주기 ↓ | 악화 (+18%) | 악화 (+42%) | 증가 | `max_wait ≈ 주기`로 상한 |
| queue-state 동적(load) | +6~7% | 응답 개선, p95 악화 | 증가 | starved 0 |
| feedback 동적 | 동률~소폭 개선 | 응답 개선 | 감소/동률 | 변화 없음 |

핵심: 어떤 레버도 한 지표만 움직이지 않습니다. quantum 축소는 응답성과 starvation을 개선하지만 switch 비용을 내고, 확대는 그 반대입니다.

## 2. 동적 quantum이 실패하는 지점

`load` 정책은 대기열이 길 때 레벨 0 quantum을 4에서 1~2로 줄입니다. 그런데 interactive task의 CPU burst는 1~4틱이므로 줄어든 quantum을 다 못 쓰고 **demote**됩니다. MLFQ는 "quantum을 다 쓰면 CPU-bound"라고 가정하는데, 동적 quantum이 이 가정을 깨뜨립니다. 결과적으로 hybrid에서 mixed interactive p95 지연이 3.8 → 103으로 악화됐습니다.

개선 방향(검증하지 않음): demotion 판정은 정적 기준 quantum의 누적 사용량으로 하고, 동적 quantum은 "선점 주기"로만 사용.

## 3. Global vs Per-CPU가 갈리는 조건

- **비용 0**: 두 구조의 평균 대기는 동일, global이 우선순위 정확도 덕에 interactive 지연 우세.
- **비용 현실화**: 락 경합 + cache-cold 마이그레이션이 코어 수에 비례해 증가. per-CPU는 이를 피해 코어가 늘어도 평균 대기가 평탄(2139→2157), global은 2배 이상 증가(2840→5725).
- **per-CPU의 대가**: 지역 큐 간 불균형(strict·steal 없음에서 imbalance 0.3), 전역 우선순위 역전(다른 CPU에는 더 높은 우선순위가 대기 중일 수 있음). idle steal이 이를 가장 싸게 해결(makespan -11%).
- **과도한 밸런싱은 역효과**: 주기를 25틱까지 줄이면 locality가 깨져 지연이 증가.

## 4. 재현성

모든 workload는 seed로 고정되며 `tests/test_sim.py`가 결정성, 작업량 보존, 정책 동작(quantum 축소, boost)을 검증합니다.
