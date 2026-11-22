#!/usr/bin/env python3
"""Keep the NVMe out of its idle power states (APST) with tiny O_DIRECT reads.

  keepawake.py FILE SECONDS [PERIOD_MS]

Reads 4 KiB from FILE (on the NVMe) every PERIOD_MS (default 5) with O_DIRECT, so every read
reaches the drive (no page cache). Prints the read count and max latency at the end.
"""
import mmap
import os
import sys
import time

path, dur = sys.argv[1], float(sys.argv[2])
period = float(sys.argv[3]) / 1000 if len(sys.argv) > 3 else 0.005
size = os.path.getsize(path)
fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
buf = mmap.mmap(-1, 4096)  # page-aligned, as O_DIRECT requires
n, worst, off = 0, 0.0, 0
t_next = t_end = time.monotonic()
t_end += dur
while t_next < t_end:
    t0 = time.monotonic()
    os.preadv(fd, [buf], off)
    worst = max(worst, time.monotonic() - t0)
    off = (off + 4096 * 97) % (size - 4096) // 4096 * 4096  # hop around, not one cached block
    n += 1
    t_next += period
    time.sleep(max(0, t_next - time.monotonic()))
print(f"reads {n}, max latency {worst * 1000:.1f} ms")
