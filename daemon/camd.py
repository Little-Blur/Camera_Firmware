#!/usr/bin/env python3
"""camd: camera daemon for one Orange Pi 5 Pro + IMX477 camera.

Owns the whole capture chain on this Pi and exposes it over the network:

  control  TCP 7070  newline-delimited JSON. Requests {"id": 1, "cmd": "start", ...}
                     get {"id": 1, "ok": true, "result": {...}} or {"id": 1, "ok": false,
                     "error": "..."}. The daemon also pushes {"event": "status", "data": {...}}
                     to every connected client (1 s while active, 5 s idle, and on changes).
  data     HTTP 8080 GET /preview.jpg        latest preview frame
                     GET /preview.mjpg       MJPEG stream (browser, ffplay)
                     GET /recordings/<name>  download a recording
                     GET /                   identity + status as JSON

Pipeline (what the old shell scripts did, now in-process so parts can change live):

  rkaiq_3A_server (3A: auto exposure, white balance) reads the IQ file at start
  mainpath  v4l2src /dev/video11 -> tee -+-> fakevideosink                        always while active
                                         +-> queue -> mpph265enc -> h265parse -> matroskamux -> file
                                         |                     record branch, plugged in/out live
                                         +-> videorate -> queue -> videoscale -> jpegenc -> appsink
                                                               preview branch, plugged in/out live

Rules learned on the rig (see SESSION_NOTES_2026-11-03/04.md):
  - the preview comes from the mainpath, not the ISP selfpath: in unite (dual ISP) mode the
    selfpath scaler shows only the left core's half, is garbage at 1/4 and 1/6, is capped at
    1920 wide, and stalls the whole ISP at some sizes (NM12 1280x720)
  - the always-on branch must be fakevideosink, not fakesink: it tells v4l2src that downstream
    understands GstVideoMeta (plane strides/offsets), otherwise v4l2src copies every frame
  - rkaiq sets the frame rate from the IQ file (AecFrameRateMode.FpsValue), and sometimes
    ignores it; the daemon measures the real rate and restarts the chain if it is wrong
  - rkaiq reads the IQ file only at start, so a preset change restarts the chain
  - shutter (setting shutter_us, 0 = auto) also goes in through the IQ file: AE in manual mode
    with only the exposure time fixed; gain stays automatic, so the brightness still adapts.
    Longest shutter = one frame period (less the sensor's 22-line margin)
  - temporal noise reduction (bayertnr) blends each frame with earlier ones where it sees no
    motion; at high gain noise hides the motion and moving things go see-through (ghosting, in
    the ISP output itself, keyframes included). Setting tnr: preset | low (half strength) | off
  - v4l2src picks the 2-plane NM12 format when offered, which the MPP encoder can only CPU-copy;
    the service preloads hide_nm12.so so it picks 1-plane NV12 and the encoder imports the ISP
    buffer directly (zero-copy: UHD60 with 0 drops instead of 3-4/s)
  - the ISP buffer pool is fixed at start (POOL_BUFFERS): by default v4l2src starts with 4 and
    grows the pool when the record branch is plugged in, and in unite mode the left ISP core
    never writes buffers added mid-stream (green left half on every one of them;
    SESSION_NOTES_2026-11-15.md)

Frame sync (camsync.py): one camera is the master, the others follow it, so all cameras' frames
start at the same moment (within tens of us). Role per camera: set_sync off|master|follower. With
sync on, the client asks the master for a start frame (sync_time) and every camera records from
that frame (record_start at_ts=...) and stops after the same frame (record_stop at_ts=...).

Identity: /etc/camrig/camera.json {"ip": ...} gives the static IP and the camera ID (cam<last
octet>); camrig-net applies it at boot. Without it the camera is NEW at BASE_IP, and the client
keeps it out of rig-wide commands until it has been given an IP (set_network).

Runs as cam1 (video group). No arbitrary shell: only the commands below.
"""
import asyncio
import ctypes
import ipaddress
import json
import logging
import mmap
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from collections import deque
from pathlib import Path

import gi

import camsync

gi.require_version("Gst", "1.0")
gi.require_version("GstAllocators", "1.0")
from gi.repository import Gst, GstAllocators  # noqa: E402

VERSION = "0.3.6"
CONTROL_PORT = 7070
HTTP_PORT = 8080
# Who this camera is: {"ip": ...}, applied to the network by camrig-net (root, at boot and
# through sudo). No file = an unconfigured camera, which sits at BASE_IP and shows as NEW.
CAMERA_CONF = Path("/etc/camrig/camera.json")
BASE_IP = "192.168.0.5"
NET_HELPER = "/usr/local/sbin/camrig-net"
IQ_DIR = Path("/etc/iqfiles")
IQ_NAME = "imx477_IMX477_Leica"  # rkaiq looks up <sensor>_<module>_<lens>.json
REC_DIR = Path.home() / "rec"
CONF_DIR = Path.home() / ".config" / "camd"
AIQ_LOG = "/dev/shm/camd_rkaiq.log"
MIN_FREE_GB = 2.0
# Unite mode: the left ISP core sometimes stops for good during NVMe write bursts (left half of
# the picture frozen) and only a chain restart brings it back. Kernel patch 9001 counts its
# frame-end timeouts; a frozen core times out on every frame, a merely late one on a few.
UNITE_STAT = Path("/sys/module/video_rkisp/parameters/unite_mi_stat")  # waits,timeouts,max_us,total_us
FROZEN_RATE = 0.8       # timeouts per frame that count as frozen
FROZEN_SECONDS = 2.0    # ... sustained this long
# RAM-buffered takes (setting rec_buffer = "ram"): record into tmpfs instead of letting the page
# cache flush to the NVMe in bursts every ~30 s (bursts make the left core late or freeze it).
# Every DRAIN_START_GB the file is moved to REC_DIR in one steady paced O_DIRECT run, the write
# pattern that never delayed the left core in tests (runE E6/E11, 120-200 MB/s steady), and the
# moved part is released from RAM while recording goes on (see Spill). Length is limited by the NVMe.
REC_BUFFERS = ("direct", "ram")
RAM_DIR = Path("/dev/shm/camd_rec")
RAM_RESERVE_GB = 0.5    # leave this much of /dev/shm and of MemAvailable; a take stops there
DISK_RESERVE_GB = 0.5   # same for the NVMe
SAVE_MBPS = 150         # paced copy RAM -> NVMe
SAVE_CHUNK = 1 << 20
DRAIN_START_GB = 1.5    # move to the NVMe once this much of the take is only in RAM
# matroskamux goes back to patch bytes it wrote: each cluster's size when the cluster closes
# (one GOP, ~1 s back) and the header (segment size, duration, seek head) at the end of the take.
# So the newest DRAIN_LAG bytes are never moved during the take, and the first SPILL_HEAD bytes
# stay in RAM and are copied again at the end.
DRAIN_LAG = 64 << 20
SPILL_HEAD = 1 << 20

# Capture modes. sensor = media-ctl format on the sensor, out = ISP mainpath size
# (unite: 4032 not 4048, each ISP core writes half the width rounded to 16),
# iq_fps = AecFrameRateMode.FpsValue (0 = the sensor mode's max), preview = JPEG size
# (CPU-scaled from the mainpath, same aspect).
MODES = {
    "uhd60": {"sensor": (3840, 2160), "out": (3840, 2160), "fps": 60, "iq_fps": 0,
              "preview": (640, 360), "label": "3840x2160 @ 60 fps"},
    "full45": {"sensor": (4056, 3040), "out": (4032, 3040), "fps": 45, "iq_fps": 45,
               "preview": (640, 480), "label": "4032x3040 @ 45 fps"},
}
# ISP buffers requested from v4l2src (it adds 2 of its own -> 16). All are allocated before the
# stream starts and the pool can't grow: the leaky record queue drops a frame instead.
# 16 x 18.4 MB (full45) ~ 295 MB of normal RAM (ISP buffers go through the IOMMU, not CMA).
POOL_BUFFERS = 14
DEFAULTS = {"mode": "uhd60", "preset": "day", "preview": True, "preview_fps": 5,
            "bitrate_mbps": 60, "sync": "off", "rec_buffer": "direct", "shutter_us": 0,
            "tnr": "preset"}
