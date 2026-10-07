"""CPU, memory and filesystem usage from /proc and statvfs."""

import os

from ..sample import Tile
from .util import gb, read, slug


class CpuCollector:
    def __init__(self, cfg):
        self.proc = cfg.proc_root
        self.prev = None

    def collect(self, s):
        fields = (read(f"{self.proc}/stat") or "").split("\n", 1)[0].split()
        if len(fields) < 9 or fields[0] != "cpu":
            return
        ticks = [int(x) for x in fields[1:9]]   # user..steal
        total, idle = sum(ticks), ticks[3] + ticks[4]   # idle + iowait
        if self.prev:
            d_total, d_idle = total - self.prev[0], idle - self.prev[1]
            if d_total > 0:
                s.set("cpu_usage", 100.0 * (d_total - d_idle) / d_total)
        self.prev = (total, idle)

        s.tile(Tile("cpu_usage", "usage", "CPU", "%", min=0, max=100,
                    warn=80, hot=95, order=0))
        load = (read(f"{self.proc}/loadavg") or "").split()
        if len(load) >= 3:
            s.note("cpu_usage", f"load {load[0]} · {load[1]} · {load[2]}")
        uptime = (read(f"{self.proc}/uptime") or "").split()
        if uptime:
            s.info["uptime"] = float(uptime[0])


class MemoryCollector:
    def __init__(self, cfg):
        self.proc = cfg.proc_root

    def collect(self, s):
        info = {}
        for line in (read(f"{self.proc}/meminfo") or "").split("\n"):
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                info[key] = int(parts[0]) * 1024   # kB -> bytes
        total = info.get("MemTotal")
        if not total:
            return
        used = total - info.get("MemAvailable", info.get("MemFree", 0))
        swap = info.get("SwapTotal", 0) - info.get("SwapFree", 0)

        s.tile(Tile("ram", "usage", "Memory", "%", min=0, max=100,
                    warn=85, hot=95, order=1))
        s.set("ram", 100.0 * used / total)
        note = f"{gb(used):.1f} / {gb(total):.1f} GB"
        if swap > 50e6:
            note += f" · swap {gb(swap):.1f} GB"
        s.note("ram", note)


class DiskSpaceCollector:
    """Filesystem fill level for each configured path (default: "/").

    Inside a container "/" is the overlay filesystem, which lives on the
    host disk that holds Docker's data, so it reports that disk.
    """

    def __init__(self, cfg):
        self.paths = cfg.disk_paths

    def collect(self, s):
        for i, (label, path) in enumerate(self.paths.items()):
            try:
                st = os.statvfs(path)
            except OSError:
                continue
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
            avail = st.f_bavail * st.f_frsize
            if used + avail <= 0:
                continue
            tid = "space_" + slug(label)
            s.tile(Tile(tid, "usage", f"Disk space · {label}", "%", min=0,
                        max=100, warn=80, hot=90, order=20 + i))
            s.set(tid, 100.0 * used / (used + avail))
            s.note(tid, f"{gb(used):.0f} / {gb(used + avail):.0f} GB")
