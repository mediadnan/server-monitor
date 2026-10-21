"""Data types shared by the collectors and the web layer."""

from dataclasses import asdict, dataclass, field

# Dashboard categories, in display order.
GROUPS = ("usage", "io", "temps", "energy", "fans")


@dataclass
class Tile:
    """One card on the dashboard: a big current value plus a history chart,
    or with chart="bar" a meter filled from min to max instead.

    A tile plots one or more series. For single-series tiles the series key
    is the tile id; multi-series tiles (disk read/write, network down/up)
    list their keys explicitly.
    """

    id: str
    group: str
    label: str
    unit: str = ""
    series: list = field(default_factory=list)   # [[key, name], ...]
    decimals: int = 0
    format: str = ""        # "rate" = bytes/s, auto-scaled by the UI
    min: float = None       # fixed y-axis range when both are set
    max: float = None
    zero: bool = False      # y-axis starts at 0
    span: float = 1.0       # smallest y-range shown, avoids zoomed-in noise
    warn: float = None      # value >= warn is amber
    hot: float = None       # value >= hot is red
    low: float = None       # value <= low is amber (stalled fan)
    source: str = ""        # "measured" or "estimated" (energy tiles)
    chart: str = ""         # "bar" = current-value meter, no history chart
    order: int = 50         # sort key inside its group

    def __post_init__(self):
        if self.group not in GROUPS:
            raise ValueError(f"unknown group {self.group!r}")
        if not self.series:
            self.series = [[self.id, self.label]]

    def to_json(self):
        return {k: v for k, v in asdict(self).items() if v is not None and v != ""}


class Sample:
    """Everything gathered in one sampling pass.

    values: numeric readings, stored in history and plotted
    notes:  short text shown under a tile's value, keyed by tile id
    tiles:  tile definitions discovered during this pass
    info:   page-level facts that are not plotted (uptime, ...)
    power:  measured component power in watts, used by the power model
    """

    def __init__(self, ts=0.0):
        self.ts = ts
        self.values = {}
        self.notes = {}
        self.tiles = {}
        self.info = {}
        self.power = {}

    def tile(self, tile):
        self.tiles[tile.id] = tile
        return tile

    def set(self, key, value, ndigits=1):
        if value is not None:
            self.values[key] = round(value, ndigits)

    def note(self, tile_id, text):
        if text:
            self.notes[tile_id] = text
