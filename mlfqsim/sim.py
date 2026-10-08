"""틱 단위 이산 사건 CPU 스케줄러 시뮬레이터.

mode="global" : 모든 CPU가 하나의 MLFQ를 공유 (락 경합 + 캐시 cold 비용 모델링)
mode="percpu" : CPU마다 MLFQ(runqueue)를 가짐 + affinity + load balancing
"""
from __future__ import annotations

import heapq
import statistics
from dataclasses import dataclass, field

from .policies import MLFQ, make_policy


@dataclass
class Task:
    tid: int
    arrival: int
    kind: str
    bursts: list  # [cpu, io, cpu, io, ..., cpu]
    idx: int = 0
    remaining: int = 0
    level: int = 0
    last_cpu: int = -1
    first_run: int = -1
    finish: int = -1
    ready_since: int = 0
    wait: int = 0
    max_wait: int = 0
    stretches: list = field(default_factory=list)

    def __post_init__(self):
        self.remaining = self.bursts[0]

    @property
    def cpu_total(self) -> int:
        return sum(self.bursts[0::2])


@dataclass
class Cpu:
    run: Task | None = None
    qleft: int = 0
    stall: int = 0
    prev_tid: int = -1
    carry: float = 0.0
    busy: int = 0
    overhead: int = 0
    idle: int = 0


@dataclass
class Config:
    ncpu: int = 1
    mode: str = "global"          # global | percpu
    policy: str = "static"        # static | load | feedback | hybrid
    base_q: int = 4
    levels: int = 4
    boost_period: int | None = 1000
    cs_cost: int = 1              # context switch 시 CPU가 멈추는 틱
    mig_penalty: int = 3          # 다른 CPU로 옮겨 실행될 때 추가 작업량(cache cold)
    lock_cost: float = 0.25       # global 큐: dispatch당 (ncpu-1)*lock_cost 틱 경합 비용
    affinity: str = "soft"        # strict | soft | none (percpu wake 배치)
    soft_slack: int = 2           # soft: 원래 CPU 부하가 최소부하보다 slack 이상 크면 이동
    lb_period: int | None = 100   # 주기적 push 밸런싱 주기 (None=끔)
    idle_steal: bool = True       # idle CPU가 다른 CPU에서 훔쳐오기
    starve_thresh: int = 1000
    max_t: int = 400_000


