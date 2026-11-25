"""Turn a camctl scene (SfM frames, optionally Brush splats) into a TRiGS COLMAP-mode dataset.

    External/open4dgs-trigs/.venv/Scripts/python tools/trigs_prep.py <scene> [--first N] [--last N]

(any Python with numpy and scipy; the TRiGS environment has both.)

Reads (camctl's layout):
  <scene>/point/colmap_<n>/images/<camera>.jpg + sparse/0   undistorted SfM frame n+1; every
                                                            frame shares the reference poses
  <scene>/point/colmap_<n>/dense/fused.ply                  optional: COLMAP dense cloud of frame n+1
                                                            (patch_match_stereo + stereo_fusion)
  <scene>/splats/brush/frame_<f>.ply                        optional: Brush splats of frame f

Writes <scene>/trigs/:
  data/sparse/0/*.bin             the first frame's model (poses + pinhole intrinsics)
  data/images/<camera>/<f>.jpg    hard links to the SfM frames' undistorted images
  keyframes.npz                   initial points of every keyframe, with per-point velocity

TRiGS never adds splats (it only relocates them), so the initial points set the splat budget. Any
point source works (Open4DGS uses RoMa matches). Per frame, --points auto takes the first that
exists: the Brush PLY (colours from the SH DC term), COLMAP's dense fused.ply, then COLMAP's
sparse points3D.bin (a few thousand points: enough to run, too few for detail).
Motion comes from the neighbouring frames, so a keyframe needs frames f-1 and f+1 too: centred
velocity (f+1 - f-1)/2 and acceleration f+1 - 2f + f-1 per video frame (the trainer fits its starting
curves to both), one-sided at the ends. A point is followed by index when both frames are Brush PLYs
with the same count (frozen warm frames keep splat i the same Gaussian), else by nearest neighbour
within --max-move of the scene's size.
"""

import argparse
import json
import os
import shutil
import struct
import sys
from pathlib import Path

import numpy as np

SH_C0 = 0.28209479177387814


