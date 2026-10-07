"""A fake /sys and /proc tree, so collectors can be tested on any machine.

The hardware it describes is made up and deliberately generic.
"""

import os
import tempfile


class FakeHost:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name
        self.sys = os.path.join(self.root, "sys")
        self.proc = os.path.join(self.root, "proc")
        self.cpu = [1000, 0, 500, 8000, 100, 0, 0, 0]
        self.disk = {"nvme0n1": [4096, 2048, 10], "sda": [4096, 2048, 10]}   # sectors r/w, ms busy
        self.net = {"eth0": [0, 0]}
        self.energy_uj = 0
        self._build()

    def close(self):
        self.dir.cleanup()

    def env(self, **extra):
        env = {"SYS_ROOT": self.sys, "PROC_ROOT": self.proc}
        env.update(extra)
        return env

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(str(text) + "\n")
        return path

    def link(self, rel, target_rel):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.makedirs(os.path.join(self.root, target_rel), exist_ok=True)
        os.symlink(os.path.join(self.root, target_rel), path)

    def _build(self):
        self.write("proc/meminfo", "MemTotal: 16000000 kB\nMemFree: 4000000 kB\n"
                   "MemAvailable: 8000000 kB\nSwapTotal: 2000000 kB\nSwapFree: 1000000 kB")
        self.write("proc/loadavg", "0.50 0.40 0.30 1/200 1234")
        self.write("proc/uptime", "93784.12 100000.00")

        # CPU (AMD style), NVMe, motherboard super I/O with fans.
        self.write("sys/class/hwmon/hwmon0/name", "k10temp")
        self.write("sys/class/hwmon/hwmon0/temp1_input", 55250)
        self.write("sys/class/hwmon/hwmon0/temp1_label", "Tctl")
        self.write("sys/class/hwmon/hwmon1/name", "nvme")
        self.write("sys/class/hwmon/hwmon1/temp1_input", 41850)
        self.link("sys/class/hwmon/hwmon1/device", "sys/devices/pci0/nvme/nvme0")
        self.write("sys/class/hwmon/hwmon2/name", "nct6798")
        self.write("sys/class/hwmon/hwmon2/temp1_input", 33000)
        self.write("sys/class/hwmon/hwmon2/temp1_label", "SYSTIN")
        self.write("sys/class/hwmon/hwmon2/temp7_input", -128000)   # junk channel
        self.write("sys/class/hwmon/hwmon2/fan1_input", 0)           # empty header
        self.write("sys/class/hwmon/hwmon2/fan2_input", 980)
        self.write("sys/class/hwmon/hwmon2/fan4_input", 720)

        # One physical NIC and one docker bridge (no device link).
        self.link("sys/class/net/eth0/device", "sys/devices/pci0/eth0dev")
        self.write("sys/class/net/eth0/operstate", "up")
        self.write("sys/class/net/docker0/operstate", "up")
        self.write("sys/class/net/docker0/statistics/rx_bytes", 5)
        self.write("sys/class/net/docker0/statistics/tx_bytes", 5)

        self.write("sys/class/powercap/intel-rapl:0/name", "package-0")
        self.write("sys/class/powercap/intel-rapl:0/max_energy_range_uj", 262143328850)
        self.write("sys/class/powercap/intel-rapl:0:0/name", "core")
        self.advance(0)

    def advance(self, seconds, cpu_busy=0.25, disk_bps=1e6, net_bps=2e5, cpu_w=30.0):
        """Move all counters forward as if `seconds` passed under some load."""
        hz = 100 * seconds * 4   # 4 CPUs at USER_HZ=100
        self.cpu[0] += int(hz * cpu_busy)
        self.cpu[3] += int(hz * (1 - cpu_busy))
        self.write("proc/stat", "cpu  " + " ".join(map(str, self.cpu)) + "\ncpu0 0 0 0 0")
        lines = []
        for i, (name, c) in enumerate(self.disk.items()):
            c[0] += int(disk_bps * seconds / 512)
            c[1] += int(disk_bps * seconds / 2 / 512)
            c[2] += int(seconds * 100)
            lines.append(f" 259 {i} {name} 10 0 {c[0]} 5 20 0 {c[1]} 7 0 {c[2]} 12 0 0 0 0")
            lines.append(f" 259 {i + 10} {name}p1 1 0 99 1 1 0 99 1 0 1 1 0 0 0 0")
        self.write("proc/diskstats", "\n".join(lines))
        for name, c in self.net.items():
            c[0] += int(net_bps * seconds)
            c[1] += int(net_bps * seconds / 4)
            self.write(f"sys/class/net/{name}/statistics/rx_bytes", c[0])
            self.write(f"sys/class/net/{name}/statistics/tx_bytes", c[1])
        self.energy_uj += int(cpu_w * seconds * 1e6)
        self.write("sys/class/powercap/intel-rapl:0/energy_uj", self.energy_uj)
