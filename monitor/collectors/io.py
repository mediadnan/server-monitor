"""Disk and network throughput, computed from counter deltas."""

import os
import re

from ..sample import Tile
from .util import fmt_bytes, read, read_num, slug

# Whole disks only; partitions (nvme0n1p1, sda1) would double count.
DISK_RE = re.compile(r"nvme\d+n\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+|mmcblk\d+")
SECTOR = 512   # /proc/diskstats always counts 512-byte sectors


class DiskIOCollector:
    def __init__(self, cfg):
        self.proc = cfg.proc_root
        self.only = set(cfg.disks)
        self.prev = {}
        self.prev_ts = None

    def _wanted(self, name):
        return name in self.only if self.only else DISK_RE.fullmatch(name)

    def collect(self, s):
        cur = {}
        for line in (read(f"{self.proc}/diskstats") or "").split("\n"):
            f = line.split()
            if len(f) < 13 or not self._wanted(f[2]):
                continue
            read_b, write_b = int(f[5]) * SECTOR, int(f[9]) * SECTOR
            if read_b == 0 and write_b == 0:
                continue   # empty card reader slots and the like
            cur[f[2]] = (read_b, write_b, int(f[12]))   # f[12]: ms busy

        dt = s.ts - self.prev_ts if self.prev_ts else 0
        for i, (name, (r, w, busy)) in enumerate(sorted(cur.items())):
            tid = "disk_" + slug(name)
            s.tile(Tile(tid, "io", f"Disk {name}", "B/s", format="rate",
                        series=[[tid + "_r", "read"], [tid + "_w", "write"]],
                        zero=True, span=1e6, order=i))
            prev = self.prev.get(name)
            if prev and dt > 0 and r >= prev[0] and w >= prev[1]:
                s.set(tid + "_r", (r - prev[0]) / dt, 0)
                s.set(tid + "_w", (w - prev[1]) / dt, 0)
                util = min(100.0, max(0.0, (busy - prev[2]) / (dt * 10.0)))
                s.note(tid, f"busy {util:.0f}%")
        self.prev, self.prev_ts = cur, s.ts


class NetworkCollector:
    """Per-interface traffic, read from the host's /sys/class/net.

    Because /sys is bind-mounted from the host, this sees the host's
    interfaces rather than the container's. By default only physical
    interfaces are shown (those backed by a real device), which skips
    loopback, docker bridges, veth pairs and VPN tunnels.
    """

    def __init__(self, cfg):
        self.base = f"{cfg.sys_root}/class/net"
        self.only = cfg.net_ifaces
        self.prev = {}
        self.prev_ts = None

    def _interfaces(self):
        if self.only:
            return self.only
        try:
            names = sorted(os.listdir(self.base))
        except OSError:
            return []
        return [n for n in names if os.path.exists(f"{self.base}/{n}/device")]

    def collect(self, s):
        cur = {}
        for name in self._interfaces():
            stats = f"{self.base}/{name}/statistics"
            rx, tx = read_num(f"{stats}/rx_bytes"), read_num(f"{stats}/tx_bytes")
            if rx is None or tx is None:
                continue
            up = read(f"{self.base}/{name}/operstate") == "up"
            if up or rx > 0 or name in self.only:
                cur[name] = (rx, tx)

        dt = s.ts - self.prev_ts if self.prev_ts else 0
        for i, (name, (rx, tx)) in enumerate(cur.items()):
            tid = "net_" + slug(name)
            s.tile(Tile(tid, "io", f"Network {name}", "B/s", format="rate",
                        series=[[tid + "_rx", "down"], [tid + "_tx", "up"]],
                        zero=True, span=1e5, order=10 + i))
            prev = self.prev.get(name)
            if prev and dt > 0 and rx >= prev[0] and tx >= prev[1]:
                s.set(tid + "_rx", (rx - prev[0]) / dt, 0)
                s.set(tid + "_tx", (tx - prev[1]) / dt, 0)
            s.note(tid, f"since boot ↓ {fmt_bytes(rx)} ↑ {fmt_bytes(tx)}")
        self.prev, self.prev_ts = cur, s.ts
