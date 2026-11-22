"""Detect a frozen half (a core that stopped writing): each buffer then keeps its old content, so
the half repeats exactly every POOL frames while live sensor noise makes neighbours differ.

    python frozen_scan.py CLIP.mkv [--pool 16] [--every 30]

Per sampled frame: mean |Y(N) - Y(N-1)| and |Y(N) - Y(N-POOL)| for the left and right halves at
1/8 scale. Live: both > ~0.5. Frozen half: N-POOL ~ 0 (identical buffer) while the other half moves.
--pool 0: pool size unknown (older recordings: 4..10 buffers); uses the smallest |Y(N) - Y(N-k)|
over k = 2..24 instead. Frozen: that minimum is near the encoder-noise floor (~0.2) for one half
and the other half is clearly higher.
"""
import argparse, subprocess, sys, json
from collections import deque
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("clip")
ap.add_argument("--pool", type=int, default=16, help="0 = unknown, search k=2..24")
ap.add_argument("--every", type=int, default=30)
a = ap.parse_args()
s = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                         "stream=width,height", "-of", "json", a.clip]))["streams"][0]
w, h = (s["width"] // 8) & ~1, (s["height"] // 8) & ~1
p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", a.clip, "-fps_mode", "passthrough", "-vf",
                      f"scale={w}:{h}:flags=area", "-pix_fmt", "gray", "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
K = 24 if a.pool == 0 else a.pool
ring = deque(maxlen=K + 1); i = 0
print("frame   t(s)   left: N-1  N-pool   right: N-1  N-pool   state")
while True:
    b = p.stdout.read(w * h)
    if len(b) < w * h: break
    ring.append(np.frombuffer(b, np.uint8).reshape(h, w).astype(np.int16))
    if len(ring) == K + 1 and i % a.every == 0:
        n, n1 = ring[-1], ring[-2]
        L1 = np.abs(n - n1)[:, :w // 2].mean(); R1 = np.abs(n - n1)[:, w // 2:].mean()
        if a.pool:
            LP = np.abs(n - ring[0])[:, :w // 2].mean(); RP = np.abs(n - ring[0])[:, w // 2:].mean()
        else:
            LP = min(np.abs(n - ring[-1 - k])[:, :w // 2].mean() for k in range(2, K + 1))
            RP = min(np.abs(n - ring[-1 - k])[:, w // 2:].mean() for k in range(2, K + 1))
        st = "LEFT FROZEN" if LP < 0.25 and RP > 2 * LP + 0.2 else (
             "right frozen" if RP < 0.25 and LP > 2 * RP + 0.2 else "live")
        print(f"{i:6d} {i/60:6.1f}   {L1:9.2f} {LP:7.2f}   {R1:10.2f} {RP:7.2f}   {st}", flush=True)
    i += 1
