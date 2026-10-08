"""재현 가능한(seed 고정) workload 생성기."""
from __future__ import annotations

import random

from .sim import Task

KINDS = ("interactive", "mixed", "batch", "bursty", "starve")


def _interactive(rng, tid, arrival, nb=None, io=(8, 30)):
    nb = nb or rng.randint(40, 80)
    bursts = []
    for i in range(nb):
        bursts.append(rng.randint(1, 4))
        if i < nb - 1:
            bursts.append(rng.randint(*io))
    return Task(tid, arrival, "interactive", bursts)


def _batch(rng, tid, arrival, total=None):
    total = total or rng.randint(400, 1200)
    bursts, left = [], total
    while left > 0:
        b = min(left, rng.randint(80, 300))
        bursts.append(b)
        left -= b
        if left > 0:
            bursts.append(rng.randint(1, 6))
    return Task(tid, arrival, "batch", bursts)


def make_workload(kind: str, ncpu: int, seed: int, per_cpu: int = 12, spread: int = 2000):
    rng = random.Random(seed)
    tasks, tid = [], 0
    if kind == "starve":
        # 상시 포화 상태의 interactive 스트림 + 소수 batch
        for _ in range(10 * ncpu):
            tasks.append(_interactive(rng, tid, rng.randint(0, 200), nb=200, io=(4, 12)))
            tid += 1
        for _ in range(3 * ncpu):
            tasks.append(_batch(rng, tid, rng.randint(100, 300), total=600))
            tid += 1
        return tasks
    frac = {"interactive": 0.8, "mixed": 0.5, "batch": 0.2, "bursty": 0.5}[kind]
    n = per_cpu * ncpu
    waves = [0, 600, 1200] if kind == "bursty" else None
    for _ in range(n):
        if waves:
            arrival = rng.choice(waves) + rng.randint(0, 20)
        else:
            arrival = rng.randint(0, spread)
        if rng.random() < frac:
            tasks.append(_interactive(rng, tid, arrival))
        else:
            tasks.append(_batch(rng, tid, arrival))
        tid += 1
    return tasks
