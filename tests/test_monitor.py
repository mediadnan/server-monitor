import base64
import json
import os
import threading
import unittest
import urllib.error
import urllib.request

from monitor.collectors.system import DiskSpaceCollector
from monitor.config import Config, parse_psu_curve
from monitor.history import History
from monitor.monitor import Monitor
from monitor.sample import Sample
from monitor.server import make_handler
from http.server import ThreadingHTTPServer

from tests.fakehost import FakeHost


def run_twice(host, cfg, seconds=2.0):
    """Two sampling passes `seconds` apart; returns the second Sample."""
    m = Monitor(cfg)
    s1 = Sample(1000.0)
    for c in m.collectors:
        c.collect(s1)
    host.advance(seconds)
    s2 = Sample(1000.0 + seconds)
    for c in m.collectors:
        c.collect(s2)
    return s2


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = Config.from_env({})
        self.assertEqual(cfg.port, 8088)
        self.assertEqual(cfg.auth_password, "")
        self.assertEqual(cfg.disk_paths, {"System": "/"})

    def test_pairs_and_lists(self):
        cfg = Config.from_env({"FAN_LABELS": "fan2=CPU fan, nct6798/fan4=Rear",
                               "NET_IFACES": "eth0, eth1"})
        self.assertEqual(cfg.fan_labels, {"fan2": "CPU fan", "nct6798/fan4": "Rear"})
        self.assertEqual(cfg.net_ifaces, ["eth0", "eth1"])

    def test_bad_values_raise(self):
        with self.assertRaises(ValueError):
            Config.from_env({"BASE_W": "lots"})
        with self.assertRaises(ValueError):
            Config.from_env({"FAN_LABELS": "fan1"})
        with self.assertRaises(ValueError):
            parse_psu_curve("1.5")

    def test_psu_curve(self):
        cfg = Config.from_env({"PSU_EFF": "20:0.80,50:0.90,100:0.85", "PSU_WATTS": "500"})
        self.assertAlmostEqual(cfg.psu_efficiency(50), 0.80)     # 10 % -> clamped
        self.assertAlmostEqual(cfg.psu_efficiency(175), 0.85)    # 35 % -> halfway
        self.assertAlmostEqual(cfg.psu_efficiency(250), 0.90)
        self.assertAlmostEqual(cfg.psu_efficiency(1000), 0.85)
        flat = Config.from_env({"PSU_EFF": "0.82"})
        self.assertAlmostEqual(flat.psu_efficiency(300), 0.82)


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()

    def tearDown(self):
        self.host.close()

    def test_full_pass(self):
        s = run_twice(self.host, Config.from_env(self.host.env()))
        v = s.values
        self.assertAlmostEqual(v["cpu_usage"], 25.0, delta=0.5)
        self.assertAlmostEqual(v["ram"], 50.0, delta=0.1)
        self.assertEqual(s.info["uptime"], 93784.12)
        self.assertEqual(v["temp_cpu"], 55.2)
        self.assertEqual(v["temp_nvme_nvme0"], 41.9)
        self.assertEqual(v["temp_board"], 33.0)
        self.assertAlmostEqual(v["disk_nvme0n1_r"], 1e6, delta=1e4)
        self.assertAlmostEqual(v["net_eth0_rx"], 2e5, delta=1e3)
        self.assertAlmostEqual(v["power_cpu"], 30.0, delta=0.1)
        self.assertEqual(s.tiles["power_cpu"].source, "measured")
        # partitions, bridges and junk sensors are ignored
        self.assertNotIn("disk_nvme0n1p1", s.tiles)
        self.assertNotIn("net_docker0", s.tiles)
        self.assertFalse(any("temp7" in k for k in v))

    def test_fans_auto_hides_unused_headers(self):
        s = run_twice(self.host, Config.from_env(self.host.env()))
        fans = {t.label for t in s.tiles.values() if t.group == "fans"}
        self.assertEqual(fans, {"Fan 2", "Fan 4"})

    def test_fan_labels(self):
        cfg = Config.from_env(self.host.env(FAN_LABELS="fan4=Rear,fan2=CPU"))
        s = run_twice(self.host, cfg)
        fans = sorted((t.order, t.label) for t in s.tiles.values() if t.group == "fans")
        self.assertEqual([label for _, label in fans], ["Rear", "CPU"])

    def test_power_estimate_without_rapl(self):
        self.host.write("sys/class/powercap/intel-rapl:0/name", "gone")
        cfg = Config.from_env(self.host.env(CPU_IDLE_W="10", CPU_MAX_W="50",
                                            BASE_W="20", PSU_EFF="0.8"))
        s = run_twice(self.host, cfg)
        self.assertAlmostEqual(s.values["power_cpu"], 20.0, delta=0.3)  # 25 % load
        self.assertAlmostEqual(s.values["power_wall"], 50.0, delta=0.5)  # (20+20)/0.8
        self.assertEqual(s.tiles["power_cpu"].source, "estimated")

    def test_every_tile_has_data_keys(self):
        s = run_twice(self.host, Config.from_env(self.host.env()))
        for tile in s.tiles.values():
            for key, _ in tile.series:
                self.assertIn(key, s.values, tile.id)


