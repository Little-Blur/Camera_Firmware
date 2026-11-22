"""Parse a video_rkisp debug=3 kernel log (dmesg -x) into one record per ISP frame (unite mode).

    python kmsg_frames.py kmsg.log [--from T0] [--to T1] [--csv out.csv]

Per frame (keyed by readback sequence): raw buffer address, readback time, left/right ISP and MI
frame-end times, whether the left MI status was set when the right MI interrupt came, the
'wait isp end timeout', stats read failures, and the output addresses programmed into each core.
"""
import argparse, csv, re, sys
from collections import Counter

R = re.compile(r"\[\s*([\d.]+)\] (\S+) ?(\S*): (.*)$")


def parse(path, t0=0.0, t1=1e18):
    frames, cur = [], None
    left_mi_seen = False
    pending_err = []
    for line in open(path, errors="replace"):
        m = R.search(line)
        if not m:
            continue
        t = float(m.group(1))
        if t < t0 or t > t1:
            continue
        msg = m.group(4)
        if msg.startswith("rkisp_rx_qbuf"):
            seq = int(re.search(r"seq:(\d+)", msg).group(1))
            dma = re.search(r"dma:(0x[0-9a-f]+)", msg).group(1)
            cur = {"seq": seq, "raw": dma, "t_qbuf": t, "timeout": 0, "stats_fail": 0,
                   "left_mi": None, "left_isp_end": None, "right_isp_end": None, "right_mi": None,
                   "Y_L": None, "SHD_L": None, "Y_R": None, "SHD_R": None, "done": None, "t_done": None,
                   "left_isp_vals": [], "t_readback": None}
            frames.append(cur)
            left_mi_seen = False
            continue
        if cur is None:
            continue
        if msg.startswith("readback frame:"):
            cur["t_readback"] = t
        elif msg.startswith("left isp isr:"):
            v = int(msg.split(":")[1], 16)
            cur["left_isp_vals"].append(hex(v))
            if v & 0x2:
                cur["left_isp_end"] = t
        elif msg.startswith("isp isr:"):
            v = int(msg.split(":")[1].split(",")[0], 16)
            if v & 0x2:
                cur["right_isp_end"] = t
        elif msg.startswith("wait isp end timeout"):
            cur["timeout"] += 1
        elif msg.startswith("left mi isr:"):
            cur["left_mi"] = msg.split(":")[1]
            cur["t_left_mi"] = t
        elif msg.startswith("mi isr:"):
            cur["right_mi"] = t
        elif msg.startswith("update_mi"):
            y = re.search(r"Y:(0x[0-9a-f]+)", msg).group(1)
            s = re.search(r"Y_SHD:(0x[0-9a-f]+)", msg).group(1)
            if msg.endswith("right"):
                cur["Y_R"], cur["SHD_R"] = y, s
            else:
                cur["Y_L"], cur["SHD_L"] = y, s
        elif "meas_ddr fail" in msg:
            cur["stats_fail"] += 1
        elif re.match(r"stream:0 seq:\d+ buf done", msg):
            cur["done"] = re.search(r"buf done:(0x[0-9a-f]+)", msg).group(1)
            cur["t_done"] = t
    return frames


def flags(f):
    out = []
    if f["timeout"]:
        out.append("TIMEOUT")
    if f["right_mi"] and f["left_mi"] is None:
        out.append("LEFT_MI_NOT_SET")
    if f["SHD_L"] and f["SHD_R"] and int(f["SHD_R"], 16) - int(f["SHD_L"], 16) != 0x780:
        out.append("SHD_MISMATCH")
    if f["Y_L"] and f["Y_R"] and int(f["Y_R"], 16) - int(f["Y_L"], 16) != 0x780:
        out.append("Y_MISMATCH")
    if f["done"] and f["SHD_L"] and f["done"] != hex(int(f["SHD_L"], 16) & ~0xfff):
        out.append("DONE_NE_SHD")
    if f["stats_fail"]:
        out.append("STATS_FAIL")
    if f["right_mi"] is None:
        out.append("NO_MI")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--from", dest="t0", type=float, default=0)
    ap.add_argument("--to", dest="t1", type=float, default=1e18)
    ap.add_argument("--csv")
    a = ap.parse_args()
    fr = parse(a.log, a.t0, a.t1)
    c = Counter()
    for f in fr:
        for x in flags(f):
            c[x] += 1
    print(f"{len(fr)} frames, seq {fr[0]['seq']}..{fr[-1]['seq']}")
    for k, v in c.most_common():
        print(f"  {k:16s} {v:6d}  ({100 * v / len(fr):.2f}%)")
    if a.csv:
        keys = ["seq", "raw", "t_qbuf", "t_readback", "left_isp_end", "right_isp_end", "right_mi", "left_mi",
                "Y_L", "SHD_L", "Y_R", "SHD_R", "done", "t_done", "timeout", "stats_fail"]
        with open(a.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(keys + ["flags", "left_isp_vals"])
            for f in fr:
                w.writerow([f.get(k) for k in keys] + ["|".join(flags(f)), " ".join(f["left_isp_vals"])])


if __name__ == "__main__":
    main()
