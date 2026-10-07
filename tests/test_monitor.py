import base64
import json
import os
import threading
import unittest
import urllib.error
import urllib.request

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
