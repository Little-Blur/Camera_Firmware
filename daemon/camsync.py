"""camsync: network frame sync between cameras (master / follower), used by camd.

Same idea as the Raspberry Pi libcamera sync (src/ipa/rpi/controller/rpi/sync.cpp): one camera,
the master, multicasts where its frames are in time; the followers lengthen or shorten single
frames until their frames start at the same moment. Differences, because of our hardware:

  timebase  libcamera assumes the system clocks already agree (NTP/PTP). Our NICs (yt6801) have
            no hardware timestamping and chrony to the internet is only good to ~1 ms, so a
            follower measures the master's CLOCK_MONOTONIC itself: ping/pong round trips with
            kernel receive timestamps, best (lowest round-trip) sample per half second, and a
            line through those over 10 s (the two crystals drift by a few ppm).
  frames    the ISP timestamps jitter by ~30 us (taken in an IRQ handler) while the sensor's
            real period is stable to ppm, so FrameClock fits a line through recent frames and
            uses that instead (libcamera does the same in ClockRecovery).
  control   one frame is stretched/shortened through the sensor's VBLANK control: set it right
            after a frame arrives, put it back on the next one -> exactly one frame changes
            (checked on cam145: +100 lines = +650 us on one frame). rkaiq doesn't touch VBLANK
            while the frame rate is fixed. Shortening needs VBLANK above the driver's minimum and
            room above the exposure; uhd60 runs at the minimum, so there a follower can only
            lengthen (it slips a whole frame if it falls behind, unless recording).
  start     no "ready frame": the client asks the master for a frame time a bit in the future
            (schedule) and every camera starts, and later stops, recording on that frame.

Messages (UDP, JSON, port 7071): master -> group 239.255.70.71 {"t": "sync", ...} every 6 frames;
follower -> master {"t": "ping"} 20x/s, master -> follower {"t": "pong"}.
"""
import asyncio
import fcntl
import json
import logging
import math
import socket
import struct
import time
from collections import deque

GROUP = "239.255.70.71"
PORT = 7071
SEND_EVERY = 6              # master: frames between sync packets
PING_INTERVAL = 0.05        # follower: seconds between pings
HOLD_FRAMES = 6             # frames to ignore after a correction (in flight: ~1.5 frames latency)
FIT_MIN = 30                # frames in the fit before a follower acts on it
DEADBAND_NS = 30_000        # don't correct errors smaller than this
LOCK_NS = 100_000           # |error| under this for 3 checks in a row = locked
SLIP_NS = 2_000_000         # while recording, slip a whole frame only for errors above this
MAX_STRETCH = 0.45          # longest single-frame stretch, in periods (FrameStats counts >1.5 as a drop)
PACKET_TIMEOUT = 2.0        # seconds without a master packet = master lost

V4L2_CID_EXPOSURE = 0x00980911
V4L2_CID_VBLANK = 0x009e0901
VIDIOC_G_CTRL = 0xc008561b
VIDIOC_S_CTRL = 0xc008561c
VIDIOC_QUERYCTRL = 0xc0445624
QUERYCTRL = struct.Struct("II32siiiiI2I")
EXPOSURE_OFFSET = 22        # imx477.c IMX477_EXPOSURE_OFFSET: exposure <= frame length - 22 lines
SO_TIMESTAMPNS = 35         # Linux: kernel receive timestamp (CLOCK_REALTIME) on each datagram

log = logging.getLogger("camd.sync")


def get_ctrl(fd, cid):
    buf = bytearray(struct.pack("Ii", cid, 0))
    fcntl.ioctl(fd, VIDIOC_G_CTRL, buf)
    return struct.unpack("Ii", buf)[1]


def set_ctrl(fd, cid, value):
    fcntl.ioctl(fd, VIDIOC_S_CTRL, bytearray(struct.pack("Ii", cid, value)))


def ctrl_min(fd, cid):
    buf = bytearray(QUERYCTRL.size)
    struct.pack_into("I", buf, 0, cid)
    fcntl.ioctl(fd, VIDIOC_QUERYCTRL, buf)
    return QUERYCTRL.unpack(buf)[3]


def realtime_minus_mono():
    return time.clock_gettime_ns(time.CLOCK_REALTIME) - time.monotonic_ns()