def colmap_points(points3d_bin):
    """xyz [N,3] float32, rgb [N,3] in 0..1 from a COLMAP points3D.bin."""
    with open(points3d_bin, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        xyz = np.empty((n, 3), np.float32)
        rgb = np.empty((n, 3), np.float32)
        for i in range(n):
            _, x, y, z, r, g, b, _ = struct.unpack("<Q3d3Bd", f.read(43))
            xyz[i] = x, y, z
            rgb[i] = r, g, b
            track = struct.unpack("<Q", f.read(8))[0]
            f.seek(8 * track, 1)
    return xyz, rgb / 255.0


PLY_TYPES = {"float": "<f4", "float32": "<f4", "double": "<f8", "uchar": "u1", "uint8": "u1",
             "int": "<i4", "int32": "<i4", "uint": "<u4", "short": "<i2", "ushort": "<u2"}


def ply_vertices(path):
    """Vertex array of a binary little-endian PLY (Brush's export, COLMAP's fused.ply)."""
    with open(path, "rb") as f:
        header = b""
        while not header.rstrip(b"\r\n").endswith(b"end_header"):   # COLMAP on Windows: CRLF
            line = f.readline()
            if not line:
                raise ValueError(f"{path}: no end_header")
            header += line
        lines = header.decode("ascii").splitlines()
        if "format binary_little_endian 1.0" not in lines:
            raise ValueError(f"{path}: not a binary little-endian PLY")
        count = next(int(l.split()[2]) for l in lines if l.startswith("element vertex"))
        props = [l.split()[1:3] for l in lines if l.startswith("property ")]
        return np.fromfile(f, dtype=[(name, PLY_TYPES[t]) for t, name in props], count=count)


def brush_points(path, min_opacity):
    """xyz, rgb (0..1) and a keep mask of the splats at least min_opacity opaque."""
    v = ply_vertices(path)
    xyz = np.stack([v["x"], v["y"], v["z"]], 1)
    rgb = np.clip(0.5 + SH_C0 * np.stack([v["f_dc_0"], v["f_dc_1"], v["f_dc_2"]], 1), 0, 1)
    keep = 1 / (1 + np.exp(-v["opacity"])) >= min_opacity
    return xyz, rgb.astype(np.float32), keep


def dense_points(path):
    """xyz, rgb (0..1) of a COLMAP stereo_fusion PLY."""
    v = ply_vertices(path)
    xyz = np.stack([v["x"], v["y"], v["z"]], 1).astype(np.float32)
    rgb = np.stack([v["red"], v["green"], v["blue"]], 1).astype(np.float32) / 255.0
    return xyz, rgb


def link_or_copy(src, dst):
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scene", type=Path)
    ap.add_argument("--first", type=int, default=0, help="first frame (0 = first SfM frame)")
    ap.add_argument("--last", type=int, default=0, help="last frame (0 = last SfM frame)")
    ap.add_argument("--keyframe-step", type=int, default=5)
    ap.add_argument("--fps", type=float, default=0, help="0 = frames/extract.json, else ffprobe")
    ap.add_argument("--points", choices=["auto", "brush", "dense", "sparse"], default="auto",
                    help="auto: Brush PLY, else COLMAP dense, else COLMAP sparse; the others: only that one")
    ap.add_argument("--min-opacity", type=float, default=0.05, help="Brush splats fainter than this are skipped")
    ap.add_argument("--max-move", type=float, default=0.005,
                    help="nearest-neighbour velocity: farthest a point may move in one frame, as a fraction "
                         "of the points' extent (5-95%% bounding-box diagonal)")
    ap.add_argument("--out", type=Path, default=None, help="default <scene>/trigs")
    a = ap.parse_args()

    scene = a.scene.resolve()
    sfm = {int(d.name[7:]) + 1: d for d in (scene / "point").glob("colmap_*")
           if (d / "sparse" / "0" / "points3D.bin").exists()}
    if not sfm:
        sys.exit(f"{scene}: no SfM frames (point/colmap_<n>/sparse/0)")
    first = a.first or min(sfm)
    last = a.last or max(sfm)
    frames = [f for f in range(first, last + 1) if f in sfm]
    missing = [f for f in range(first, last + 1) if f not in sfm]
    if missing:
        sys.exit(f"SfM frames missing in {first}..{last}: {missing[:10]}{' …' if len(missing) > 10 else ''}")
    fps = a.fps or scene_fps(scene)
    out = (a.out or scene / "trigs").resolve()
    data = out / "data"

    # Poses and intrinsics: every SfM frame was triangulated with the reference frame's cameras.
    ref = sfm[first] / "sparse" / "0"
    (data / "sparse" / "0").mkdir(parents=True, exist_ok=True)
    for f in ref.glob("*.bin"):
        shutil.copy2(f, data / "sparse" / "0" / f.name)
    cams = sorted(p.stem for p in (sfm[first] / "images").glob("*.jpg"))
    for cam in cams:
        (data / "images" / cam).mkdir(parents=True, exist_ok=True)
    n_img = 0
    for f in frames:
        for cam in cams:
            src = sfm[f] / "images" / f"{cam}.jpg"
            if not src.exists():
                sys.exit(f"{src} missing: every camera needs every frame")
            link_or_copy(src, data / "images" / cam / f"{f:06d}.jpg")
            n_img += 1
    print(f"images: {len(cams)} cameras x {len(frames)} frames ({n_img} links) -> {data / 'images'}")

    brush_dir = scene / "splats" / "brush"

    def points(f):
        ply = brush_dir / f"frame_{f:06d}.ply"
        fused = sfm[f] / "dense" / "fused.ply"
        if a.points in ("auto", "brush") and ply.exists():
            xyz, rgb, keep = brush_points(ply, a.min_opacity)
            return xyz, rgb, keep, "brush"
        if a.points in ("auto", "dense") and fused.exists():
            xyz, rgb = dense_points(fused)
            return xyz, rgb, np.ones(len(xyz), bool), "dense"
        if a.points in ("auto", "sparse"):
            xyz, rgb = colmap_points(sfm[f] / "sparse" / "0" / "points3D.bin")
            return xyz, rgb, np.ones(len(xyz), bool), "sparse"
        sys.exit(f"frame {f}: no {a.points} points ({ply if a.points == 'brush' else fused})")

    from scipy.spatial import cKDTree

    def offsets(k, xyz, keep, src, f):
        """Per point of frame k: its position in frame f minus its own, and whether it was found."""
        oxyz, _, okeep, osrc = points(f)
        if src == osrc == "brush" and len(oxyz) == len(xyz):
            return oxyz - xyz, okeep.copy(), "index"
        lo, hi = np.percentile(xyz[keep], [5, 95], axis=0)
        reach = a.max_move * float(np.linalg.norm(hi - lo))
        d, j = cKDTree(oxyz[okeep]).query(xyz, k=1, distance_upper_bound=reach)
        found = np.isfinite(d)
        move = np.zeros_like(xyz)
        move[found] = oxyz[okeep][j[found]] - xyz[found]
        return move, found, "nearest"

    keyframes = list(range(first, last + 1, a.keyframe_step))
    names = ("positions", "velocities", "accelerations", "colors", "has_velocity", "has_acceleration",
             "source_frames")
    rows = {k: [] for k in names}
    for k in keyframes:
        xyz, rgb, keep, src = points(k)
        zero, none = np.zeros_like(xyz), np.zeros(len(xyz), bool)
        fwd, has_f, how = offsets(k, xyz, keep, src, k + 1) if k < last else (zero, none, "")
        back, has_b, how_b = offsets(k, xyz, keep, src, k - 1) if k > first else (zero, none, "")
        # Centred where both neighbours are known (and the acceleration with it), one-sided otherwise.
        both = has_f & has_b
        vel = np.where(both[:, None], 0.5 * (fwd - back), np.where(has_f[:, None], fwd, -back))
        acc = np.where(both[:, None], fwd + back, 0.0)
        has = has_f | has_b
        rows["positions"].append(xyz[keep])
        rows["velocities"].append(vel[keep])
        rows["accelerations"].append(acc[keep])
        rows["colors"].append(rgb[keep])
        rows["has_velocity"].append(has[keep])
        rows["has_acceleration"].append(both[keep])
        rows["source_frames"].append(np.full(int(keep.sum()), k, np.int64))
        speed = np.linalg.norm(vel[keep][has[keep]], axis=1)
        print(f"keyframe {k}: {int(keep.sum()):,} {src} points, motion by {how or how_b or 'nothing'}: "
              f"{int(has[keep].sum()):,} with velocity ({int(both[keep].sum()):,} centred), "
              f"median {np.median(speed) if speed.size else 0:.5f}/frame")

    cat = {k: np.concatenate(v) for k, v in rows.items()}
    n_frames = last - first + 1
    times = ((cat["source_frames"] - first) / n_frames).astype(np.float32).reshape(-1, 1)
    npz = out / "keyframes.npz"
    np.savez_compressed(
        npz,
        positions=cat["positions"].astype(np.float32),
        velocities=cat["velocities"].astype(np.float32),
        accelerations=cat["accelerations"].astype(np.float32),
        has_acceleration=cat["has_acceleration"],
        colors=cat["colors"].astype(np.float32),
        times=times,
        has_velocity=cat["has_velocity"],
        source_frames=cat["source_frames"],
        frame_start=first, frame_end=last,
        source_frame_start=first, source_frame_end=last,
        keyframe_step=a.keyframe_step, n_keyframes=len(keyframes), fps=fps,
        velocity_units="world_per_video_frame",
        mode="keyframes_with_velocity",
    )
    print(f"{len(cat['positions']):,} initial points in {len(keyframes)} keyframes -> {npz}")
    print("\ntrain (from External/open4dgs-trigs):")
    print(f'  .venv\\Scripts\\trigs-train-colmap --data-dir "{data}" --npz-path "{npz}" '
          f'--output-dir "{out / "run"}" --data-factor 4 --config.fps {fps:g} --config.n-steps 30000 '
          f'--config.target-count-per-keyframe 0 --config.init-sigma-t-mode speed')
    print("  (target-count-per-keyframe 0 keeps every initial point; speed: lifetimes from the velocities,"
          " as this NPZ has no per-point durations)")


def scene_fps(scene):
    """camctl's frames/extract.json, else the first video's frame rate (ffprobe)."""
    try:
        num, den = json.loads((scene / "frames" / "extract.json").read_text())["source_fps"].split("/")
        return int(num) / int(den)
    except (OSError, ValueError, KeyError):
        pass
    import subprocess
    video = next(scene.glob("*.mkv"), None)
    if video is None:
        sys.exit("no frame rate: pass --fps")
    rate = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=r_frame_rate", "-of", "csv=p=0", str(video)],
                          capture_output=True, text=True).stdout.strip()
    num, _, den = rate.partition("/")
    return float(num) / float(den or 1)


if __name__ == "__main__":
    main()