TNR_MODES = {"preset": 1.0, "low": 0.5, "off": 0.0}   # scale on bayertnr filter strength
V4L2_CID_ANALOGUE_GAIN = 0x009e0903
GAIN_UNIT = 16          # rockchip imx477 driver: analogue_gain 16 = 1x (IQ GainDot max 352 = 5632)
SHUTTER_MIN_US = 20
SHUTTER_FRAME_SHARE = 0.97   # longest shutter = this share of the frame period (22-line margin)
SYNC_ROLES = ("off", "master", "follower")

log = logging.getLogger("camd")


# ---------------------------------------------------------------- hardware lookup

def find_video(name):
    """/dev/videoN whose V4L2 name is exactly `name` (numbers can change between boots)."""
    for p in sorted(Path("/sys/class/video4linux").glob("video*")):
        if (p / "name").read_text().strip() == name:
            return f"/dev/{p.name}"
    raise RuntimeError(f"no video device named {name}")


def find_sensor():
    """(/dev/v4l-subdevN, media entity name) of the IMX477."""
    for p in sorted(Path("/sys/class/video4linux").glob("v4l-subdev*")):
        name = (p / "name").read_text().strip()
        if "imx477" in name:
            return f"/dev/{p.name}", name
    raise RuntimeError("imx477 not detected (check the camera cable, then reboot)")


async def run(*args, timeout=10):
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    return proc.returncode, out.decode(errors="replace").strip()


def identity():
    """Who this camera is. The camera ID comes from the configured static IP, so all Pis
    run one identical image; hostname/MAC/serial let the client spot mix-ups."""
    ip = "0.0.0.0"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent; just picks the outgoing interface
            ip = s.getsockname()[0]
    except OSError:
        pass
    mac = ""
    try:  # MAC of the interface that carries that IP
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True).stdout
        iface = next(line.split()[1] for line in out.splitlines() if f" {ip}/" in line)
        mac = Path(f"/sys/class/net/{iface}/address").read_text().strip()
    except (OSError, StopIteration):
        pass
    serial = ""
    try:
        serial = Path("/proc/device-tree/serial-number").read_text().strip("\x00\n ")
    except OSError:
        pass
    conf_ip = load_json(CAMERA_CONF, {}).get("ip")
    return {"camera_id": f"cam{conf_ip.rsplit('.', 1)[-1]}" if conf_ip else "NEW",
            "configured": bool(conf_ip), "configured_ip": conf_ip, "base_ip": BASE_IP,
            "ip": ip, "hostname": socket.gethostname(), "mac": mac, "serial": serial,
            "version": VERSION, "http_port": HTTP_PORT}


def temperatures():
    temps = {}
    for z in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            temps[(z / "type").read_text().strip()] = int((z / "temp").read_text()) / 1000
        except (OSError, ValueError):
            pass
    return temps


def unite_timeouts():
    """Left ISP core frame-end timeouts since boot (kernel patch 9001), None without the patch."""
    try:
        return int(UNITE_STAT.read_text().split(",")[1])
    except (OSError, ValueError, IndexError):
        return None


def space_left(path):
    """Bytes a take may still write to path's filesystem before it must stop (reserve kept).
    tmpfs also counts against RAM, so a RAM take is also limited by MemAvailable."""
    if path == RAM_DIR:
        avail = 0
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) * 1024
        return min(shutil.disk_usage(RAM_DIR).free, avail) - RAM_RESERVE_GB * 1e9
    return shutil.disk_usage(path).free - DISK_RESERVE_GB * 1e9


_libc = ctypes.CDLL(None, use_errno=True)
_libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]
FALLOC_FL_KEEP_SIZE, FALLOC_FL_PUNCH_HOLE = 1, 2


def punch(path, start, end):
    """Free bytes [start, end) of a tmpfs file from RAM; the file keeps its size and the writer
    its offset (reads of the range return zeros)."""
    fd = os.open(path, os.O_WRONLY)
    try:
        if end > start and _libc.fallocate(fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE,
                                           start, end - start):
            raise OSError(ctypes.get_errno(), "fallocate punch hole failed")
    finally:
        os.close(fd)


