"""Temperatures, fans and AMD GPU power from /sys/class/hwmon.

Each hwmon device exposes a driver name ("k10temp", "nvme", "nct6798",
...) and numbered channels (temp1_input, fan2_input, ...). Known drivers
are mapped to friendly labels; anything else can be added through the
EXTRA_TEMPS and FAN_LABELS settings.
"""

import glob
import os
import re

from ..sample import Tile
from .util import read, read_num, slug

CPU_DRIVERS = ("k10temp", "coretemp", "zenpower", "cpu_thermal")
# Super I/O chips on desktop/server motherboards: they carry the fan headers.
BOARD_RE = re.compile(r"nct\d+|it\d+|f71\d+|w83\d+|asus.*|dell_smm|thinkpad")

THRESHOLDS = {           # (warn, hot) in °C
    "cpu": (80, 90),
    "gpu": (80, 88),
    "nvme": (65, 75),
    "disk": (50, 60),
    "board": (55, 65),
    "other": (70, 85),
}


def channel_labels(path, kind):
    """{'temp1': 'Tctl', ...} for the channels of one kind that have labels."""
    out = {}
    for f in glob.glob(f"{path}/{kind}*_label"):
        ch = os.path.basename(f)[:-len("_label")]
        out[ch] = read(f) or ""
    return out


def channels(path, kind):
    """Sorted channel names like ['fan1', 'fan2'] that have an _input file."""
    found = [os.path.basename(f)[:-len("_input")]
             for f in glob.glob(f"{path}/{kind}*_input")]
    return sorted(found, key=lambda c: int(re.sub(r"\D", "", c) or 0))


class HwmonCollector:
    def __init__(self, cfg):
        self.base = f"{cfg.sys_root}/class/hwmon"
        self.fan_labels = cfg.fan_labels
        self.extra_temps = cfg.extra_temps
        self.fans_seen = set()   # fan channels that ever reported > 0 rpm

    def collect(self, s):
        devices = []
        for path in sorted(glob.glob(f"{self.base}/hwmon*")):
            devices.append((read(f"{path}/name") or "", path))

        cpu_count = sum(1 for name, _ in devices if name in CPU_DRIVERS)
        cpu_n = 0
        for name, path in devices:
            if name in CPU_DRIVERS:
                cpu_n += 1
                label = "CPU" if cpu_count == 1 else f"CPU {cpu_n}"
                self._temp(s, "cpu" + ("" if cpu_count == 1 else str(cpu_n)),
                           label, self._cpu_temp(name, path), "cpu", order=0)
            elif name == "nvme":
                dev = self._device_name(path, "nvme")
                self._temp(s, "nvme_" + slug(dev), f"NVMe {dev}",
                           read_num(f"{path}/temp1_input"), "nvme", order=20)
            elif name == "drivetemp":
                dev = self._device_name(path, "drive")
                self._temp(s, "drive_" + slug(dev), f"Disk {dev}",
                           read_num(f"{path}/temp1_input"), "disk", order=25)
            elif name == "amdgpu":
                self._amdgpu(s, path)
            elif BOARD_RE.fullmatch(name):
                labels = channel_labels(path, "temp")
                for ch, lbl in labels.items():
                    if lbl.upper() == "SYSTIN":
                        self._temp(s, "board", "Motherboard",
                                   read_num(f"{path}/{ch}_input"), "board", order=30)
            self._fans(s, name, path)

        self._extra_temps(s, devices)

    # -- temperatures -----------------------------------------------------

    @staticmethod
    def _cpu_temp(driver, path):
        labels = channel_labels(path, "temp")
        by_label = {v: k for k, v in labels.items()}
        if driver == "coretemp":
            pkg = [k for k, v in labels.items() if v.startswith("Package")]
            ch = pkg[0] if pkg else "temp1"
        else:
            # k10temp: Tdie is the real die temperature on CPUs where Tctl
            # carries a fan-control offset; otherwise Tctl is all we have.
            ch = by_label.get("Tdie") or by_label.get("Tctl") or "temp1"
        return read_num(f"{path}/{ch}_input")

    @staticmethod
    def _device_name(path, fallback):
        """Kernel name of the device behind a hwmon node (nvme0, sda)."""
        dev = os.path.realpath(f"{path}/device")
        for pattern in ("block/*", "nvme/nvme*"):
            found = sorted(glob.glob(f"{dev}/{pattern}"))
            if found:
                return os.path.basename(found[0])
        base = os.path.basename(dev)
        return base if re.fullmatch(r"[a-z]+\d+", base) else fallback

    @staticmethod
    def _temp(s, key, label, millideg, kind, order=50):
        if millideg is None:
            return
        value = millideg / 1000.0
        if value <= -40 or value >= 150:
            return   # unconnected sensor inputs report junk like -128 or 255
        warn, hot = THRESHOLDS[kind]
        tid = "temp_" + key
        s.tile(Tile(tid, "temps", label, "°C", warn=warn, hot=hot, span=10,
                    order=order))
        s.set(tid, value)

    def _extra_temps(self, s, devices):
        for i, (label, ref) in enumerate(self.extra_temps.items()):
            chip, _, ch = ref.partition("/")
            for name, path in devices:
                if name == chip:
                    self._temp(s, "x_" + slug(label), label,
                               read_num(f"{path}/{ch or 'temp1'}_input"),
                               "other", order=40 + i)
                    break

    # -- AMD GPU ------------------------------------------------------------

    def _amdgpu(self, s, path):
        dev = os.path.basename(os.path.realpath(f"{path}/device"))
        key = "amdgpu_" + slug(dev)
        labels = {v: k for k, v in channel_labels(path, "temp").items()}
        ch = labels.get("edge", "temp1")
        self._temp(s, key, "GPU (AMD)", read_num(f"{path}/{ch}_input"),
                   "gpu", order=10)
        uw = read_num(f"{path}/power1_average") or read_num(f"{path}/power1_input")
        if uw is not None:
            s.power["gpu:" + key] = uw / 1e6
            tid = "power_" + key
            s.tile(Tile(tid, "energy", "GPU (AMD)", "W", decimals=1, zero=True,
                        span=10, source="measured", order=10))
            s.set(tid, uw / 1e6)

    # -- fans -------------------------------------------------------------

    def _fans(self, s, chip, path):
        labels = channel_labels(path, "fan")
        for ch in channels(path, "fan"):
            rpm = read_num(f"{path}/{ch}_input")
            if rpm is None:
                continue
            ident = f"{chip}/{ch}"
            label = self.fan_labels.get(ident) or self.fan_labels.get(ch)
            if self.fan_labels and not label:
                continue   # an explicit FAN_LABELS list hides everything else
            if not label:
                # Auto mode: unused headers read 0 forever, so only show a
                # channel once it has spun at least once.
                if rpm > 0:
                    self.fans_seen.add(ident)
                if ident not in self.fans_seen:
                    continue
                label = labels.get(ch) or (
                    "GPU fan (AMD)" if chip == "amdgpu" else f"Fan {ch[3:]}")
            if self.fan_labels:   # keep the order the user listed them in
                key = ident if ident in self.fan_labels else ch
                order = 20 + list(self.fan_labels).index(key)
            else:
                order = 10 if chip == "amdgpu" else 20
            tid = "fan_" + slug(f"{chip}_{ch}")
            s.tile(Tile(tid, "fans", label, "rpm", zero=True, span=300, low=0,
                        order=order))
            s.set(tid, rpm, 0)