def run(tasks: list[Task], cfg: Config) -> dict:
    n = cfg.ncpu
    glob = cfg.mode == "global"
    mlfqs = [MLFQ(cfg.levels, make_policy(cfg.policy, cfg.base_q, cfg.levels),
                  cpus_served=n if glob else 1) for _ in range(1 if glob else n)]
    cpus = [Cpu() for _ in range(n)]
    arrivals = sorted(tasks, key=lambda t: t.arrival)
    ai = 0
    wake: list = []
    done = 0
    switches = migrations = 0
    total = len(tasks)

    def mq(c: int) -> MLFQ:
        return mlfqs[0] if glob else mlfqs[c]

    def load(c: int) -> int:
        return len(mlfqs[c]) + (1 if cpus[c].run else 0)

    def place(task: Task, t: int) -> None:
        task.ready_since = t
        if glob:
            mlfqs[0].push(task)
            return
        loads = [load(c) for c in range(n)]
        least = min(range(n), key=loads.__getitem__)
        lc = task.last_cpu
        if lc < 0 or cfg.affinity == "none":
            target = least
        elif cfg.affinity == "strict":
            target = lc
        else:
            target = lc if loads[lc] - loads[least] < cfg.soft_slack else least
        mlfqs[target].push(task)

    def steal_for(c: int):
        best, bl = -1, 0
        for v in range(n):
            if v != c and len(mlfqs[v]) > bl:
                best, bl = v, len(mlfqs[v])
        return mlfqs[best].steal() if best >= 0 else None

    def balance() -> None:
        for _ in range(n):
            ls = [load(c) for c in range(n)]
            b = max(range(n), key=ls.__getitem__)
            l = min(range(n), key=ls.__getitem__)
            if ls[b] - ls[l] < 2 or not len(mlfqs[b]):
                return
            tk = mlfqs[b].steal()
            mlfqs[l].push(tk)

    t = 0
    while done < total and t < cfg.max_t:
        while ai < total and arrivals[ai].arrival <= t:
            place(arrivals[ai], t)
            ai += 1
        while wake and wake[0][0] <= t:
            place(heapq.heappop(wake)[2], t)

        if cfg.boost_period and t > 0 and t % cfg.boost_period == 0:
            for m in mlfqs:
                m.boost()
            for c in cpus:
                if c.run:
                    c.run.level = 0
        if not glob and cfg.lb_period and t > 0 and t % cfg.lb_period == 0:
            balance()

        for ci, c in enumerate(cpus):
            m = mq(ci)
            # 선점: 더 높은 우선순위가 대기 중이면
            if c.run and m.top_level() < c.run.level:
                tk = c.run
                tk.ready_since = t
                m.push(tk, front=True)
                c.run = None
            if c.stall > 0:
                c.stall -= 1
                c.overhead += 1
                continue
            if c.run is None:
                tk = m.pop()
                if tk is None and not glob and cfg.idle_steal:
                    tk = steal_for(ci)
                if tk is None:
                    c.idle += 1
                    continue
                st = t - tk.ready_since
                tk.wait += st
                tk.stretches.append(st)
                tk.max_wait = max(tk.max_wait, st)
                if tk.first_run < 0:
                    tk.first_run = t
                stall = 0
                if tk.tid != c.prev_tid:
                    switches += 1
                    stall += cfg.cs_cost
                if glob and n > 1:
                    c.carry += cfg.lock_cost * (n - 1)
                    k = int(c.carry)
                    c.carry -= k
                    stall += k
                if tk.last_cpu not in (-1, ci):
                    migrations += 1
                    tk.remaining += cfg.mig_penalty
                tk.last_cpu = ci
                c.run = tk
                c.prev_tid = tk.tid
                c.qleft = m.quantum(tk.level)
                if stall > 0:
                    c.stall = stall - 1
                    c.overhead += 1
                    continue
            tk = c.run
            tk.remaining -= 1
            c.qleft -= 1
            c.busy += 1
            if tk.remaining == 0:
                m.policy.observe(tk.level, False)
                tk.idx += 1
                if tk.idx >= len(tk.bursts):
                    tk.finish = t + 1
                    done += 1
                else:
                    io = tk.bursts[tk.idx]
                    tk.idx += 1
                    tk.remaining = tk.bursts[tk.idx]
                    heapq.heappush(wake, (t + 1 + io, tk.tid, tk))
                c.run = None
            elif c.qleft == 0:
                m.policy.observe(tk.level, True)
                tk.level = min(tk.level + 1, cfg.levels - 1)
                tk.ready_since = t + 1
                m.push(tk)
                c.run = None
        t += 1

    return _metrics(tasks, cpus, t, switches, migrations, cfg)


def _p95(xs):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return float(xs[min(len(xs) - 1, int(0.95 * len(xs)))])


def _metrics(tasks, cpus, makespan, switches, migrations, cfg) -> dict:
    inter = [x for t in tasks if t.kind == "interactive" for x in t.stretches]
    batch = [t for t in tasks if t.kind == "batch" and t.finish > 0]
    useful = sum(t.cpu_total for t in tasks)
    cap = cfg.ncpu * makespan
    slow = [(b.finish - b.arrival) / b.cpu_total for b in batch]
    jain = (sum(slow) ** 2 / (len(slow) * sum(s * s for s in slow))) if slow else 1.0
    busy = [c.busy for c in cpus]
    return {
        "avg_wait": statistics.fmean(t.wait for t in tasks),
        "avg_resp": statistics.fmean(t.first_run - t.arrival for t in tasks if t.first_run >= 0),
        "int_lat_mean": statistics.fmean(inter) if inter else 0.0,
        "int_lat_p95": _p95(inter),
        "batch_slowdown": statistics.fmean(slow) if slow else 0.0,
        "max_wait": max(t.max_wait for t in tasks),
        "starved": sum(1 for t in tasks if t.max_wait > cfg.starve_thresh),
        "switches": switches,
        "switch_per_kt": 1000.0 * switches / max(1, makespan),
        "migrations": migrations,
        "util": useful / cap,
        "overhead_frac": sum(c.overhead for c in cpus) / cap,
        "idle_frac": sum(c.idle for c in cpus) / cap,
        "imbalance": (max(busy) - min(busy)) / max(1, max(busy)),
        "makespan": makespan,
        "jain_batch": jain,
        "unfinished": sum(1 for t in tasks if t.finish < 0),
    }