def paced_copy(src, dst, start, end, mbps, progress=None):
    """Copy bytes [start, end) of src into dst at the same offsets, in SAVE_CHUNK O_DIRECT writes
    on a fixed schedule: steady NVMe traffic, not the page cache's bursts, and no fsync (its FLUSH
    made bursts worse in tests). start must be 4 KiB aligned; a short last chunk is written padded
    to whole blocks (the caller truncates)."""
    buf = mmap.mmap(-1, SAVE_CHUNK)  # page-aligned, as O_DIRECT requires
    period = SAVE_CHUNK / (mbps * 1e6)
    fi = os.open(src, os.O_RDONLY)
    fo = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_DIRECT, 0o644)
    try:
        off, t_next = start, time.monotonic()
        while off < end:
            n = os.preadv(fi, [buf], off) if end - off >= SAVE_CHUNK else \
                os.preadv(fi, [memoryview(buf)[:end - off]], off)
            if n <= 0:
                break
            if n < SAVE_CHUNK:
                buf[n:] = bytes(SAVE_CHUNK - n)
            os.pwritev(fo, [memoryview(buf)[:(n + 4095) // 4096 * 4096]], off)
            off += n
            if progress:
                progress(off)
            t_next = max(t_next + period, time.monotonic() - period)  # behind: don't catch up in a burst
            time.sleep(max(0.0, t_next - time.monotonic()))
    finally:
        os.close(fi)
        os.close(fo)
        buf.close()


class Spill:
    """A RAM take on its way to REC_DIR. drain() moves everything but the newest DRAIN_LAG bytes
    while the take is still recording and frees it from RAM; finish() (after EOS) moves the rest,
    copies the head again (matroskamux rewrote it), and renames <name>.mkv.part to <name>.mkv.
    Progress is kept in <name>.spill.json next to the RAM file, so a camd restart can finish it."""

    def __init__(self, src):
        self.src = src
        self.dst = REC_DIR / src.name
        self.part = self.dst.with_name(self.dst.name + ".part")  # not listed until complete
        self.meta = src.with_suffix(".spill.json")
        self.moved = load_json(self.meta, {}).get("moved", 0)  # bytes [0, moved) are on the NVMe
        if not self.moved:
            self.part.unlink(missing_ok=True)  # a stale .part of the same name would mix in
        self.drains = 0
        self.stopping = False

    def pending(self):
        return os.path.getsize(self.src) - self.moved

    def drain(self, progress=None):
        end = (os.path.getsize(self.src) - DRAIN_LAG) // 4096 * 4096
        if end <= self.moved:
            return
        paced_copy(self.src, self.part, self.moved, end, SAVE_MBPS, progress)
        start, self.moved = self.moved, end
        self.meta.write_text(json.dumps({"moved": end}))  # before freeing: a restart must not copy the hole
        punch(self.src, max(start, SPILL_HEAD), end)
        self.drains += 1

    def finish(self, progress=None):
        size = os.path.getsize(self.src)
        paced_copy(self.src, self.part, self.moved, size, SAVE_MBPS, progress)
        paced_copy(self.src, self.part, 0, min(SPILL_HEAD, size), SAVE_MBPS)
        os.truncate(self.part, size)
        self.part.rename(self.dst)
        self.src.unlink()
        self.meta.unlink(missing_ok=True)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def presets():
    return sorted(m.group(1) for p in IQ_DIR.glob(f"{IQ_NAME}.*.json")
                  if (m := re.fullmatch(rf"{IQ_NAME}\.(.+)\.json", p.name)))


def max_shutter_us(mode):
    return int(1e6 / MODES[mode]["fps"] * SHUTTER_FRAME_SHARE)


def check_shutter(us):
    """0 = auto exposure, else a fixed exposure time in us (the mode's limit is applied at start)."""
    us, top = int(us), max(max_shutter_us(m) for m in MODES)
    if us and not SHUTTER_MIN_US <= us <= top:
        raise ValueError(f"shutter must be 0 (auto) or {SHUTTER_MIN_US}-{top} us")
    return us


def write_active_iq(preset, iq_fps, shutter_s=0, tnr="preset"):
    """Copy preset -> the active IQ file rkaiq reads, with this mode's frame rate and, with
    shutter_s, a fixed exposure time (AE manual for the time only: gain stays automatic).
    tnr scales (low) or switches off (off) the temporal noise reduction at every ISO.
    The presets themselves are never modified."""
    data = json.loads((IQ_DIR / f"{IQ_NAME}.{preset}.json").read_text())
    found, shutters, tnrs = [], [], []

    def set_tnr(node):
        tuning = node.get("TuningPara")
        if not isinstance(tuning, dict):
            return
        if tnr == "off":
            tuning["enable"] = 0
        for s in tuning.get("Setting", []):
            for iso in s.get("Tuning_ISO", []):
                for k in ("lo_filter_strength", "hi_filter_strength"):
                    if k in iso:
                        iso[k] = round(iso[k] * TNR_MODES[tnr], 3)
        tnrs.append(node)

    def walk(node):
        if isinstance(node, dict):
            if tnr != "preset" and isinstance(node.get("bayertnr_v2"), dict):
                set_tnr(node["bayertnr_v2"])
            if shutter_s and "AecOpType" in node:
                lin = (node.get("AecManualCtrl") or {}).get("LinearAE")
                if isinstance(lin, dict):
                    node["AecOpType"] = "RK_AIQ_OP_MODE_MANUAL"
                    lin.update(ManualTimeEn=1, TimeValue=shutter_s, ManualGainEn=0, ManualIspDgainEn=0)
                    shutters.append(lin)
            for k, v in node.items():
                if k == "AecFrameRateMode" and isinstance(v, dict):
                    v["isFpsFix"], v["FpsValue"] = 1, iq_fps
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(data)
    if not found:
        raise RuntimeError(f"preset {preset}: no AecFrameRateMode in IQ file")
    if shutter_s and not shutters:
        raise RuntimeError(f"preset {preset}: no AecManualCtrl.LinearAE in IQ file (fixed shutter)")
    if tnr != "preset" and not tnrs:
        raise RuntimeError(f"preset {preset}: no bayertnr_v2 in IQ file (tnr {tnr})")
    tmp = IQ_DIR / f".{IQ_NAME}.json.tmp"
    tmp.write_text(json.dumps(data, indent="\t"))
    os.replace(tmp, IQ_DIR / f"{IQ_NAME}.json")


class PreviewError(RuntimeError):
    pass


# ---------------------------------------------------------------- frame statistics

class FrameStats:
    """Counts frames from buffer timestamps (sensor capture time). A gap of more than
    1.5 frame periods means frames were dropped somewhere before us."""

    def __init__(self, fps):
        self.period = 1e9 / fps
        self.window = deque(maxlen=2 * fps)  # ~2 s for the fps estimate
        self.frames = self.dropped = 0
        self.last_pts = None
        self.last_wall = time.monotonic()
        self.buffers = set()  # dmabuf fds seen = ISP pool size actually in use

    def add(self, pts, fd=None):  # called on the GStreamer streaming thread
        if fd is not None:
            self.buffers.add(fd)
        if pts == Gst.CLOCK_TIME_NONE:
            return
        if self.last_pts is not None:
            gap = pts - self.last_pts
            if gap > 1.5 * self.period:
                self.dropped += round(gap / self.period) - 1
        self.last_pts = pts
        self.frames += 1
        self.window.append(pts)
        self.last_wall = time.monotonic()

    def fps(self):
        w = list(self.window)
        if len(w) < 2 or w[-1] == w[0]:
            return 0.0
        return (len(w) - 1) * 1e9 / (w[-1] - w[0])


# ---------------------------------------------------------------- the camera

class Camera:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.ident = identity()
        self.settings = {**DEFAULTS, **load_json(CONF_DIR / "state.json", {})}
        self.state = "idle"  # idle | starting | active | stopping | error
        self.busy = None
        self.error = None
        self.warnings = []
        self.lock = asyncio.Lock()
        self.mode = None
        self.main = self.tee = self.preview = None
        self.aiq = None
        self.stats = None
        self.pstats = None
        self.rec = None
        self.sync = None   # camsync.SyncService while active with a sync role
        self.sensor_fd = None  # sensor subdev while active: reads back the exposure rkaiq set
        self.base = 0      # pipeline base time: buffer pts + base = CLOCK_MONOTONIC of the frame
        self.jpeg = None
        self.jpeg_seq = 0
        self.jpeg_cond = asyncio.Condition()
        self.listeners = set()
        self.started = time.time()
        self.left = None       # (monotonic, timeouts, seconds frozen so far) for the freeze check
        self.recoveries = 0    # chain restarts after a frozen left core, since camd started
        self.save_queue = asyncio.Queue()  # Spills of finished RAM takes, to be finished
        self.saving = None     # {"file", "bytes", "done", "during_take"} while one is being copied

    # -- settings / status

    def save_settings(self):
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        (CONF_DIR / "state.json").write_text(json.dumps(self.settings, indent=1))

    def status(self):
        m = MODES.get(self.mode) if self.mode else None
        temps = temperatures()
        rec = None
        if self.rec:
            r = self.rec
            rec = {"file": r["path"].name, "started": r["started"],
                   "seconds": round(time.time() - r["started"], 1),
                   "frames": r["in"] - r["leaked"],
                   "dropped": (self.stats.dropped - r["drops0"]) + r["leaked"],
                   "bytes": r["path"].stat().st_size if r["path"].exists() else 0,
                   "buffer": "ram" if r.get("spill") else "direct"}
            if r.get("spill"):
                s = r["spill"]
                rec.update(ram_mb=round(s.pending() / 1e6), moves=s.drains)
            # Time left before the NVMe is full, at the rate written so far (the encoder runs
            # VBR: a quiet scene writes far less than the set bitrate)
            secs = time.time() - r["started"]
            rate = rec["bytes"] / secs if secs > 5 and rec["bytes"] else self.settings["bitrate_mbps"] / 8 * 1e6
            rec["seconds_left"] = int(max(0, space_left(REC_DIR)) / rate)
        du = shutil.disk_usage(REC_DIR)
        return {
            "camera_id": self.ident["camera_id"], "configured": self.ident["configured"],
            "state": self.state, "busy": self.busy,
            "error": self.error, "warnings": self.warnings,
            "mode": self.mode, "settings": self.settings,
            "stream": {"target_fps": m["fps"], "fps": round(self.stats.fps(), 2),
                       "frames": self.stats.frames, "dropped": self.stats.dropped,
                       "buffers": len(self.stats.buffers),
                       "size": m["out"]} if self.stats and m else None,
            "exposure": self._exposure(),
            "preview": {"running": self.preview is not None,
                        "fps": round(self.pstats.fps(), 1) if self.pstats else 0,
                        "size": m["preview"] if m else None},
            "recording": rec,
            "sync": self.sync.status() if self.sync else
            {"role": self.settings["sync"], "state": "off" if self.settings["sync"] == "off" else "idle"},
            "temp_c": max(temps.values()) if temps else None, "temps": temps,
            "disk_free_gb": round(du.free / 1e9, 1), "uptime_s": int(time.time() - self.started),
            "left_core_restarts": self.recoveries,
            # RAM takes: the move to the NVMe running now (during a take or after it)
            "ram": {"saving": self.saving, "queued": self.save_queue.qsize()},
        }

    def _exposure(self):
        """The sensor's exposure time (us) and analogue gain right now, set by AE or the fixed shutter."""
        if not self.sensor_fd or not self.mode:
            return None
        try:
            fd = self.sensor_fd.fileno()
            lines = camsync.get_ctrl(fd, camsync.V4L2_CID_EXPOSURE)
            vblank = camsync.get_ctrl(fd, camsync.V4L2_CID_VBLANK)
            gain = camsync.get_ctrl(fd, V4L2_CID_ANALOGUE_GAIN)
        except OSError:
            return None
        m = MODES[self.mode]
        line_us = 1e6 / m["fps"] / (m["sensor"][1] + vblank)
        return {"us": round(lines * line_us), "lines": lines, "fixed": bool(self.settings["shutter_us"]),
                "gain": round(gain / GAIN_UNIT, 2)}

    def changed(self):
        for cb in list(self.listeners):
            cb()

    # -- bring-up / tear-down

    async def start(self, mode=None, preset=None, preview=None, shutter_us=None, tnr=None):
        async with self.lock:
            mode = mode or self.settings["mode"]
            preset = preset or self.settings["preset"]
            shutter = self.settings["shutter_us"] if shutter_us is None else check_shutter(shutter_us)
            tnr = tnr or self.settings["tnr"]
            if tnr not in TNR_MODES:
                raise ValueError(f"tnr must be one of {', '.join(TNR_MODES)}")
            if mode not in MODES:
                raise ValueError(f"unknown mode {mode}; modes: {', '.join(MODES)}")
            if preset not in presets():
                raise ValueError(f"unknown preset {preset}; presets: {', '.join(presets())}")
            if preview is not None:
                self.settings["preview"] = bool(preview)
            if self.state == "active" and self.mode == mode and self.settings["preset"] == preset \
                    and self.settings["shutter_us"] == shutter and self.settings["tnr"] == tnr:
                await self._sync_preview()
                return self.status()
            if self.rec:
                raise RuntimeError("stop recording before changing mode, preset, shutter or tnr")
            self.settings.update(mode=mode, preset=preset, shutter_us=shutter, tnr=tnr)
            self.save_settings()
            await self._tear_down()
            await self._bring_up(mode)
            return self.status()

    async def stop(self):
        async with self.lock:
            await self._tear_down()
            return self.status()

    async def _bring_up(self, mode):
        self.state, self.busy, self.error, self.warnings = "starting", "start", None, []
        self.changed()
        try:
            for attempt in range(1, 4):
                fps = await self._start_chain(mode)
                target = MODES[mode]["fps"]
                if fps <= target * 1.02:
                    break
                # rkaiq sometimes ignores FpsValue and runs the sensor at its max rate
                log.warning("attempt %d: %.1f fps instead of %d, restarting", attempt, fps, target)
                await self._stop_chain()
            else:
                raise RuntimeError(f"frame rate stuck at {fps:.1f} fps (target {target})")
            if fps < target * 0.97:
                self.warnings.append(f"stream at {fps:.1f} fps, below target {target}")
            self.state = "active"
            self.left = None
            self._sync_restart()
            await self._sync_preview()
        except Exception as e:
            log.exception("start failed")
            await self._stop_chain()
            self.state, self.error = "error", str(e)
            raise
        finally:
            self.busy = None
            self.changed()

    async def _start_chain(self, mode):
        """rkaiq + mainpath. Returns the measured frame rate after it settles."""
        m = MODES[mode]
        self.mode = mode
        sensor, entity = find_sensor()
        main_dev = find_video("rkisp_mainpath")
        # Anything left from the old scripts would hold the devices
        await run("pkill", "-x", "gst-launch-1.0")
        await run("pkill", "-x", "rkaiq_3A_server")
        shutter = self.settings["shutter_us"]
        if shutter > max_shutter_us(mode):
            shutter = max_shutter_us(mode)
            self.warnings.append(f"shutter {self.settings['shutter_us']} us is longer than a "
                                 f"{mode} frame, using {shutter} us")
        write_active_iq(self.settings["preset"], m["iq_fps"], shutter / 1e6, self.settings["tnr"])
        sw, sh = m["sensor"]
        # The sensor's media entity lives in rkcif's media graph; find which /dev/media*
        for media in sorted(Path("/dev").glob("media*")):
            rc, _ = await run("media-ctl", "-d", str(media), "--set-v4l2",
                              f'"{entity}":0[fmt:SRGGB10_1X10/{sw}x{sh}]')
            if rc == 0:
                break
        else:
            raise RuntimeError("media-ctl could not set the sensor format")
        with open(AIQ_LOG, "wb") as aiq_log:
            self.aiq = await asyncio.create_subprocess_exec(
                "rkaiq_3A_server", stdout=aiq_log, stderr=asyncio.subprocess.STDOUT)
        await asyncio.sleep(3)  # rkaiq opens the sensor and loads the IQ file
        if self.aiq.returncode is not None:
            raise RuntimeError(f"rkaiq_3A_server exited ({self.aiq.returncode}), see {AIQ_LOG}")
        self.sensor_fd = open(sensor, "rb", buffering=0)

        w, h = m["out"]
        self.main = Gst.parse_launch(
            f"v4l2src name=src device={main_dev} io-mode=dmabuf "
            f"! video/x-raw,format=NV12,width={w},height={h},framerate={m['fps']}/1 "
            f"! tee name=t allow-not-linked=true ! fakevideosink sync=false async=false")
        self.tee = self.main.get_by_name("t")
        self.stats = FrameStats(m["fps"])
        stats = self.stats

        def on_buffer(pad, info):  # streaming thread
            buf = info.get_buffer()
            mem = buf.peek_memory(0)
            fd = GstAllocators.dmabuf_memory_get_fd(mem) if GstAllocators.is_dmabuf_memory(mem) else None
            stats.add(buf.pts, fd)
            if self.sync:
                self.loop.call_soon_threadsafe(self._on_frame, buf.pts)
            return Gst.PadProbeReturn.OK

        def on_query(pad, info):  # pin the pool: min = max = POOL_BUFFERS in every ALLOCATION reply
            q = info.get_query()
            if q.type == Gst.QueryType.ALLOCATION:
                if q.get_n_allocation_pools():
                    pool, size, _, _ = q.parse_nth_allocation_pool(0)
                    q.set_nth_allocation_pool(0, pool, size, POOL_BUFFERS, POOL_BUFFERS)
                else:
                    q.add_allocation_pool(None, 0, POOL_BUFFERS, POOL_BUFFERS)
            return Gst.PadProbeReturn.OK
        srcpad = self.main.get_by_name("src").get_static_pad("src")
        srcpad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)
        srcpad.add_probe(Gst.PadProbeType.QUERY_DOWNSTREAM | Gst.PadProbeType.PULL, on_query)
        if self.main.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("mainpath pipeline failed to start")
        for _ in range(50):
            await asyncio.sleep(0.1)
            self._check_bus(self.main, "mainpath")
            if stats.frames >= 5:
                break
        else:
            raise RuntimeError("mainpath: no frames from the ISP within 5 s")
        self.base = self.main.get_base_time()
        await asyncio.sleep(2.5)  # let the frame rate settle
        return stats.fps()

    async def _stop_chain(self):
        self._sync_stop()
        await self._stop_preview()
        if self.main:
            await self.loop.run_in_executor(None, self.main.set_state, Gst.State.NULL)
            self.main = self.tee = None
        if self.aiq and self.aiq.returncode is None:
            self.aiq.terminate()
            try:
                await asyncio.wait_for(self.aiq.wait(), 3)
            except asyncio.TimeoutError:
                self.aiq.kill()
                await self.aiq.wait()
        self.aiq = None
        if self.sensor_fd:
            self.sensor_fd.close()
            self.sensor_fd = None

    async def _tear_down(self):
        if self.state in ("idle",) and not self.main:
            return
        self.state, self.busy, self.error = "stopping", "stop", None
        self.changed()
        try:
            if self.rec:
                await self._record_stop()
            await self._stop_chain()
        finally:
            self.state, self.busy, self.mode, self.stats = "idle", None, None, None
            self.changed()

    # -- preview (a branch on the mainpath tee: exactly the frames being recorded)

    async def set_preview(self, enabled):
        async with self.lock:
            self.settings["preview"] = bool(enabled)
            self.save_settings()
            await self._sync_preview()
            return self.status()

    async def _sync_preview(self):
        want = self.settings["preview"] and self.state == "active"
        if want and not self.preview:
            self._start_preview()
        elif not want and self.preview:
            await self._stop_preview()
        self.changed()

    def _start_preview(self):
        pw, ph = MODES[self.mode]["preview"]
        # videorate runs in the tee's thread and drops all but preview_fps frames (cheap, C only).
        # The leaky 1-buffer queue moves scaling + JPEG to its own thread and never holds back
        # capture. videoscale reads the big frame on the CPU, but only a few times a second.
        branch = Gst.parse_bin_from_description(
            f"videorate drop-only=true max-rate={self.settings['preview_fps']} "
            f"! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream "
            f"! videoscale method=nearest-neighbour ! video/x-raw,width={pw},height={ph} "
            f"! jpegenc quality=80 ! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false",
            True)
        branch.set_name("preview")
        self.pstats = FrameStats(max(1, self.settings["preview_fps"]))
        branch.get_by_name("sink").connect("new-sample", self._on_jpeg)
        self.main.add(branch)
        branch.sync_state_with_parent()
        teepad = self.tee.request_pad_simple("src_%u")
        teepad.link(branch.get_static_pad("sink"))
        self.preview = {"bin": branch, "teepad": teepad}

    def _on_jpeg(self, sink):  # streaming thread
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            data = bytes(info.data)
            buf.unmap(info)
            self.pstats.add(buf.pts)
            self.loop.call_soon_threadsafe(self._publish_jpeg, data)
        return Gst.FlowReturn.OK

    def _publish_jpeg(self, data):
        self.jpeg, self.jpeg_seq = data, self.jpeg_seq + 1

        async def notify():
            async with self.jpeg_cond:
                self.jpeg_cond.notify_all()
        asyncio.ensure_future(notify())

    async def _stop_preview(self):
        if self.preview:
            pv, self.preview, self.pstats = self.preview, None, None
            if self.main:
                await self._unplug(pv["teepad"], pv["bin"])

    async def _unplug(self, teepad, branch, eos_done=None):
        """Remove a branch from the running tee. The IDLE probe waits until the tee pad isn't
        pushing a buffer, so unlinking can't cut a frame in half. With eos_done, EOS is sent
        into the branch and we wait for it to reach the end (finishes a recording file)."""
        sink = branch.get_static_pad("sink")
        unlinked = self.loop.create_future()

        def on_idle(pad, info):
            pad.unlink(sink)
            self.tee.release_request_pad(pad)
            if eos_done is not None:
                sink.send_event(Gst.Event.new_eos())
            self.loop.call_soon_threadsafe(lambda: unlinked.done() or unlinked.set_result(True))
            return Gst.PadProbeReturn.REMOVE
        teepad.add_probe(Gst.PadProbeType.IDLE, on_idle)
        try:
            await asyncio.wait_for(unlinked, 2)
            if eos_done is not None:
                await asyncio.wait_for(eos_done, 5)
        except asyncio.TimeoutError:
            log.warning("%s: unplug timed out", branch.get_name())
        await self.loop.run_in_executor(None, branch.set_state, Gst.State.NULL)
        self.main.remove(branch)

    # -- frame sync (camsync.py)

    async def set_sync(self, role):
        if role not in SYNC_ROLES:
            raise ValueError(f"sync role must be one of {', '.join(SYNC_ROLES)}")
        async with self.lock:
            if self.rec:
                raise RuntimeError("stop recording before changing frame sync")
            self.settings["sync"] = role
            self.save_settings()
            self._sync_restart()
            self.changed()
            return self.status()

    def _sync_restart(self):
        self._sync_stop()
        role = self.settings["sync"]
        if role == "off" or self.state != "active":
            return
        m = MODES[self.mode]
        svc = camsync.SyncService(role, self.ident["camera_id"], self.ident["ip"], self.mode, m["fps"],
                                  m["sensor"][1], find_sensor()[0], lambda: self.rec is not None)
        try:
            svc.start()
        except Exception as e:
            log.exception("frame sync failed to start")
            svc.stop()
            self.warnings.append(f"frame sync: {e}")
            return
        self.sync = svc

    def _sync_stop(self):
        if self.sync:
            self.sync.stop()
            self.sync = None

    def _on_frame(self, pts):  # event loop, every frame while sync runs
        if not self.sync:
            return
        try:
            self.sync.on_frame(pts + self.base)
        except Exception as e:
            log.exception("frame sync failed")
            self._sync_stop()
            self.warnings.append(f"frame sync stopped: {e}")
            self.changed()

    def sync_time(self, lead_ms=600):
        """Master: the time of a frame lead_ms ahead, on the master's clock. The client gives it
        to every camera as record_start/record_stop at_ts, so they all cut on the same frame."""
        if not self.sync or self.sync.role != "master":
            raise RuntimeError("this camera is not running as sync master")
        return {"ts": self.sync.schedule(int(float(lead_ms) * 1e6)), "camera_id": self.ident["camera_id"]}

    def _local_frame_time(self, at_ts):
        """at_ts (master clock) -> our CLOCK_MONOTONIC; refuses times already passed."""
        if not self.sync:
            raise RuntimeError("frame sync is off on this camera")
        local = self.sync.to_local(int(at_ts))
        if local < time.monotonic_ns():
            raise RuntimeError("the scheduled frame has already passed")
        return local

    # -- recording (a branch plugged into the running mainpath tee)

    async def record_start(self, bitrate_mbps=None, take=None, at_ts=None):
        """take: file name shared by every camera in a rig recording (the client picks it),
        so the cameras' files of one scene can be matched later. Without it the file is
        named after this camera and the time. at_ts: start with the frame at this time on the
        sync master's clock (from sync_time), so every camera's file starts on the same frame."""
        async with self.lock:
            if self.state != "active":
                raise RuntimeError("camera is not active; start it first")
            if self.rec:
                raise RuntimeError("already recording")
            if shutil.disk_usage(REC_DIR).free < MIN_FREE_GB * 1e9:
                raise RuntimeError(f"less than {MIN_FREE_GB} GB free")
            rec_dir = RAM_DIR if self.settings["rec_buffer"] == "ram" else REC_DIR
            if rec_dir == RAM_DIR:
                RAM_DIR.mkdir(parents=True, exist_ok=True)
                if space_left(RAM_DIR) < 1e9:
                    raise RuntimeError("less than 1 GB of RAM free for a RAM take "
                                       "(earlier takes still saving?)")
            if take is not None and not re.fullmatch(r"[\w.-]{1,100}", take):
                raise ValueError("bad take name (letters, digits, _ . - only)")
            if bitrate_mbps:
                self.settings["bitrate_mbps"] = int(bitrate_mbps)
                self.save_settings()
            m = MODES[self.mode]
            bps = self.settings["bitrate_mbps"] * 1_000_000
            name = take or f"{self.ident['camera_id']}_{time.strftime('%Y%m%d_%H%M%S')}_{self.mode}"
            path = rec_dir / f"{name}.mkv"
            if (REC_DIR / path.name).exists() or (RAM_DIR / path.name).exists():
                raise RuntimeError(f"{path.name} already exists on this camera")
            start_local = self._local_frame_time(at_ts) if at_ts is not None else None
            # Leaky queue: if the encoder falls behind it drops frames here (counted)
            # instead of blocking the tee and stalling capture.
            branch = Gst.parse_bin_from_description(
                f"queue name=q max-size-buffers=6 max-size-bytes=0 max-size-time=0 leaky=downstream "
                f"! mpph265enc auto-tile=true bps={bps} bps-max={bps * 3 // 2} rc-mode=vbr gop={m['fps']} "
                f"! h265parse ! matroskamux ! filesink name=fs location={path}", True)
            rec = {"bin": branch, "path": path, "started": time.time(), "in": 0, "leaked": 0,
                   "drops0": self.stats.dropped}
            sink = branch.get_static_pad("sink")
            # Shift timestamps so the file starts at 0 instead of the pipeline's uptime (the
            # offset goes out with the segment event at link time, so it can't change later).
            # Synced start: the first frame lands ~1 ms in; a frame a hair before 0 would be
            # dropped by the encoder as outside the segment, and it's the keyframe.
            sink.set_offset(-int(start_local - self.base - 1_000_000 if start_local is not None
                                 else self.stats.last_pts or 0))
            sink.add_probe(Gst.PadProbeType.BUFFER, lambda p, i: (rec.__setitem__("in", rec["in"] + 1),
                                                                  Gst.PadProbeReturn.OK)[1])
            # "overrun" fires when the queue is full, i.e. each time it leaks a frame. No Python
            # runs on the queue -> encoder thread: a callback there would need the GIL every frame.
            branch.get_by_name("q").connect("overrun", lambda q: rec.__setitem__("leaked", rec["leaked"] + 1))
            self.main.add(branch)
            branch.sync_state_with_parent()
            rec["teepad"] = self.tee.request_pad_simple("src_%u")
            if start_local is not None:
                rec["teepad"].add_probe(Gst.PadProbeType.BUFFER,
                                        self._start_gate(start_local, 1e9 / m["fps"]))
                rec["started"] = time.time() + (start_local - time.monotonic_ns()) / 1e9
            rec["teepad"].link(sink)
            if rec_dir == RAM_DIR:
                rec["spill"] = Spill(path)
                rec["drainer"] = asyncio.create_task(self._drainer(rec["spill"]))
            self.rec = rec
            ex = self._exposure() or {}
            log.info("recording %s at %d Mbit/s%s%s; preset %s, shutter %s, exposure %s us, gain %sx, tnr %s",
                     path.name, bps // 1_000_000,
                     " (synced start)" if start_local is not None else "",
                     " into RAM" if rec_dir == RAM_DIR else "",
                     self.settings["preset"], self.settings["shutter_us"] or "auto",
                     ex.get("us"), ex.get("gain"), self.settings["tnr"])
            self.changed()
            return self.status()

    def _start_gate(self, start_local, period):
        """Tee pad probe: drop frames until the scheduled one, then get out of the way."""
        base = self.base

        def gate(pad, info):  # streaming thread
            if info.get_buffer().pts + base < start_local - period / 2:
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.REMOVE
        return gate

    async def record_stop(self, at_ts=None):
        """at_ts: the last frame to record, on the sync master's clock (from sync_time)."""
        async with self.lock:
            if not self.rec:
                raise RuntimeError("not recording")
            if at_ts is not None:
                await self._stop_gate(self._local_frame_time(at_ts))
            result = await self._record_stop()
            self.changed()
            return result

    async def _stop_gate(self, stop_local):
        """Drop every frame after stop_local and wait until one arrives (= the last frame
        to record has gone into the branch)."""
        period = 1e9 / MODES[self.mode]["fps"]
        base = self.base
        past = self.loop.create_future()

        def gate(pad, info):  # streaming thread
            if info.get_buffer().pts + base > stop_local + period / 2:
                self.loop.call_soon_threadsafe(lambda: past.done() or past.set_result(True))
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK
        self.rec["teepad"].add_probe(Gst.PadProbeType.BUFFER, gate)
        try:
            await asyncio.wait_for(past, (stop_local - time.monotonic_ns()) / 1e9 + 2)
        except asyncio.TimeoutError:
            log.warning("synced stop: no frame after the stop time, stopping now")

    async def _record_stop(self):
        """Unplug the branch from the tee, then push EOS through it so matroskamux writes
        its index and the file is finished; only then remove the branch."""
        rec, self.rec = self.rec, None
        status = {"file": rec["path"].name, "seconds": round(time.time() - rec["started"], 1),
                  "frames": rec["in"] - rec["leaked"],
                  "dropped": (self.stats.dropped - rec["drops0"]) + rec["leaked"]}
        done = self.loop.create_future()

        def on_eos(pad, info):
            if info.get_event().type == Gst.EventType.EOS:
                self.loop.call_soon_threadsafe(lambda: done.done() or done.set_result(True))
            return Gst.PadProbeReturn.OK
        rec["bin"].get_by_name("fs").get_static_pad("sink").add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM, on_eos)

        await self._unplug(rec["teepad"], rec["bin"], eos_done=done)
        status["bytes"] = rec["path"].stat().st_size if rec["path"].exists() else 0
        log.info("recorded %s: %d frames, %.1f s", status["file"], status["frames"], status["seconds"])
        if rec.get("spill"):
            rec["spill"].stopping = True
            await rec["drainer"]  # lets a move in progress complete
            self.save_queue.put_nowait(rec["spill"])
            status["saving"] = True
        return status

    async def _move(self, spill, step, during_take):
        """Run one Spill step (drain or finish) on a worker thread, with progress in status."""
        start = spill.moved
        self.saving = {"file": spill.src.name, "during_take": during_take,
                       "mb": round((os.path.getsize(spill.src) - start) / 1e6), "done_mb": 0}
        self.changed()
        t0, m0 = time.monotonic(), spill.moved
        try:
            await self.loop.run_in_executor(
                None, getattr(spill, step), lambda off: self.saving.__setitem__("done_mb", round((off - start) / 1e6)))
        finally:
            self.saving = None
            self.changed()
        return time.monotonic() - t0, (spill.moved - m0) / 1e6

    async def _drainer(self, spill):
        """While a RAM take records: move it to the NVMe each time DRAIN_START_GB has built up
        (or RAM runs low), in one steady paced run, and free that part of RAM."""
        while not spill.stopping:
            await asyncio.sleep(1)
            if spill.stopping or not (spill.pending() >= DRAIN_START_GB * 1e9 or
                                      space_left(RAM_DIR) < 0.5e9):
                continue
            try:
                secs, mb = await self._move(spill, "drain", True)
                log.info("moved %.0f MB of %s to NVMe in %.1f s (move %d, take continues)",
                         mb, spill.src.name, secs, spill.drains)
            except Exception as e:
                log.exception("moving %s to NVMe failed", spill.src.name)
                self.warnings.append(f"moving the take to the NVMe failed: {e}; it stays in RAM, "
                                     f"the take stops when RAM is full")
                self.changed()
                return

    async def saver(self):
        """Background task: finish RAM takes (move the rest, rename) one at a time, paced."""
        while True:
            spill = await self.save_queue.get()
            try:
                secs, _ = await self._move(spill, "finish", False)
                log.info("saved %s to NVMe (%d moves during the take, last %.1f s)",
                         spill.dst.name, spill.drains, secs)
            except Exception as e:
                log.exception("saving %s failed", spill.src.name)
                self.warnings.append(f"saving {spill.src.name} to NVMe failed: {e} (rest of the file "
                                     f"is still in RAM, lost at reboot)")
            finally:
                self.save_queue.task_done()
                self.changed()

    # -- health

    def _check_bus(self, pipeline, label):
        bus = pipeline.get_bus()
        while msg := bus.pop():
            if msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                if self.preview and msg.src.has_as_ancestor(self.preview["bin"]):
                    raise PreviewError(f"preview: {err.message}")
                raise RuntimeError(f"{label}: {err.message}")
            if msg.type == Gst.MessageType.WARNING:
                w, _ = msg.parse_warning()
                log.warning("%s: %s", label, w.message)

    async def watch(self):
        """Background health check: pipeline errors, stalled capture, rkaiq dying."""
        while True:
            await asyncio.sleep(0.5)
            if self.state != "active" or self.lock.locked():
                continue
            problem = None
            try:
                self._check_bus(self.main, "mainpath")
            except PreviewError as e:  # capture-first: a preview failure only drops the preview
                log.error(e)
                self.warnings.append(f"{e} (preview stopped)")
                async with self.lock:
                    self.settings["preview"] = False
                    await self._stop_preview()
                self.changed()
            except RuntimeError as e:
                problem = str(e)
            if not problem and time.monotonic() - self.stats.last_wall > 3:
                problem = "capture stalled: no frames for 3 s"
            if not problem and self.aiq and self.aiq.returncode is not None:
                problem = f"rkaiq_3A_server exited ({self.aiq.returncode})"
            ram_full = self.rec and self.rec.get("spill") and space_left(RAM_DIR) < 0
            if not problem and self.rec and (ram_full or space_left(REC_DIR) < 0):
                # Stop cleanly before the filesystem is full (a full one errors the whole chain).
                # RAM only fills if moving to the NVMe fails or can't keep up.
                where = "RAM" if ram_full else "disk"
                secs = round(time.time() - self.rec["started"])
                try:
                    await self.record_stop()
                except RuntimeError:
                    pass
                self.warnings.append(f"take stopped after {secs} s: {where} full")
                log.warning("take stopped after %d s: %s full", secs, where)
                self.changed()
                continue
            if not problem and self._left_core_frozen():
                await self._recover_left_core()
                continue
            if problem:
                log.error(problem)
                async with self.lock:
                    await self._tear_down()
                    self.state, self.error = "error", problem
                    self.changed()

    def _left_core_frozen(self):
        """Sampled about once a second from watch(): True once the left ISP core has timed out
        on more than FROZEN_RATE of the frames for FROZEN_SECONDS. A late core (NVMe bursts)
        times out on a few frames per second; a frozen one on all of them, until restarted."""
        n = unite_timeouts()
        if n is None:
            return False
        now = time.monotonic()
        if self.left is None:
            self.left = (now, n, 0.0)
            return False
        t0, n0, frozen = self.left
        if now - t0 < 0.9:
            return False
        rate = (n - n0) / (now - t0) / MODES[self.mode]["fps"]
        frozen = frozen + (now - t0) if rate > FROZEN_RATE else 0.0
        self.left = (now, n, frozen)
        return frozen >= FROZEN_SECONDS

    async def _recover_left_core(self):
        """Restart the chain (~6 s gap). A recording is finished and continues in a new file
        <name>_rN.mkv; a synced start is lost for that file (the rig's other cameras go on)."""
        self.recoveries += 1
        when = time.strftime("%H:%M:%S")
        log.error("left ISP core frozen (timeouts on every frame), restarting the chain")
        mode, rec = self.mode, self.rec
        try:
            async with self.lock:
                await self._tear_down()   # finishes the recording file first
                await self._bring_up(mode)
        except Exception:
            log.exception("restart after frozen left core failed")  # _bring_up set state=error
            return
        msg = f"left ISP core froze at {when}; chain restarted ({self.recoveries} since camd start)"
        if rec:
            stem = re.sub(r"_r\d+$", "", rec["path"].stem)
            try:
                await self.record_start(take=f"{stem}_r{self.recoveries}")
                msg += f", recording continues in {self.rec['path'].name}"
            except Exception as e:
                log.exception("could not resume recording")
                msg += f", recording NOT resumed: {e}"
        self.warnings.append(msg)
        log.warning(msg)
        self.changed()

    # -- recordings on disk

    def recordings(self):
        current = self.rec["path"].name if self.rec else None
        out = []
        # RAM takes are listed too (in_ram: recording, or the last part still being saved);
        # they can't be downloaded until saved (moved parts are freed from RAM)
        files = list(REC_DIR.glob("*.mkv")) + list(RAM_DIR.glob("*.mkv"))
        for p in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
            st = p.stat()
            out.append({"name": p.name, "bytes": st.st_size, "mtime": st.st_mtime,
                        "recording": p.name == current, "in_ram": p.parent == RAM_DIR})
        return out

    def recording_path(self, name):
        if not re.fullmatch(r"[\w.-]+\.mkv", name or ""):
            raise ValueError("bad recording name")
        if (REC_DIR / name).is_file():
            return REC_DIR / name
        if (RAM_DIR / name).is_file():
            raise RuntimeError("that take is still being saved to the NVMe, try again shortly")
        raise FileNotFoundError(name)

    def delete_recording(self, name):
        p = self.recording_path(name)
        if self.rec and self.rec["path"] == p:
            raise RuntimeError("that file is being recorded")
        p.unlink()
        return {"deleted": name}

    # -- device: bitrate setting, network identity, power

    def set_bitrate(self, mbps):
        mbps = int(mbps)
        if not 5 <= mbps <= 200:
            raise ValueError("bitrate must be 5-200 Mbit/s")
        self.settings["bitrate_mbps"] = mbps
        self.save_settings()
        self.changed()
        return self.status()

    def set_shutter(self, us):
        """Active: start() restarts the chain (rkaiq reads the IQ file only at start).
        Otherwise saved for the next start."""
        if self.state == "active":
            return self.start(shutter_us=us)
        self.settings["shutter_us"] = check_shutter(us)
        self.save_settings()
        self.changed()
        return self.status()

    def set_tnr(self, tnr):
        """Same as set_shutter: restarts an active chain, else saved for the next start."""
        if tnr not in TNR_MODES:
            raise ValueError(f"tnr must be one of {', '.join(TNR_MODES)}")
        if self.state == "active":
            return self.start(tnr=tnr)
        self.settings["tnr"] = tnr
        self.save_settings()
        self.changed()
        return self.status()

    def _later(self, coro_fn):
        """Run after the reply has gone out (the connection may not survive the action)."""
        async def go():
            await asyncio.sleep(0.5)
            try:
                await coro_fn()
            except Exception:
                log.exception("deferred action failed")
        asyncio.ensure_future(go())

    async def power(self, action):
        """reboot | shutdown. A running recording is finished first (complete MKV)."""
        cmd = {"reboot": "reboot", "shutdown": "poweroff"}[action]

        async def go():
            async with self.lock:
                await self._tear_down()
            if self.save_queue.qsize() or self.saving:  # RAM takes are lost at power-off
                log.info("%s: waiting for RAM takes to be saved", action)
                await self.save_queue.join()
            log.info("%s requested", action)
            rc, out = await run("sudo", "-n", "/usr/bin/systemctl", cmd)
            if rc:
                log.error("systemctl %s failed: %s", cmd, out)
        self._later(go)
        return {"action": action}

    def set_network(self, ip):
        """Give this camera its static IP (= its ID: 192.168.1.150 -> cam150). Written to the
        config file and applied right away by camrig-net; the camera reappears at the new
        address within a few seconds, and keeps it across reboots."""
        try:
            addr = ipaddress.IPv4Address(ip)
        except ValueError:
            raise ValueError(f"not an IPv4 address: {ip}")
        net = ipaddress.ip_network("192.168.0.0/23")
        if addr not in net or addr in (net.network_address, net.broadcast_address):
            raise ValueError(f"{ip} is not in the rig network {net}")
        if str(addr) == BASE_IP:
            raise ValueError(f"{BASE_IP} is reserved for new, unconfigured cameras")
        if self.rec:
            raise RuntimeError("stop recording first")
        conf = {**load_json(CAMERA_CONF, {}), "ip": str(addr)}
        tmp = CAMERA_CONF.with_name(".camera.json.tmp")
        tmp.write_text(json.dumps(conf, indent=1) + "\n")
        os.replace(tmp, CAMERA_CONF)

        async def go():
            rc, out = await run("sudo", "-n", NET_HELPER, timeout=30)
            log.info("camrig-net: %s", out) if rc == 0 else log.error("camrig-net failed: %s", out)
            self.ident = identity()
            self.changed()
        self._later(go)
        return {"ip": str(addr), "camera_id": f"cam{addr.packed[-1]}"}


# ---------------------------------------------------------------- control server (TCP JSON lines)

class ControlServer:
    def __init__(self, cam):
        self.cam = cam
        self.commands = {
            "hello": lambda a: cam.ident,
            "status": lambda a: cam.status(),
            "get_config": lambda a: {"modes": MODES, "presets": presets(), "settings": cam.settings,
                                     "shutter_max_us": {m: max_shutter_us(m) for m in MODES},
                                     "tnr_modes": list(TNR_MODES)},
            "start": lambda a: cam.start(a.get("mode"), a.get("preset"), a.get("preview"),
                                         a.get("shutter_us"), a.get("tnr")),
            "stop": lambda a: cam.stop(),
            "set_preset": lambda a: cam.start(preset=a["preset"]) if cam.state == "active"
            else self._set_setting("preset", a["preset"], presets()),
            "set_mode": lambda a: cam.start(mode=a["mode"]) if cam.state == "active"
            else self._set_setting("mode", a["mode"], MODES),
            "set_preview": lambda a: cam.set_preview(a["enabled"]),
            "set_bitrate": lambda a: cam.set_bitrate(a["mbps"]),
            "set_shutter": lambda a: cam.set_shutter(a["us"]),
            "set_tnr": lambda a: cam.set_tnr(a["tnr"]),
            "set_rec_buffer": lambda a: self._set_setting("rec_buffer", a["buffer"], REC_BUFFERS),
            "record_start": lambda a: cam.record_start(a.get("bitrate_mbps"), a.get("take"), a.get("at_ts")),
            "record_stop": lambda a: cam.record_stop(a.get("at_ts")),
            "set_sync": lambda a: cam.set_sync(a["role"]),
            "sync_time": lambda a: cam.sync_time(a.get("lead_ms", 600)),
            "list_recordings": lambda a: cam.recordings(),
            "delete_recording": lambda a: cam.delete_recording(a["name"]),
            "set_network": lambda a: cam.set_network(a["ip"]),
            "reboot": lambda a: cam.power("reboot"),
            "shutdown": lambda a: cam.power("shutdown"),
        }

    def _set_setting(self, key, value, allowed):
        if value not in allowed:
            raise ValueError(f"unknown {key} {value}")
        self.cam.settings[key] = value
        self.cam.save_settings()
        self.cam.changed()
        return self.cam.status()

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        log.info("client %s connected", peer)
        wlock = asyncio.Lock()
        pending = asyncio.Event()

        async def send(obj):
            if writer.is_closing():
                return
            async with wlock:
                try:
                    writer.write(json.dumps(obj).encode() + b"\n")
                    await writer.drain()
                except ConnectionError:
                    writer.close()

        def on_change():
            pending.set()
        self.cam.listeners.add(on_change)

        async def push_status():
            while True:
                interval = 1 if self.cam.state in ("active", "starting", "stopping") else 5
                try:
                    await asyncio.wait_for(pending.wait(), interval)
                except asyncio.TimeoutError:
                    pass
                pending.clear()
                await send({"event": "status", "data": self.cam.status()})

        async def do(req):
            rid = req.get("id")
            try:
                fn = self.commands.get(req.get("cmd"))
                if not fn:
                    raise ValueError(f"unknown command {req.get('cmd')!r}")
                result = fn(req)
                if asyncio.iscoroutine(result):
                    result = await result
                await send({"id": rid, "ok": True, "result": result})
            except Exception as e:
                await send({"id": rid, "ok": False, "error": str(e) or type(e).__name__})

        pusher = asyncio.create_task(push_status())
        tasks = set()
        try:
            while line := await reader.readline():
                try:
                    req = json.loads(line)
                except ValueError:
                    await send({"ok": False, "error": "invalid JSON"})
                    continue
                t = asyncio.create_task(do(req))  # long commands don't block status/other requests
                tasks.add(t)
                t.add_done_callback(tasks.discard)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.cam.listeners.discard(on_change)
            pusher.cancel()
            writer.close()
            log.info("client %s disconnected", peer)


# ---------------------------------------------------------------- data server (minimal HTTP)

class HttpServer:
    def __init__(self, cam):
        self.cam = cam

    async def handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            method, target, _ = head.split(b"\r\n", 1)[0].decode().split(" ", 2)
            path = target.split("?", 1)[0]
            if method not in ("GET", "HEAD"):
                await self.reply(writer, 405, b"GET only\n")
            elif path == "/":
                body = json.dumps({**self.cam.ident, "status": self.cam.status()}, indent=1).encode()
                await self.reply(writer, 200, body, "application/json")
            elif path == "/preview.jpg":
                if self.cam.jpeg and self.cam.preview:
                    await self.reply(writer, 200, self.cam.jpeg, "image/jpeg")
                else:
                    await self.reply(writer, 503, b"no preview\n")
            elif path == "/preview.mjpg":
                await self.mjpeg(writer)
            elif path.startswith("/recordings/"):
                await self.download(writer, path[len("/recordings/"):], method == "HEAD")
            else:
                await self.reply(writer, 404, b"not found\n")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                ConnectionError, ValueError):
            pass
        finally:
            writer.close()

    async def reply(self, writer, code, body, ctype="text/plain"):
        reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
                  503: "Service Unavailable"}[code]
        writer.write(f"HTTP/1.1 {code} {reason}\r\nContent-Type: {ctype}\r\n"
                     f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                     f"Connection: close\r\n\r\n".encode() + body)
        await writer.drain()

    async def mjpeg(self, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\n"
                     b"Cache-Control: no-store\r\nConnection: close\r\n\r\n")
        seq = -1
        while True:
            async with self.cam.jpeg_cond:
                try:
                    await asyncio.wait_for(
                        self.cam.jpeg_cond.wait_for(lambda: self.cam.jpeg_seq != seq), 10)
                except asyncio.TimeoutError:
                    return  # preview stopped; the viewer can reconnect
            seq, data = self.cam.jpeg_seq, self.cam.jpeg
            writer.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(data)
                         + data + b"\r\n")
            await writer.drain()

    async def download(self, writer, name, head_only):
        try:
            p = self.cam.recording_path(name)
        except (ValueError, FileNotFoundError):
            return await self.reply(writer, 404, b"no such recording\n")
        if self.cam.rec and self.cam.rec["path"] == p:
            return await self.reply(writer, 409, b"still recording\n")
        size = p.stat().st_size
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: video/x-matroska\r\nContent-Length: {size}\r\n"
                     f"Content-Disposition: attachment; filename=\"{p.name}\"\r\n"
                     f"Connection: close\r\n\r\n".encode())
        await writer.drain()
        if not head_only:
            with open(p, "rb") as f:
                # sendfile: the kernel copies file -> socket directly, no Python in the loop
                await asyncio.get_running_loop().sendfile(writer.transport, f)


