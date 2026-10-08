"""MLFQ와 time-quantum 정책 (정적 / 큐 상태 기반 / 피드백 기반 / 혼합)."""
from __future__ import annotations

from collections import deque


class MLFQ:
    """다단계 피드백 큐. 레벨 0이 가장 높은 우선순위."""

    def __init__(self, levels: int, policy: "QuantumPolicy", cpus_served: int = 1):
        self.levels = levels
        self.policy = policy
        self.cpus_served = cpus_served
        self.queues = [deque() for _ in range(levels)]
        self.n = 0

    def __len__(self) -> int:
        return self.n

    def push(self, task, front: bool = False) -> None:
        q = self.queues[task.level]
        q.appendleft(task) if front else q.append(task)
        self.n += 1

    def pop(self):
        for q in self.queues:
            if q:
                self.n -= 1
                return q.popleft()
        return None

    def top_level(self) -> int:
        for i, q in enumerate(self.queues):
            if q:
                return i
        return self.levels  # 비어 있음

    def steal(self):
        """가장 낮은 우선순위 큐의 꼬리에서 하나 꺼낸다 (로드밸런싱용)."""
        for q in reversed(self.queues):
            if q:
                self.n -= 1
                return q.pop()
        return None

    def boost(self) -> None:
        """priority boost: 모든 대기 task를 레벨 0으로."""
        q0 = self.queues[0]
        for q in self.queues[1:]:
            while q:
                t = q.popleft()
                t.level = 0
                q0.append(t)

    def quantum(self, level: int) -> int:
        return max(1, int(round(self.policy.scale(level, self) * self.policy.base_q(level))))


class QuantumPolicy:
    """정적 정책: q[l] = base * growth**l."""

    name = "static"

    def __init__(self, base: int = 4, levels: int = 4, growth: int = 2):
        self.q = [base * growth**i for i in range(levels)]

    def base_q(self, level: int) -> int:
        return self.q[level]

    def scale(self, level: int, mlfq: MLFQ) -> float:
        return 1.0

    def observe(self, level: int, expired: bool) -> None:
        pass


class LoadPolicy(QuantumPolicy):
    """큐 상태 기반: 대기 압력 p = (대기 task 수 / 담당 CPU 수).
    scale = clamp(2/(1+p), lo, hi) -> 한가하면 quantum을 키워 context switch를 줄이고,
    붐비면 줄여 응답시간/대기시간을 낮춘다."""

    name = "load"

    def __init__(self, base=4, levels=4, growth=2, lo=0.25, hi=2.0):
        super().__init__(base, levels, growth)
        self.lo, self.hi = lo, hi

    def scale(self, level, mlfq):
        p = mlfq.n / mlfq.cpus_served
        return min(self.hi, max(self.lo, 2.0 / (1.0 + p)))


class FeedbackPolicy(QuantumPolicy):
    """workload 기반: 레벨별로 'quantum을 다 쓴 비율'의 EMA를 추적.
    대부분 다 쓰면(CPU-bound) quantum을 키우고, 대부분 일찍 반납하면(I/O-bound) 줄인다."""

    name = "feedback"

    def __init__(self, base=4, levels=4, growth=2, lo=0.5, hi=4.0):
        super().__init__(base, levels, growth)
        self.ema = [0.5] * levels
        self.s = [1.0] * levels
        self.lo, self.hi = lo, hi

    def scale(self, level, mlfq):
        return self.s[level]

    def observe(self, level, expired):
        self.ema[level] = 0.9 * self.ema[level] + 0.1 * (1.0 if expired else 0.0)
        if self.ema[level] > 0.75:
            self.s[level] = min(self.hi, self.s[level] * 1.05)
        elif self.ema[level] < 0.35:
            self.s[level] = max(self.lo, self.s[level] * 0.95)


class HybridPolicy(FeedbackPolicy):
    """workload(피드백) x queue state(load)."""

    name = "hybrid"

    def __init__(self, base=4, levels=4, growth=2):
        super().__init__(base, levels, growth)
        self._load = LoadPolicy(base, levels, growth)

    def scale(self, level, mlfq):
        return self.s[level] * self._load.scale(level, mlfq)


def make_policy(name: str, base: int = 4, levels: int = 4) -> QuantumPolicy:
    return {"static": QuantumPolicy, "load": LoadPolicy,
            "feedback": FeedbackPolicy, "hybrid": HybridPolicy}[name](base, levels)
