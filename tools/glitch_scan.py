"""Find unite left-half glitch frames in a recording and test where the bad content came from.

    python glitch_scan.py CLIP.mkv [--max-k 45] [--thresh 6]

Decodes at 1/8 scale (ffmpeg, -fps_mode passthrough, so frame numbers match the file's frames).
Detection: per row band (6 bands per half), a frame is a transient when it differs from both its
neighbours while the neighbours agree with each other:
    t = min(d(N,N-1), d(N,N+1)) - d(N-1,N+1)
Real motion keeps t near 0; a one-frame glitch makes it large. Left-half bands are flagged.

Source test for each flagged frame: the flagged region of frame N is compared with the same region
of frames N-1..N-max_k (and N+1, N+2), plus a vertical-shift search in N itself and the best
earlier frame. A clear minimum at some k > 1 = stale buffer content from frame N-k.
"""
import argparse, json, subprocess, sys
from collections import deque
import numpy as np

BANDS = 6


def probe(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                   "stream=width,height", "-of", "json", path])
    s = json.loads(out)["streams"][0]
    return s["width"], s["height"]


def frames(path, w, h):
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-fps_mode", "passthrough",
           "-vf", f"scale={w}:{h}:flags=area", "-pix_fmt", "yuv420p", "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=1 << 24)
    ysz, csz = w * h, (w // 2) * (h // 2)
    while True:
        b = p.stdout.read(ysz + 2 * csz)
        if len(b) < ysz + 2 * csz:
            break
        a = np.frombuffer(b, np.uint8)
        y = a[:ysz].reshape(h, w)
        u = a[ysz:ysz + csz].reshape(h // 2, w // 2)
        v = a[ysz + csz:].reshape(h // 2, w // 2)
        yield y, u, v
    p.wait()


def band_diff(a, b, h, w):
    """mean |a-b| per (band, half) -> array [BANDS, 2]"""
    d = np.abs(a.astype(np.int16) - b.astype(np.int16))
    bh = h // BANDS
    out = np.empty((BANDS, 2))
    for i in range(BANDS):
        r = d[i * bh:(i + 1) * bh]
        out[i, 0] = r[:, :w // 2].mean()
        out[i, 1] = r[:, w // 2:].mean()
    return out


def region_mae(a, b, mask):
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16))[mask].mean())


def analyse(ring, idx, bands_hit, h, w, max_k):
    """ring: dict frame_index -> (y,u,v). Returns a result dict for frame idx."""
    bh = h // BANDS
    mask = np.zeros((h, w), bool)
    for b in bands_hit:
        mask[b * bh:(b + 1) * bh, :w // 2] = True
    yN, uN, vN = ring[idx]
    # chroma colour of the bad region (magenta = U and V both high, green = both low)
    cm = mask[::2, ::2]
    res = {"frame": idx, "bands": bands_hit,
           "U": round(float(uN[cm].mean()), 1), "V": round(float(vN[cm].mean()), 1),
           "Y": round(float(yN[mask].mean()), 1)}
    # same region in earlier / later frames
    maes = {}
    for k in list(range(1, max_k + 1)) + [-1, -2]:
        j = idx - k
        if j in ring:
            maes[k] = region_mae(yN, ring[j][0], mask)
    res["mae"] = maes
    earlier = {k: m for k, m in maes.items() if k > 0}
    bk = min(earlier, key=earlier.get)
    res["best_k"], res["best_mae"], res["mae_k1"] = bk, round(earlier[bk], 2), round(maes.get(1, -1), 2)
    # control: the right half (assumed clean) compared with N-best_k vs N-1, to see scene motion
    right = np.zeros((h, w), bool)
    right[:, w // 2:] = True
    res["right_mae_k1"] = round(region_mae(yN, ring[idx - 1][0], right), 2)
    res["right_mae_bk"] = round(region_mae(yN, ring[idx - bk][0], right), 2)
    # vertical shift search: bad rows of N against rows shifted by dy in N (same frame) and N-best_k
    rows = np.where(mask[:, 0])[0]
    left = slice(0, w // 2)
    for name, src in (("self", yN), ("bestk", ring[idx - bk][0]), ("prev", ring[idx - 1][0])):
        best = (1e9, 0)
        for dy in range(-h + 1, h):
            r2 = rows + dy
            ok = (r2 >= 0) & (r2 < h)
            if name == "self":
                ok &= ~mask[np.clip(r2, 0, h - 1), 0]  # don't match the bad rows against themselves
            if ok.sum() < len(rows) * 0.5:
                continue
            m = np.abs(yN[rows[ok], left].astype(np.int16) - src[r2[ok], left].astype(np.int16)).mean()
            if m < best[0]:
                best = (m, dy)
        res[f"shift_{name}"] = {"dy_full_res": best[1] * 8, "mae": round(float(best[0]), 2)}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip")
    ap.add_argument("--max-k", type=int, default=45)
    ap.add_argument("--thresh", type=float, default=6.0, help="transient score per band (Y levels)")
    ap.add_argument("--json", help="write results here")
    a = ap.parse_args()
    W, H = probe(a.clip)
    w, h = (W // 8) & ~1, (H // 8) & ~1
    print(f"{a.clip}: {W}x{H} -> analysing at {w}x{h}", file=sys.stderr)
    keep = a.max_k + 4
    ring, order = {}, deque()
    pending = []  # (idx, bands) waiting for N+2
    results, n = [], 0
    for i, f in enumerate(frames(a.clip, w, h)):
        ring[i] = f
        order.append(i)
        while len(order) > keep:
            del ring[order.popleft()]
        n = i + 1
        # transient test for frame i-1 (needs i-2, i-1, i)
        c = i - 1
        if c >= 1:
            d_prev = band_diff(ring[c][0], ring[c - 1][0], h, w)
            d_next = band_diff(ring[c][0], ring[c + 1][0], h, w)
            d_nb = band_diff(ring[c - 1][0], ring[c + 1][0], h, w)
            t = np.minimum(d_prev, d_next) - d_nb
            hit = [b for b in range(BANDS) if t[b, 0] > a.thresh and t[b, 0] > 3 * max(t[b, 1], 0.5)]
            if hit:
                pending.append((c, hit, round(float(t[:, 0].max()), 1)))
        for p in [p for p in pending if p[0] + 2 <= i]:
            pending.remove(p)
            if p[0] - a.max_k in ring or p[0] > a.max_k:
                r = analyse(ring, p[0], p[1], h, w, a.max_k)
                r["score"] = p[2]
                results.append(r)
                mk = r["mae"]
                print(f"frame {r['frame']:6d} bands {r['bands']} score {r['score']:5.1f} "
                      f"YUV {r['Y']:.0f}/{r['U']:.0f}/{r['V']:.0f} | region vs N-1 {r['mae_k1']:5.2f}, "
                      f"best N-{r['best_k']} {r['best_mae']:5.2f}, N+1 {mk.get(-1, -1):5.2f} | "
                      f"right half N-1 {r['right_mae_k1']:.2f} N-{r['best_k']} {r['right_mae_bk']:.2f} | "
                      f"shift self {r['shift_self']} bestk {r['shift_bestk']}", flush=True)
    print(f"{n} frames, {len(results)} flagged", file=sys.stderr)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"clip": a.clip, "frames": n, "size": [W, H], "results": results}, fh, indent=1)


if __name__ == "__main__":
    main()
