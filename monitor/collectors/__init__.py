"""Collectors read one area of the system and add values/tiles to a Sample.

Each collector is a class built from the Config with a collect(sample)
method. Order matters: PowerCollector uses readings from the others.
"""

from .hwmon import HwmonCollector
from .io import DiskIOCollector, NetworkCollector
from .nvidia import NvidiaCollector
from .power import PowerCollector
from .system import CpuCollector, DiskSpaceCollector, MemoryCollector

ALL = (
    CpuCollector,
    MemoryCollector,
    DiskSpaceCollector,
    NvidiaCollector,
    HwmonCollector,
    DiskIOCollector,
    NetworkCollector,
    PowerCollector,
)