class DiskSpaceTest(unittest.TestCase):
    """Host mounts seen through HOST_ROOT. All fake mountpoints live on one
    temp filesystem, so they share its device number."""

    def setUp(self):
        self.host = FakeHost()
        h = self.host
        self.hostfs = os.path.join(h.root, "hostfs")
        for d in ("srv/immich-data", "mnt/immich_backup", "data/coolify/backups",
                  "boot/efi", "snap/core/1", "run", "pool", "pool/home", "media/my disk"):
            os.makedirs(os.path.join(self.hostfs, d), exist_ok=True)
        st = os.stat(self.hostfs)
        self.dev = f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"

        # LVM on an NVMe partition, LUKS on a whole disk, LUKS on a partition,
        # md RAID1 of two partitions.
        nvme, sata = "sys/devices/pci0/nvme/nvme0n1", "sys/devices/pci0/ata"
        h.link("sys/class/block/nvme0n1p3", f"{nvme}/nvme0n1p3")
        h.write(f"{nvme}/nvme0n1p3/partition", 3)
        h.link("sys/class/block/sda", f"{sata}/sda")
        h.link("sys/class/block/sdb1", f"{sata}/sdb/sdb1")
        h.write(f"{sata}/sdb/sdb1/partition", 1)
        for disk in ("sdc", "sdd"):
            h.link(f"sys/class/block/{disk}1", f"{sata}/{disk}/{disk}1")
            h.write(f"{sata}/{disk}/{disk}1/partition", 1)
        for dm, name, slaves in (("dm-0", "ubuntu--vg-ubuntu--lv", ["nvme0n1p3"]),
                                 ("dm-1", "immich_crypt", ["sda"]),
                                 ("dm-2", "immich_backup", ["sdb1"]),
                                 ("md0", "", ["sdc1", "sdd1"])):
            h.link(f"sys/class/block/{dm}", f"sys/devices/virtual/block/{dm}")
            if name:
                h.write(f"sys/devices/virtual/block/{dm}/dm/name", name)
            for slave in slaves:
                h.link(f"sys/devices/virtual/block/{dm}/slaves/{slave}",
                       f"sys/class/block/{slave}")
        # The host's /dev resolves mapper names for two of them; "/" is found
        # through sysfs dm/name instead.
        os.makedirs(os.path.join(self.hostfs, "dev/mapper"))
        for name, dm in (("immich_crypt", "dm-1"), ("immich_backup", "dm-2")):
            os.symlink(f"../{dm}", os.path.join(self.hostfs, "dev/mapper", name))

    def tearDown(self):
        self.host.close()

    def mountinfo(self, *extra):
        d = self.dev
        lines = [
            f"25 1 {d} / / rw,relatime shared:1 - ext4 /dev/mapper/ubuntu--vg-ubuntu--lv rw",
            f"26 25 0:5 / /run rw shared:2 - tmpfs tmpfs rw",
            f"27 25 7:3 / /snap/core/1 ro shared:3 - squashfs /dev/loop3 ro",
            f"28 25 {d} / /boot/efi rw shared:4 - vfat /dev/nvme0n1p1 rw",
            f"29 25 {d} / /srv/immich-data rw shared:5 - ext4 /dev/mapper/immich_crypt rw",
            f"30 25 {d} / /mnt/immich_backup rw shared:6 - ext4 /dev/mapper/immich_backup rw",
            f"31 25 {d} /coolify /data/coolify/backups rw shared:6 - ext4 /dev/mapper/immich_backup rw",
            f"32 25 0:44 / /var/lib/docker/overlay2/x/merged rw - overlay overlay rw",
        ] + list(extra)
        self.host.write("hostfs/proc/1/mountinfo", "\n".join(lines))

    def collect(self, **env):
        cfg = Config.from_env(self.host.env(HOST_ROOT=self.hostfs, **env))
        s = Sample(1000.0)
        DiskSpaceCollector(cfg).collect(s)
        return s

    def test_config(self):
        cfg = Config.from_env({"HOST_ROOT": "/hostfs/", "DISK_EXCLUDE": "/boot/efi/",
                               "DISK_LABELS": "/srv/data/=Data"})
        self.assertEqual(cfg.host_root, "/hostfs")
        self.assertEqual(cfg.disk_paths, {})
        self.assertEqual(cfg.disk_labels, {"/srv/data": "Data"})
        self.assertEqual(cfg.disk_exclude, ["/boot/efi"])
        cfg = Config.from_env({"HOST_ROOT": "/hostfs", "DISK_PATHS": "Docker=/"})
        self.assertEqual(cfg.disk_paths, {"Docker": "/"})
        self.assertEqual(Config.from_env({"HOST_ROOT": "/"}).host_root, "/")

    def test_every_disk_once_with_labels(self):
        self.mountinfo()
        s = self.collect(DISK_LABELS="/srv/immich-data=Photos,/mnt/immich_backup=Backup",
                         DISK_EXCLUDE="/boot/efi")
        tiles = sorted(s.tiles.values(), key=lambda t: t.order)
        self.assertEqual([t.id for t in tiles], ["space_system", "space_photos", "space_backup"])
        self.assertEqual(tiles[1].label, "Disk space · Photos")
        self.assertTrue(s.notes["space_system"].endswith("GB · nvme0n1"))
        self.assertTrue(s.notes["space_photos"].endswith("GB · sda"))
        self.assertTrue(s.notes["space_backup"].endswith("GB · sdb"))
        for tile in tiles:
            self.assertIn(tile.id, s.values)

    def test_bind_mount_is_not_a_disk(self):
        # Without labels and with the bind mount listed first, the
        # filesystem's own root still wins.
        self.mountinfo()
        lines = self.host_lines()
        lines.insert(1, lines.pop(6))
        self.host.write("hostfs/proc/1/mountinfo", "\n".join(lines))
        s = self.collect()
        self.assertEqual(sorted(s.tiles), ["space_efi", "space_immich_backup",
                                           "space_immich_data", "space_system"])
        self.assertEqual(s.tiles["space_efi"].label, "Disk space · efi")
        self.assertTrue(s.notes["space_efi"].endswith("GB"))   # unknown in fake sysfs

    def test_hidden_mount_is_skipped(self):
        # Mounted on the host after the container started, without rslave:
        # the path inside the container is still the parent filesystem.
        self.mountinfo("40 25 8:99 / /media/my\\040disk rw - ext4 /dev/sde1 rw")
        s = self.collect(DISK_EXCLUDE="/boot/efi")
        self.assertNotIn("space_my_disk", s.tiles)
        self.mountinfo(f"40 25 {self.dev} / /media/my\\040disk rw - ext4 /dev/sde1 rw")
        s = self.collect(DISK_EXCLUDE="/boot/efi")
        self.assertEqual(s.tiles["space_my_disk"].label, "Disk space · my disk")

    def test_raid_and_subvolumes(self):
        # btrfs on md RAID1: two subvolumes of one filesystem are one tile.
        d = self.dev
        self.mountinfo(f"41 25 {d} /@pool /pool rw - btrfs /dev/md0 rw",
                       f"42 41 {d} /@home /pool/home rw - btrfs /dev/md0 rw")
        s = self.collect(DISK_EXCLUDE="/boot/efi")
        self.assertIn("space_pool", s.tiles)
        self.assertNotIn("space_home", s.tiles)
        self.assertTrue(s.notes["space_pool"].endswith("GB · sdc, sdd"))

    def test_unreadable_mountinfo_falls_back_to_host_root(self):
        s = self.collect()
        self.assertEqual(list(s.tiles), ["space_system"])
        self.assertNotIn("·", s.notes["space_system"].split("GB")[1])

    def host_lines(self):
        with open(os.path.join(self.hostfs, "proc/1/mountinfo")) as f:
            return f.read().strip().split("\n")


