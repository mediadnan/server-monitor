"""Power: measured where the hardware allows it, modelled for the rest.

CPU   RAPL package energy counters (/sys/class/powercap/intel-rapl:*),
      available on Intel and on AMD Zen with recent kernels. They need root to
      read. If they are missing, CPU power is estimated from CPU load
      between CPU_IDLE_W and CPU_MAX_W.
GPU   reported by the card (nvidia-smi or amdgpu hwmon), see the GPU
      collectors.
Rest  BASE_W: motherboard, RAM, drives, fans, VRM losses. A constant.
Wall  (CPU + GPU + rest) / PSU efficiency, where the efficiency can follow
      a load curve (PSU_EFF + PSU_WATTS).

Must run after the collectors that fill in cpu_usage and Sample.power.
"""

import glob
import os

from ..sample import Tile
from .util import read, read_num


class RaplReader:
    """CPU package power from RAPL energy counters (summed over sockets)."""

    def __init__(self, sys_root):
        self.domains = []
        for path in sorted(glob.glob(f"{sys_root}/class/powercap/intel-rapl:*")):
            # Top-level domains only (intel-rapl:0, not intel-rapl:0:0).
            name = os.path.basename(path)
            label = read(f"{path}/name") or ""
            if name.count(":") == 1 and label.startswith("package"):
                self.domains.append(path)
        self.prev = {}

    def read(self, ts):
        """Watts, or None when the counters are unavailable/unreadable."""
        total, ok = 0.0, False
        for path in self.domains:
            uj = read_num(f"{path}/energy_uj")
            if uj is None:
                return None   # typically: not running as root
            prev = self.prev.get(path)
            self.prev[path] = (ts, uj)
            if not prev or ts <= prev[0]:
                continue
            delta = uj - prev[1]
            if delta < 0:   # counter wrapped
                delta += read_num(f"{path}/max_energy_range_uj") or 0
            total += delta / 1e6 / (ts - prev[0])
            ok = True
        return total if ok else None


class PowerCollector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.rapl = RaplReader(cfg.sys_root)

    def collect(self, s):
        cfg = self.cfg
        cpu_w = self.rapl.read(s.ts) if self.rapl.domains else None
        measured = cpu_w is not None
        if not measured:
            usage = s.values.get("cpu_usage")
            if usage is None:
                return
            cpu_w = cfg.cpu_idle_w + (cfg.cpu_max_w - cfg.cpu_idle_w) * usage / 100.0

        gpu_w = sum(w for k, w in s.power.items() if k.startswith("gpu:"))
        dc_w = cpu_w + gpu_w + cfg.base_w
        eff = cfg.psu_efficiency(dc_w)
        wall_w = dc_w / eff

        s.tile(Tile("power_wall", "energy", "System at the wall", "W", zero=True,
                    span=20, source="estimated", order=0))
        s.set("power_wall", wall_w)
        parts = [f"CPU {cpu_w:.0f}"]
        if gpu_w:
            parts.append(f"GPU {gpu_w:.0f}")
        parts += [f"rest {cfg.base_w:.0f}", f"PSU loss {wall_w - dc_w:.0f} W"]
        s.note("power_wall", " · ".join(parts))

        s.tile(Tile("power_cpu", "energy", "CPU package", "W", decimals=1,
                    zero=True, span=10, order=1,
                    source="measured" if measured else "estimated"))
        s.set("power_cpu", cpu_w)
        s.note("power_cpu", "RAPL counter" if measured else "from CPU load")