class FrameClock:
    """Fits ts = a + period * k through the last frames (k counts dropped frames too)."""

    def __init__(self, period_ns, maxlen=120):
        self.nominal = period_ns
        self.frames = deque(maxlen=maxlen)
        self.k = -1
        self.last = None

    def reset(self):
        self.frames.clear()

    def add(self, ts):
        if self.last is not None:
            self.k += max(1, round((ts - self.last) / self.nominal))
        else:
            self.k = 0
        self.last = ts
        self.frames.append((self.k, ts))
        return self.k

    def fit(self, min_frames=FIT_MIN):
        """(k, de-jittered ts of frame k, period) for the newest frame, or None."""
        pts = list(self.frames)
        if len(pts) < min_frames:
            return None
        for _ in range(2):  # second pass without the outliers of the first
            k0, t0 = pts[0]
            n = len(pts)
            xs = [k - k0 for k, _ in pts]
            ys = [t - t0 for _, t in pts]
            mx, my = sum(xs) / n, sum(ys) / n
            sxx = sum((x - mx) ** 2 for x in xs)
            if sxx == 0:
                return None
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
            res = [y - (my + slope * (x - mx)) for x, y in zip(xs, ys)]
            sd = math.sqrt(sum(r * r for r in res) / n)
            keep = [p for p, r in zip(pts, res) if abs(r) <= 2.5 * sd + 1]
            if len(keep) == len(pts) or len(keep) < min_frames:
                break
            pts = keep
        k_last = self.frames[-1][0]
        return k_last, t0 + my + slope * (k_last - k0 - mx), slope