class HistoryTest(unittest.TestCase):
    def test_slow_tier_and_energy(self):
        h = History(2.0)
        for i in range(150):   # 5 minutes at a steady 120 W
            h.add(1000.0 + 2 * i, {"power_wall": 120.0, "x": i})
        self.assertEqual(len(h.slow), 10)
        self.assertEqual(h.slow[0][1]["x"], 7.0)   # mean of 0..14
        wh, covered = h.integrate("power_wall", 24 * 3600)
        self.assertAlmostEqual(covered, 300.0)
        self.assertAlmostEqual(wh, 10.0)           # 120 W * 5 min

    def test_save_and_load(self):
        import tempfile
        import time
        h = History(2.0)
        now = time.time()
        for i in range(30):
            h.add(now - 60 + 2 * i, {"a": float(i)})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "h.json")
            History.save(h.snapshot(), path)
            h2 = History(2.0)
            self.assertTrue(h2.load(path))
            self.assertEqual(list(h2.fast), [(t, v) for t, v in h.fast])
            self.assertFalse(History(5.0).load(path))   # different interval


class ServerTest(unittest.TestCase):
    def start(self, **env):
        self.host = FakeHost()
        cfg = Config.from_env(self.host.env(PORT="0", **env))
        mon = Monitor(cfg)
        mon.step()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(mon, cfg))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.host.close()

    def get(self, path, auth=None):
        req = urllib.request.Request(self.base + path)
        if auth:
            req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_open_mode(self):
        self.start(MONITOR_TITLE="Box <1>")
        code, headers, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"<title>Box &lt;1&gt;</title>", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        code, _, body = self.get("/api/tiles")
        tiles = json.loads(body)
        self.assertEqual(tiles["groups"], ["usage", "io", "temps", "energy", "fans"])
        self.assertTrue(tiles["tiles"])
        self.assertEqual(self.get("/static/app.js")[0], 200)
        self.assertEqual(self.get("/static/../config.py")[0], 404)
        self.assertEqual(self.get("/static/index.html")[0], 404)

    def test_auth(self):
        self.start(MONITOR_PASSWORD="s3cret")
        self.assertEqual(self.get("/healthz")[0], 200)
        code, headers, _ = self.get("/api/now")
        self.assertEqual(code, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])
        self.assertEqual(self.get("/api/now", "admin:s3cret")[0], 200)
        self.assertEqual(self.get("/", "admin:wrong")[0], 401)


if __name__ == "__main__":
    unittest.main()
