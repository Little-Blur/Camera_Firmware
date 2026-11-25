"""COLMAP dense point clouds for a camctl scene's SfM frames (initial points for tools/trigs_prep.py).

    python tools/colmap_dense.py <scene> --frames 850,851,855,856,860 [--max-size 1600] [--colmap PATH]
    python tools/colmap_dense.py <scene> --first 850 --last 860 --keyframe-step 5

Each frame f (point/colmap_<f-1>, already undistorted) gets a workspace point/colmap_<f-1>/dense/
(images linked, sparse/ copied) and patch_match_stereo + stereo_fusion write dense/fused.ply.
With --keyframe-step, the keyframes and the frames either side (for velocity and acceleration).
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

COLMAP_DEFAULT = r"C:\Users\kalib\Documents\colmap-4.2.1\bin\colmap.exe"


def run(colmap, *args, log):
    cmd = [colmap, *args]
    log.write(f"==== {' '.join(cmd)}\n")
    log.flush()
    r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    if r.returncode:
        sys.exit(f"colmap {args[0]} failed ({r.returncode}): see {log.name}")


def densify(colmap, sfm_dir, max_size, min_pixels):
    ws = sfm_dir / "dense"
    if (ws / "fused.ply").exists():
        return "exists"
    (ws / "images").mkdir(parents=True, exist_ok=True)
    (ws / "sparse").mkdir(exist_ok=True)
    for sub in ("depth_maps", "normal_maps", "consistency_graphs"):
        (ws / "stereo" / sub).mkdir(parents=True, exist_ok=True)
    images = sorted(p.name for p in (sfm_dir / "images").glob("*.jpg"))
    for name in images:
        dst = ws / "images" / name
        if not dst.exists():
            try:
                os.link(sfm_dir / "images" / name, dst)
            except OSError:
                shutil.copy2(sfm_dir / "images" / name, dst)
    for f in (sfm_dir / "sparse" / "0").glob("*.bin"):
        shutil.copy2(f, ws / "sparse" / f.name)
    # Every other camera is a source view (a small rig: let COLMAP pick among all of them).
    cfg = "".join(f"{name}\n__all__\n" for name in images)
    (ws / "stereo" / "patch-match.cfg").write_text(cfg)
    (ws / "stereo" / "fusion.cfg").write_text("".join(f"{name}\n" for name in images))
    with open(ws / "dense.log", "w", encoding="utf-8") as log:
        run(colmap, "patch_match_stereo", "--workspace_path", str(ws), "--workspace_format", "COLMAP",
            "--PatchMatchStereo.max_image_size", str(max_size), "--PatchMatchStereo.geom_consistency", "1",
            log=log)
        run(colmap, "stereo_fusion", "--workspace_path", str(ws), "--workspace_format", "COLMAP",
            "--input_type", "geometric", "--output_path", str(ws / "fused.ply"),
            "--StereoFusion.min_num_pixels", str(min_pixels), "--StereoFusion.max_image_size", str(max_size),
            log=log)
    shutil.rmtree(ws / "images")    # links only; keep sparse/, stereo/ (depth maps) and fused.ply
    return "done"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scene", type=Path)
    ap.add_argument("--frames", default="", help="comma-separated frame numbers")
    ap.add_argument("--first", type=int, default=0)
    ap.add_argument("--last", type=int, default=0)
    ap.add_argument("--keyframe-step", type=int, default=0)
    ap.add_argument("--max-size", type=int, default=1600, help="longest image side for stereo")
    ap.add_argument("--min-pixels", type=int, default=3,
                    help="views a fused point needs (7 cameras: COLMAP's default 5 is strict)")
    ap.add_argument("--colmap", default=COLMAP_DEFAULT)
    a = ap.parse_args()

    frames = {int(x) for x in a.frames.split(",") if x.strip()}
    if a.keyframe_step:
        for k in range(a.first, a.last + 1, a.keyframe_step):
            frames |= {max(k - 1, a.first), k, min(k + 1, a.last)}
    elif a.first and a.last:
        frames |= set(range(a.first, a.last + 1))
    if not frames:
        sys.exit("no frames: pass --frames, or --first/--last [--keyframe-step]")
    for f in sorted(frames):
        sfm_dir = a.scene / "point" / f"colmap_{f - 1}"
        if not (sfm_dir / "sparse" / "0" / "points3D.bin").exists():
            sys.exit(f"{sfm_dir}: no SfM frame")
        print(f"frame {f}: {densify(a.colmap, sfm_dir, a.max_size, a.min_pixels)}", flush=True)


if __name__ == "__main__":
    main()
