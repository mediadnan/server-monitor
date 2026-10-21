"""CPU, memory and filesystem usage from /proc and statvfs."""

import os
import re

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


def _unescape(field):
    """mountinfo escapes space, tab, newline and backslash as \\ooo."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text):
    """All mounts from a mountinfo file, in mount order.

    Returns dicts with dev ("major:minor"), root (path inside the
    filesystem, "/" unless it is a bind mount or subvolume), mountpoint,
    fstype and source.
    """
    out = []
    for line in (text or "").split("\n"):
        fields = line.split()
        if "-" not in fields:
            continue
        sep = fields.index("-")
        if sep < 5 or len(fields) < sep + 3:
            continue
        out.append({"dev": fields[2], "root": _unescape(fields[3]),
                    "mountpoint": _unescape(fields[4]),
                    "fstype": fields[sep + 1], "source": _unescape(fields[sep + 2])})
    return out


class DiskSpaceCollector:
    """Filesystem fill level per disk.

    DISK_PATHS lists paths inside the container (default: "/", the overlay
    filesystem, which lives on the disk that holds Docker's data).

    With HOST_ROOT set, the host's mount table is read as well and every
    block-device filesystem gets a tile: one per device, so bind mounts and
    subvolumes of a disk that is already shown are skipped, as are loop
    devices (snaps) and mountpoints listed in DISK_EXCLUDE. The note names
    the physical disk(s) behind each filesystem, through partitions, LUKS,
    LVM and md RAID.
    """

    SKIP_FS = {"squashfs", "iso9660", "udf"}

    def __init__(self, cfg):
        self.paths = cfg.disk_paths
        self.host = cfg.host_root.rstrip("/")   # "" when HOST_ROOT is "/"
        self.enabled = bool(cfg.host_root)
        self.sys = cfg.sys_root
        self.labels = cfg.disk_labels
        self.exclude = set(cfg.disk_exclude)
        self._disk_cache = {}

    def collect(self, s):
        entries = [(label, path, "space_" + slug(label), "")
                   for label, path in self.paths.items()]
        if self.enabled:
            entries += self._host_entries()
        used_ids = set()
        for i, (label, path, tid, disks) in enumerate(entries):
            try:
                st = os.statvfs(path)
            except OSError:
                continue
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
            avail = st.f_bavail * st.f_frsize
            if used + avail <= 0:
                continue
            base, n = tid, 2
            while tid in used_ids:
                tid, n = f"{base}_{n}", n + 1
            used_ids.add(tid)
            s.tile(Tile(tid, "usage", f"Disk space · {label}", "%", min=0,
                        max=100, warn=80, hot=90, chart="bar", order=20 + i))
            s.set(tid, 100.0 * used / (used + avail))
            dec = 1 if gb(used + avail) < 10 else 0   # small /boot: "0.4 / 2.0 GB"
            note = f"{gb(used):.{dec}f} / {gb(used + avail):.{dec}f} GB"
            s.note(tid, f"{note} · {disks}" if disks else note)

    def _host_entries(self):
        """(label, path in container, tile id, disk names) per host disk."""
        mounts = parse_mountinfo(read(f"{self.host}/proc/1/mountinfo"))
        if not mounts:
            # Mount table not readable: at least show the host's "/".
            return [(self.labels.get("/", "System"), self.host or "/",
                     "space_system", "")]

        # Group mounts by device, keeping the order the host mounted them.
        by_source = {}
        for m in mounts:
            src = m["source"]
            if (not src.startswith("/dev/") or src.startswith("/dev/loop")
                    or m["fstype"] in self.SKIP_FS
                    or m["mountpoint"] in self.exclude):
                continue
            by_source.setdefault(src, []).append(m)

        found = []
        for src, group in by_source.items():
            # Prefer a labelled mountpoint, then the filesystem's own root
            # over bind mounts or subvolumes, then whichever came first.
            group = sorted(group, key=lambda m: (m["mountpoint"] not in self.labels,
                                                 m["root"] != "/"))
            for m in group:
                path = self._visible(m)
                if path:
                    found.append((m, path))
                    break

        # Host "/" first, then labelled disks in DISK_LABELS order, then the rest.
        order = list(self.labels)
        found.sort(key=lambda f: (f[0]["mountpoint"] != "/",
                                  order.index(f[0]["mountpoint"])
                                  if f[0]["mountpoint"] in order else len(order),
                                  f[0]["mountpoint"]))
        out = []
        for m, path in found:
            mp = m["mountpoint"]
            if mp == "/":
                label, tid = self.labels.get("/", "System"), "space_system"
            else:
                label = self.labels.get(mp) or os.path.basename(mp)
                tid = "space_" + slug(label)
            out.append((label, path, tid, ", ".join(self._disks_for(m))))
        return out

    def _visible(self, m):
        """Path of the mount inside the container, or None.

        A mount that appeared on the host after the container started (and
        without rslave propagation) is not there: the path then belongs to
        the parent filesystem, whose numbers would be wrong.
        """
        path = self.host + (m["mountpoint"] if m["mountpoint"] != "/" else "") or "/"
        try:
            st = os.stat(path)
        except OSError:
            return None
        return path if f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}" == m["dev"] else None

    def _disks_for(self, m):
        key = (m["source"], m["dev"])
        if key not in self._disk_cache:
            name = self._block_name(m)
            self._disk_cache[key] = self._physical(name) if name else []
        return self._disk_cache[key]

    def _block_name(self, m):
        """Kernel name (sda2, dm-0, ...) of a mount's source device."""
        candidates = [os.path.realpath(f"{self.sys}/dev/block/{m['dev']}"),
                      os.path.realpath(f"{self.host}{m['source']}"),
                      m["source"]]
        for c in candidates:
            name = os.path.basename(c)
            if name and os.path.exists(f"{self.sys}/class/block/{name}"):
                return name
        if m["source"].startswith("/dev/mapper/"):   # host /dev not visible
            want = m["source"][len("/dev/mapper/"):]
            block = f"{self.sys}/class/block"
            for name in sorted(os.listdir(block) if os.path.isdir(block) else []):
                if read(f"{block}/{name}/dm/name") == want:
                    return name
        return None

    def _physical(self, name, depth=0):
        """Whole disks under a block device: partition -> its disk,
        dm (LUKS, LVM) and md -> their members, recursively."""
        base = f"{self.sys}/class/block/{name}"
        if depth > 8 or not os.path.exists(base):
            return []
        try:
            slaves = sorted(os.listdir(f"{base}/slaves"))
        except OSError:
            slaves = []
        if slaves:
            out = []
            for slave in slaves:
                for disk in self._physical(slave, depth + 1):
                    if disk not in out:
                        out.append(disk)
            return out
        if os.path.exists(f"{base}/partition"):
            return [os.path.basename(os.path.dirname(os.path.realpath(base)))]
        return [name]
