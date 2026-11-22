#!/usr/bin/env python3
"""Hardware check for network frame sync (run on a camera Pi, with camd stopped via the client
or `camcli stop`): brings up camd's capture chain, then

  1. measures frame timestamp jitter (are ISP timestamps good enough to sync to?)
  2. stretches single frames through the sensor's VBLANK control and checks that the frame
     phase moves by exactly that much, and that rkaiq doesn't put VBLANK back

  python3 sync_probe.py [uhd60|full45]
"""
import asyncio
import fcntl
import statistics
import struct
import sys
import time

sys.path.insert(0, "/home/cam1/camd")
import camd  # noqa: E402
from camd import Gst  # noqa: E402

V4L2_CID_VBLANK = 0x009e0901
VIDIOC_G_CTRL = 0xc008561b
VIDIOC_S_CTRL = 0xc008561c


def ctrl(fd, cid, value=None):
    buf = bytearray(struct.pack("Ii", cid, value or 0))
    fcntl.ioctl(fd, VIDIOC_G_CTRL if value is None else VIDIOC_S_CTRL, buf)
    return struct.unpack("Ii", buf)[1]


async def main(mode):
    Gst.init(None)
    cam = camd.Camera()
    ts = []  # (frame monotonic ns from the ISP, monotonic ns when we saw it)
    await cam._start_chain(mode)
    src = cam.main.get_by_name("src").get_static_pad("src")
    base = cam.main.get_base_time()
    src.add_probe(Gst.PadProbeType.BUFFER,
                  lambda p, i: (ts.append((i.get_buffer().pts + base, time.monotonic_ns())),
                                Gst.PadProbeReturn.OK)[1])
    sensor, _ = camd.find_sensor()
    fd = open(sensor, "rb", buffering=0)
    vb0 = ctrl(fd.fileno(), V4L2_CID_VBLANK)
    h = camd.MODES[mode]["sensor"][1]
    fps = camd.MODES[mode]["fps"]
    line_ns = 1e9 / fps / (h + vb0)
    print(f"{mode}: VBLANK {vb0}, frame {h + vb0} lines, line {line_ns:.0f} ns")

    await asyncio.sleep(6)
    d = [b[0] - a[0] for a, b in zip(ts, ts[1:])][-5 * fps:]
    lat = [b - a for a, b in ts[-5 * fps:]]
    print(f"intervals: mean {statistics.mean(d)/1e3:.2f} us, stdev {statistics.pstdev(d)/1e3:.2f} us, "
          f"min {min(d)/1e3:.1f}, max {max(d)/1e3:.1f}")
    print(f"delivery latency (timestamp -> our probe): median {statistics.median(lat)/1e6:.2f} ms, "
          f"max {max(lat)/1e6:.2f} ms")

    for extra in (100, 500, 1000):
        await asyncio.sleep(1)
        n0 = len(ts)
        # stretch one frame: VBLANK + extra right after a frame arrives, back one frame later
        while len(ts) == n0:
            await asyncio.sleep(0.0005)
        ctrl(fd.fileno(), V4L2_CID_VBLANK, vb0 + extra)
        n1 = len(ts)
        while len(ts) == n1:
            await asyncio.sleep(0.0005)
        ctrl(fd.fileno(), V4L2_CID_VBLANK, vb0)
        await asyncio.sleep(0.5)
        seg = ts[n0 - 5:n0 + 12]
        iv = [round((b[0] - a[0]) / 1e3) for a, b in zip(seg, seg[1:])]
        long_ = [x for x in iv if x > 1e6 / fps * 1.0 + line_ns * extra / 2e3]
        print(f"+{extra} lines (expect +{extra * line_ns / 1e3:.0f} us on one frame): intervals {iv}")
        print(f"   stretched frames: {len(long_)}, VBLANK now {ctrl(fd.fileno(), V4L2_CID_VBLANK)}")

    await asyncio.sleep(5)  # does rkaiq rewrite VBLANK on its own over time?
    print(f"after 5 s: VBLANK {ctrl(fd.fileno(), V4L2_CID_VBLANK)}")
    await cam._stop_chain()


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "uhd60"))