class OffsetModel:
    """master CLOCK_MONOTONIC - our CLOCK_MONOTONIC, from ping/pong round trips."""
    BUCKET = 0.5e9

    def __init__(self):
        self.buckets = deque(maxlen=20)  # (bucket, rtt, t_mid, offset): best sample per 0.5 s

    def add(self, t1, t2, t3, t4):
        rtt = (t4 - t1) - (t3 - t2)
        if rtt < 0:
            return
        offset = ((t2 - t1) + (t3 - t4)) / 2
        b = int(t4 // self.BUCKET)
        sample = (b, rtt, (t1 + t4) / 2, offset)
        if self.buckets and self.buckets[-1][0] == b:
            if rtt < self.buckets[-1][1]:
                self.buckets[-1] = sample
        else:
            self.buckets.append(sample)

    def valid(self, now):
        return len(self.buckets) >= 3 and now - self.buckets[-1][2] < 2e9

    def offset(self, t):
        pts = [(tm, off) for _, _, tm, off in self.buckets]
        n = len(pts)
        mx = sum(p[0] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        sxx = sum((x - mx) ** 2 for x, _ in pts)
        slope = sum((x - mx) * (y - my) for x, y in pts) / sxx if n >= 4 and sxx else 0.0
        return my + slope * (t - mx)

    def quality(self):
        """(best round trip, scatter of the samples around the line), in us"""
        if not self.buckets:
            return None, None
        best = min(b[1] for b in self.buckets) / 1e3
        if len(self.buckets) < 4:
            return best, None
        res = [off - self.offset(tm) for _, _, tm, off in self.buckets]
        return best, math.sqrt(sum(r * r for r in res) / len(res)) / 1e3


class SyncService:
    """One sync session: runs while the capture chain is active and the role isn't off."""

    def __init__(self, role, cam_id, my_ip, mode, fps, height, sensor_dev, is_recording):
        self.role, self.cam_id, self.my_ip = role, cam_id, my_ip
        self.mode, self.fps, self.height = mode, fps, height
        self.sensor_dev = sensor_dev
        self.is_recording = is_recording
        self.period = 1e9 / fps
        self.clock = FrameClock(self.period)
        self.offset = OffsetModel()
        self.sock = self.fd = None
        self.loop = asyncio.get_running_loop()
        self.pinger = None
        self.master = None            # follower: newest packet + who sent it
        self.other_masters = {}       # cam_id -> last seen (a second master is a mistake)
        self.pending_restore = None   # VBLANK value to put back on the next frame
        self.hold = 0
        self.seq = 0
        self.state = "starting"
        self.error_ns = None
        self.good = 0
        self.locked = False
        self.corrections = self.slips = self.no_headroom = 0
        self.last_correction_ns = 0
        self.ping_id = 0

    # -- life cycle

    def start(self):
        self.fd = open(self.sensor_dev, "rb", buffering=0)
        self.vblank_min = ctrl_min(self.fd.fileno(), V4L2_CID_VBLANK)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        s.bind(("0.0.0.0", PORT))
        iface = socket.inet_aton(self.my_ip)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(GROUP) + iface)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, iface)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        s.setblocking(False)
        self.sock = s
        self.loop.add_reader(s.fileno(), self._on_readable)
        if self.role == "follower":
            self.pinger = asyncio.ensure_future(self._ping_loop())
            self.state = "waiting for master"
        else:
            self.state = "master"
        log.info("sync %s started (%s, VBLANK min %d)", self.role, self.mode, self.vblank_min)

    def stop(self):
        if self.pinger:
            self.pinger.cancel()
        if self.pending_restore is not None and self.fd:
            try:
                set_ctrl(self.fd.fileno(), V4L2_CID_VBLANK, self.pending_restore)
            except OSError:
                pass
        if self.sock:
            self.loop.remove_reader(self.sock.fileno())
            self.sock.close()
        if self.fd:
            self.fd.close()
        self.sock = self.fd = None

    # -- frames (called on the event loop for every captured frame, ts = CLOCK_MONOTONIC ns)

    def on_frame(self, ts):
        self.clock.add(ts)
        if self.pending_restore is not None:
            set_ctrl(self.fd.fileno(), V4L2_CID_VBLANK, self.pending_restore)
            self.pending_restore = None
            self.hold = HOLD_FRAMES
            return
        if self.hold:
            self.hold -= 1
            if not self.hold:
                self.clock.reset()  # the fit must not span the stretched frame
            return
        self.seq += 1
        if self.role == "master":
            if self.seq % SEND_EVERY == 0:
                self._send_sync()
        elif self.seq % 3 == 0:
            self._follow()

    def _send_sync(self):
        fit = self.clock.fit()
        if not fit:
            return
        k, ts, period = fit
        msg = {"t": "sync", "cam": self.cam_id, "mode": self.mode, "fps": self.fps,
               "k": k, "ts": int(ts), "period": period}
        try:
            self.sock.sendto(json.dumps(msg).encode(), (GROUP, PORT))
        except OSError as e:
            log.warning("sync send: %s", e)

    # -- follower

    def _follow(self):
        now = time.monotonic_ns()
        m = self.master
        if not m or time.monotonic() - m["seen"] > PACKET_TIMEOUT:
            self.state, self.locked, self.good = "waiting for master", False, 0
            return
        if m["mode"] != self.mode or m["fps"] != self.fps:
            self.state, self.locked = f"mode mismatch (master {m['cam']} runs {m['mode']})", False
            return
        if not self.offset.valid(now):
            self.state = "measuring clock offset"
            return
        fit = self.clock.fit()
        if not fit:
            return
        _, t_mine, my_period = fit
        period = m["period"]
        t_master = m["ts"] - self.offset.offset(now)          # master's frame, in our clock
        e = (t_mine - t_master + period / 2) % period - period / 2  # >0: our frames start late
        self.error_ns = e
        self.good = self.good + 1 if abs(e) < LOCK_NS else 0
        self.locked = self.good >= 3
        self.state = "locked" if self.locked else "locking"
        if abs(e) >= DEADBAND_NS:
            self._correct(e, my_period)

    def _correct(self, e, period):
        fd = self.fd.fileno()
        vb = get_ctrl(fd, V4L2_CID_VBLANK)
        line = period / (self.height + vb)
        max_stretch = int(MAX_STRETCH * period / line)
        if e < 0:   # early: lengthen one frame
            delta = min(round(-e / line), max_stretch)
        else:       # late: shorten one frame, within VBLANK headroom and above the exposure
            exposure = get_ctrl(fd, V4L2_CID_EXPOSURE)
            room = min(vb - self.vblank_min, self.height + vb - exposure - EXPOSURE_OFFSET - 2)
            delta = -min(round(e / line), max(room, 0))
            if delta == 0:
                self.no_headroom += 1
                if self.is_recording() and e < SLIP_NS:
                    return  # drift for now rather than slip a frame mid-take
                # slip: lengthen until we line up with the master's *next* frame
                delta = min(round((period - e) / line), max_stretch)
                self.slips += 1
        if delta == 0:
            return
        set_ctrl(fd, V4L2_CID_VBLANK, vb + delta)
        self.pending_restore = vb
        self.corrections += 1
        self.last_correction_ns = delta * line
        self.good = 0 if abs(e) >= LOCK_NS else self.good

    async def _ping_loop(self):
        while True:
            await asyncio.sleep(PING_INTERVAL)
            if not self.master:
                continue
            self.ping_id += 1
            msg = {"t": "ping", "id": self.ping_id, "t1": time.monotonic_ns()}
            try:
                self.sock.sendto(json.dumps(msg).encode(), (self.master["ip"], PORT))
            except OSError:
                pass

    # -- network

    def _on_readable(self):
        while True:
            try:
                data, anc, _, (ip, port) = self.sock.recvmsg(2048, socket.CMSG_SPACE(16))
            except (BlockingIOError, InterruptedError):
                return
            except OSError as e:
                log.warning("sync recv: %s", e)
                return
            rx = None
            for level, kind, cdata in anc:
                if level == socket.SOL_SOCKET and kind == SO_TIMESTAMPNS:
                    sec, nsec = struct.unpack("qq", cdata[:16])
                    rx = sec * 1_000_000_000 + nsec - realtime_minus_mono()
            if rx is None:
                rx = time.monotonic_ns()
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            kind = msg.get("t")
            if kind == "ping":
                reply = {"t": "pong", "id": msg.get("id"), "t1": msg.get("t1"), "t2": rx}
                reply["t3"] = time.monotonic_ns()
                try:
                    self.sock.sendto(json.dumps(reply).encode(), (ip, port))
                except OSError:
                    pass
            elif kind == "pong" and self.role == "follower":
                self.offset.add(msg["t1"], msg["t2"], msg["t3"], rx)
            elif kind == "sync" and msg.get("cam") != self.cam_id:
                if self.role == "master":
                    self.other_masters[msg["cam"]] = time.monotonic()
                    continue
                if self.master and self.master["cam"] != msg["cam"] \
                        and time.monotonic() - self.master["seen"] < PACKET_TIMEOUT:
                    self.other_masters[msg["cam"]] = time.monotonic()
                    continue  # stick with the master we have
                self.master = {**msg, "ip": ip, "seen": time.monotonic()}

    # -- for camd

    def to_local(self, master_ts):
        """A time on the master's clock -> our CLOCK_MONOTONIC."""
        if self.role == "master":
            return master_ts
        if not self.offset.valid(time.monotonic_ns()):
            raise RuntimeError("sync: no clock offset to the master yet")
        return master_ts - self.offset.offset(time.monotonic_ns())

    def schedule(self, lead_ns):
        """Master: the time of the first frame at least lead_ns from now (for record start/stop)."""
        if self.role != "master":
            raise RuntimeError("only the sync master schedules")
        fit = self.clock.fit(10)
        if not fit:
            raise RuntimeError("sync: not enough frames yet")
        _, ts, period = fit
        n = math.ceil((time.monotonic_ns() + lead_ns - ts) / period)
        return int(ts + n * period)

    def status(self):
        now = time.monotonic()
        others = sorted(c for c, t in self.other_masters.items() if now - t < 5)
        rtt, scatter = self.offset.quality()
        return {"role": self.role, "state": self.state, "locked": self.locked,
                "master": self.cam_id if self.role == "master" else (self.master or {}).get("cam"),
                "error_us": round(self.error_ns / 1e3, 1) if self.error_ns is not None else None,
                "offset_rtt_us": round(rtt, 1) if rtt is not None else None,
                "offset_scatter_us": round(scatter, 1) if scatter is not None else None,
                "corrections": self.corrections, "slips": self.slips,
                "no_headroom": self.no_headroom,
                "last_correction_us": round(self.last_correction_ns / 1e3, 1),
                "other_masters": others}