# ---------------------------------------------------------------- main

async def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    Gst.init(None)
    REC_DIR.mkdir(parents=True, exist_ok=True)
    cam = Camera()
    log.info("camd %s: %s (%s), presets %s", VERSION, cam.ident["camera_id"], cam.ident["ip"],
             ", ".join(presets()))
    ctl = await asyncio.start_server(ControlServer(cam).handle, "0.0.0.0", CONTROL_PORT)
    http = await asyncio.start_server(HttpServer(cam).handle, "0.0.0.0", HTTP_PORT)
    watcher = asyncio.create_task(cam.watch())
    # RAM takes left by an earlier camd (tmpfs outlives camd, not a reboot): save them now
    RAM_DIR.mkdir(parents=True, exist_ok=True)
    for p in sorted(RAM_DIR.glob("*.mkv")):
        log.info("found unsaved RAM take %s", p.name)
        cam.save_queue.put_nowait(Spill(p))  # its .spill.json says how much is on the NVMe already
    saver = asyncio.create_task(cam.saver())

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("shutting down")
    watcher.cancel()
    saver.cancel()  # an interrupted copy restarts from scratch at the next start
    ctl.close()
    http.close()
    async with cam.lock:
        await cam._tear_down()  # finishes any recording cleanly


if __name__ == "__main__":
    asyncio.run(main())
