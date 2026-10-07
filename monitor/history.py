"""In-memory history in two resolutions.

fast: every sample, last 30 minutes   (900 points at 2 s)
slow: averages of 15 samples, last 24 h (2880 points at 30 s)

Not thread-safe on its own; the Monitor guards it with a lock.
"""

import json
import os
import time
from collections import deque

FAST_SECONDS = 30 * 60
SLOW_SECONDS = 24 * 3600
SLOW_EVERY = 15


class History:
    def __init__(self, interval):
        self.interval = interval
        self.fast = deque(maxlen=int(FAST_SECONDS / interval))
        self.slow = deque(maxlen=int(SLOW_SECONDS / (interval * SLOW_EVERY)))
        self.pending = []

    def add(self, ts, values):
        """Store a sample. Returns True when a slow point was produced."""
        self.fast.append((ts, values))
        self.pending.append(values)
        if len(self.pending) < SLOW_EVERY:
            return False
        self.slow.append((ts, average(self.pending)))
        self.pending = []
        return True

    def rows(self, tier):
        return list(self.slow if tier == "slow" else self.fast)

    def integrate(self, key, window):
        """Energy-style integral of `key` over the last `window` seconds of
        the slow tier: returns (value * hours, seconds covered)."""
        if not self.slow:
            return 0.0, 0.0
        step = self.interval * SLOW_EVERY
        end = self.slow[-1][0]
        total = covered = 0.0
        prev_ts = None
        for ts, values in self.slow:
            if ts <= end - window:
                prev_ts = ts
                continue
            # Each slow point is the average of the `step` seconds before it.
            dt = step if prev_ts is None else min(ts - prev_ts, step)
            prev_ts = ts
            v = values.get(key)
            if v is not None:
                total += v * dt
                covered += dt
        return total / 3600.0, covered

    # -- persistence ----------------------------------------------------------

    def snapshot(self):
        """A copy that can be written to disk without holding the lock."""
        return {"version": 1, "interval": self.interval,
                "fast": list(self.fast), "slow": list(self.slow)}

    @staticmethod
    def save(snapshot, path):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, path)

    def load(self, path):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        if data.get("version") != 1 or data.get("interval") != self.interval:
            return False
        now = time.time()
        for name, tier, keep in (("fast", self.fast, FAST_SECONDS),
                                 ("slow", self.slow, SLOW_SECONDS)):
            for ts, values in data.get(name, []):
                if now - ts <= keep:
                    tier.append((ts, values))
        return True


def average(samples):
    sums, counts = {}, {}
    for values in samples:
        for k, v in values.items():
            if v is not None:
                sums[k] = sums.get(k, 0.0) + v
                counts[k] = counts.get(k, 0) + 1
    return {k: round(sums[k] / counts[k], 2) for k in sums}


def columns(rows):
    """[(ts, {k: v})] -> {"t": [...], "series": {k: [...]}} for the UI."""
    keys = set()
    for _, values in rows:
        keys.update(values)
    return {"t": [round(ts, 1) for ts, _ in rows],
            "series": {k: [values.get(k) for _, values in rows] for k in keys}}
