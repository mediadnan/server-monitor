"""The sampling loop: runs the collectors, keeps history and tile metadata."""

import logging
import os
import threading
import time

from . import collectors
from .history import History, columns
from .sample import GROUPS, Sample

log = logging.getLogger(__name__)

SAVE_EVERY = 300   # seconds between history snapshots when DATA_DIR is set


class Monitor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.collectors = [c(cfg) for c in collectors.ALL]
        self.lock = threading.Lock()
        self.history = History(cfg.interval)
        self.tiles = {}      # id -> Tile, every tile seen since start
        self.rev = 0         # bumped when the tile list changes
        self.latest = {"ts": 0, "v": {}, "notes": {}, "info": {}}
        self.energy_note = ""
        self.broken = set()  # collectors that already logged an error
        self.data_file = os.path.join(cfg.data_dir, "history.json") if cfg.data_dir else ""
        if self.data_file and self.history.load(self.data_file):
            log.info("restored history from %s", self.data_file)
            self._update_energy_note()

    # -- sampling -------------------------------------------------------------

    def sample(self):
        s = Sample(time.time())
        for c in self.collectors:
            name = type(c).__name__
            try:
                c.collect(s)
            except Exception:
                if name not in self.broken:
                    self.broken.add(name)
                    log.exception("%s failed (further errors are silenced)", name)
        return s

    def step(self):
        s = self.sample()
        with self.lock:
            for tid, tile in s.tiles.items():
                if self.tiles.get(tid) != tile:
                    self.tiles[tid] = tile
                    self.rev += 1
            if self.history.add(s.ts, s.values):
                self._update_energy_note()
            notes = dict(s.notes)
            if self.energy_note and "power_wall" in notes:
                notes["power_wall"] += "\n" + self.energy_note
            self.latest = {"ts": round(s.ts, 1), "v": s.values,
                           "notes": notes, "info": s.info}

    def _update_energy_note(self):
        wh, covered = self.history.integrate("power_wall", 24 * 3600)
        if covered < 60:
            return
        hours, minutes = int(covered // 3600), int(covered % 3600 // 60)
        window = "24 h" if covered >= 24 * 3600 - 120 else (
            f"{hours} h {minutes} m" if hours else f"{minutes} m")
        amount = f"{wh / 1000:.2f} kWh" if wh >= 1000 else f"{wh:.0f} Wh"
        text = f"last {window}: {amount}"
        if self.cfg.energy_price > 0:
            cost = wh / 1000 * self.cfg.energy_price
            text += f" ≈ {cost:.2f} {self.cfg.currency}".rstrip()
        self.energy_note = text

    def run(self):
        last_save = time.time()
        while True:
            started = time.time()
            self.step()
            if self.data_file and started - last_save >= SAVE_EVERY:
                self.save()
                last_save = started
            time.sleep(max(0.2, self.cfg.interval - (time.time() - started)))

    def start(self):
        threading.Thread(target=self.run, name="sampler", daemon=True).start()

    def save(self):
        if not self.data_file:
            return
        with self.lock:
            snapshot = self.history.snapshot()
        try:
            History.save(snapshot, self.data_file)
        except OSError as e:
            log.warning("could not save history to %s: %s", self.data_file, e)

    # -- read API used by the web server ----------------------------------------

    def now(self):
        with self.lock:
            return dict(self.latest, rev=self.rev)

    def tile_list(self):
        with self.lock:
            tiles = list(self.tiles.values())
            rev = self.rev
        tiles.sort(key=lambda t: (GROUPS.index(t.group), t.order, t.label))
        return {"rev": rev, "title": self.cfg.title, "groups": list(GROUPS),
                "tiles": [t.to_json() for t in tiles]}

    def history_columns(self, tier):
        with self.lock:
            rows = self.history.rows(tier)
        return columns(rows)
