"""NVIDIA GPUs via nvidia-smi.

nvidia-smi is not part of the image: the NVIDIA container runtime mounts
it in when the container has the "utility" driver capability. Without it
this collector simply does nothing.
"""

import logging
import shutil
import subprocess

from ..sample import Tile
from .util import num

log = logging.getLogger(__name__)

FIELDS = ("index", "utilization.gpu", "memory.used", "memory.total",
          "temperature.gpu", "power.draw", "power.limit", "fan.speed")


class NvidiaCollector:
    def __init__(self, cfg):
        self.binary = shutil.which("nvidia-smi")
        self.failures = 0
        self.calls = 0

    def _query(self):
        out = subprocess.run(
            [self.binary, "--query-gpu=" + ",".join(FIELDS),
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, check=True).stdout
        rows = []
        for line in out.strip().split("\n"):
            parts = [num(x.strip()) for x in line.split(",")]
            if len(parts) == len(FIELDS):
                rows.append(dict(zip(FIELDS, parts)))
        return rows

    def collect(self, s):
        if not self.binary:
            return
        # After repeated failures, only retry every 30th sample.
        self.calls += 1
        if self.failures >= 3 and self.calls % 30:
            return
        try:
            rows = self._query()
            self.failures = 0
        except (OSError, subprocess.SubprocessError) as e:
            self.failures += 1
            if self.failures == 3:
                log.warning("nvidia-smi keeps failing (%s), backing off", e)
            return

        single = len(rows) == 1
        for row in rows:
            i = int(row["index"] or 0)
            name = "GPU" if single else f"GPU {i}"
            k = f"gpu{i}"

            s.tile(Tile(f"{k}_util", "usage", name, "%", min=0, max=100, order=10 + i))
            s.set(f"{k}_util", row["utilization.gpu"])

            used, total = row["memory.used"], row["memory.total"]
            if used is not None and total:
                s.tile(Tile(f"{k}_vram", "usage", f"{name} memory", "%", min=0,
                            max=100, warn=90, hot=98, order=11 + i))
                s.set(f"{k}_vram", 100.0 * used / total)
                s.note(f"{k}_vram", f"{used / 1024:.1f} / {total / 1024:.1f} GB")

            if row["temperature.gpu"] is not None:
                s.tile(Tile(f"temp_{k}", "temps", name, "°C", warn=80, hot=88,
                            span=10, order=10 + i))
                s.set(f"temp_{k}", row["temperature.gpu"])

            watts = row["power.draw"]
            if watts is not None:
                s.power[f"gpu:{k}"] = watts
                s.tile(Tile(f"power_{k}", "energy", name, "W", decimals=1,
                            zero=True, span=10, source="measured", order=10 + i))
                s.set(f"power_{k}", watts)
                if row["power.limit"]:
                    s.note(f"power_{k}", f"limit {row['power.limit']:.0f} W")

            if row["fan.speed"] is not None:
                s.tile(Tile(f"fan_{k}", "fans", f"{name} fan", "%", min=0,
                            max=100, order=10 + i))
                s.set(f"fan_{k}", row["fan.speed"])
