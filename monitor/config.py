"""Runtime configuration, read once from environment variables.

Everything that is specific to one machine (fan wiring, power figures,
which disks to show, credentials) comes from the environment so the code
itself stays generic. See the README for the full list.
"""

import os
from dataclasses import dataclass, field


def _str(env, name, default=""):
    return env.get(name, default).strip()


def _float(env, name, default):
    raw = _str(env, name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def _list(env, name):
    """'a, b,c' -> ['a', 'b', 'c']"""
    return [x.strip() for x in _str(env, name).split(",") if x.strip()]


def _pairs(env, name):
    """'k1=v1, k2=v2' -> {'k1': 'v1', 'k2': 'v2'} (insertion ordered)."""
    out = {}
    for item in _list(env, name):
        key, sep, value = item.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise ValueError(f"{name}: expected key=value, got {item!r}")
        out[key.strip()] = value.strip()
    return out


def parse_psu_curve(raw):
    """Parse PSU_EFF.

    Either a single efficiency ("0.87") or a load curve
    ("10:0.80,20:0.87,50:0.90,100:0.87") where the left side is percent of
    the PSU's rated output. Returns a sorted list of (load_pct, eff).
    """
    raw = raw.strip()
    if ":" not in raw:
        eff = float(raw)
        if not 0.3 <= eff <= 1.0:
            raise ValueError("PSU_EFF must be between 0.3 and 1.0")
        return [(0.0, eff)]
    points = []
    for item in raw.split(","):
        load, _, eff = item.partition(":")
        points.append((float(load), float(eff)))
    if any(not 0.3 <= e <= 1.0 for _, e in points):
        raise ValueError("PSU_EFF efficiencies must be between 0.3 and 1.0")
    return sorted(points)


@dataclass(frozen=True)
class Config:
    port: int = 8088
    title: str = "Server Monitor"
    interval: float = 2.0

    # Where the host's /sys and /proc are visible inside the container.
    sys_root: str = "/sys"
    proc_root: str = "/proc"

    # HTTP basic auth. Auth is disabled when the password is empty.
    auth_user: str = "admin"
    auth_password: str = ""

    # Optional directory to persist history across restarts.
    data_dir: str = ""

    # Hardware mapping (all optional, auto-detected when empty).
    fan_labels: dict = field(default_factory=dict)    # "fan1" or "chip/fan1" -> label
    extra_temps: dict = field(default_factory=dict)   # label -> "chip/temp2"
    net_ifaces: list = field(default_factory=list)
    disks: list = field(default_factory=list)
    disk_paths: dict = field(default_factory=lambda: {"System": "/"})

    # Host "/" mounted read-only in the container. When set, every mounted
    # disk of the host gets a disk-space tile.
    host_root: str = ""
    disk_labels: dict = field(default_factory=dict)   # host mountpoint -> label
    disk_exclude: list = field(default_factory=list)  # host mountpoints to skip

    # Power model.
    cpu_idle_w: float = 10.0
    cpu_max_w: float = 65.0
    base_w: float = 20.0
    psu_watts: float = 0.0
    psu_curve: list = field(default_factory=lambda: [(0.0, 0.85)])

    # Optional cost display.
    energy_price: float = 0.0
    currency: str = ""

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        psu_raw = _str(env, "PSU_EFF")
        host_root = _str(env, "HOST_ROOT")
        host_root = host_root.rstrip("/") or host_root   # "/" stays "/"
        return cls(
            port=int(_str(env, "PORT") or 8088),
            title=_str(env, "MONITOR_TITLE") or "Server Monitor",
            interval=max(1.0, _float(env, "INTERVAL", 2.0)),
            sys_root=_str(env, "SYS_ROOT") or "/sys",
            proc_root=_str(env, "PROC_ROOT") or "/proc",
            auth_user=_str(env, "MONITOR_USER") or "admin",
            auth_password=env.get("MONITOR_PASSWORD", ""),
            data_dir=_str(env, "DATA_DIR"),
            fan_labels=_pairs(env, "FAN_LABELS"),
            extra_temps=_pairs(env, "EXTRA_TEMPS"),
            net_ifaces=_list(env, "NET_IFACES"),
            disks=_list(env, "DISKS"),
            disk_paths=_pairs(env, "DISK_PATHS") or ({} if host_root else {"System": "/"}),
            host_root=host_root,
            disk_labels={k.rstrip("/") or "/": v
                         for k, v in _pairs(env, "DISK_LABELS").items()},
            disk_exclude=[p.rstrip("/") or "/" for p in _list(env, "DISK_EXCLUDE")],
            cpu_idle_w=_float(env, "CPU_IDLE_W", 10.0),
            cpu_max_w=_float(env, "CPU_MAX_W", 65.0),
            base_w=_float(env, "BASE_W", 20.0),
            psu_watts=_float(env, "PSU_WATTS", 0.0),
            psu_curve=parse_psu_curve(psu_raw) if psu_raw else [(0.0, 0.85)],
            energy_price=_float(env, "ENERGY_PRICE", 0.0),
            currency=_str(env, "CURRENCY"),
        )

    def psu_efficiency(self, dc_watts):
        """Efficiency at a given DC load, interpolated on the PSU curve."""
        curve = self.psu_curve
        if len(curve) == 1 or self.psu_watts <= 0:
            # No rated wattage to place the load on the curve: use the point
            # closest to 50 % load as a typical value.
            return min(curve, key=lambda p: abs(p[0] - 50))[1]
        load = 100.0 * dc_watts / self.psu_watts
        if load <= curve[0][0]:
            return curve[0][1]
        for (l0, e0), (l1, e1) in zip(curve, curve[1:]):
            if load <= l1:
                return e0 + (e1 - e0) * (load - l0) / (l1 - l0)
        return curve[-1][1]
