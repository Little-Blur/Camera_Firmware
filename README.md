# camrig: camera management (v0.3)

```
daemon/camd.py             runs on each camera Pi: owns rkaiq + the GStreamer pipeline
daemon/camsync.py          frame sync between cameras (master / follower), used by camd
daemon/camd.service        systemd unit (runs as cam1, preloads hide_nm12.so)
daemon/camrig-net          root helper: /etc/camrig/camera.json -> static IP (NetworkManager)
daemon/camrig-net.service  runs camrig-net at boot
daemon/camrig.sudoers      lets cam1 run camrig-net, reboot and poweroff (nothing else)
daemon/hide_nm12.c         v4l2 shim: single-plane NV12, so the encoder imports ISP buffers (zero-copy)
daemon/install.sh          sudo bash install.sh [--new]   (on the Pi, from ~/camd)
client/camctl.py           desktop app (PySide6): python client/camctl.py
client/cameras.json        camera IPs the client connects to
tools/camcli.py            one-shot commands from a terminal, no dependencies
tools/sync_listen.py       print the sync master's packets (any machine on the network)
tools/sync_probe.py        hardware check: frame timestamp jitter + VBLANK stretch (on a Pi, camd stopped)
```

## Camera identity
Every camera runs the same image. `/etc/camrig/camera.json` says who it is:
`{"ip": "192.168.1.150"}` → static 192.168.1.150, camera ID **cam150**
(optional keys: `prefix` 23, `gateway` / `dns` 192.168.1.1).

- No file: the camera comes up at **192.168.0.5** and the client lists it as **NEW**. NEW cameras
  are left out of every rig-wide action. Give it a number (100–200) on its Device tab: the camera
  writes the file, switches address at once (camrig-net) and keeps it after reboots.
- Configure **one new camera at a time**: two unconfigured cameras would share 192.168.0.5.
- The network is 192.168.0.0/23. Keep the router's DHCP pool away from .0.5 and .1.100–200.
- `install.sh` adopts the camera's current IP as its static IP; `install.sh --new` leaves it
  unconfigured (use that when preparing the image for new cameras).

## Client
- **(RIG)** at the top of the list: one virtual camera for the whole rig.
  - Live: mode, preset, bitrate and preview go to every configured camera. Each camera has a green
    (in sync) or red (differs / offline / error) indicator. *Push settings to all* fixes the
    red ones. There's a 2×2 preview grid with a camera dropdown per tile. *Record scene* starts every
    active camera with the same take name (`scene_<date>_<time>_<mode>`).
  - Videos: scenes = recording names across all cameras (shows "3 / 4 cameras"). Download goes to
    `<folder>/<scene>/<camID>.mkv`; files already there (same size) are skipped, so downloading
    again fills in the holes. Delete removes the scene from every camera.
  - Device: reboot all / shut down all.
- **Prepare** (top tab): the scene folders on this PC (the RIG → Videos download folder).
  *Extract frames* turns `<scene>/<camera>.mkv` into `<scene>/frames/frame_000001/<camera>.jpg` …:
  one folder per moment with every camera's view, for COLMAP. Needs ffmpeg on PATH. Frames are
  numbered by timestamp, so a dropped frame leaves one folder without that camera instead of
  shifting the rest. Meaningful for frame-synced takes.
- Each camera has **Live / Videos / Device** tabs. Videos lists the files on that camera with
  per-file download and delete. Device shows the identity, the camera number and reboot / shut down.

## Frame sync
RIG → Live → **Frame sync**: tick *Sync frames to a master camera* and pick the master
(*Auto* = the first camera). The master multicasts where its frames are (UDP 7071, group
239.255.70.71); each follower measures the master's clock with ping round trips and stretches or
shortens single frames (sensor VBLANK) until its frames start together with the master's.
*Record scene* then asks the master for a frame ~0.6 s ahead and every camera starts on that frame,
and stops after the same frame, so all files of a scene have the same frames.
- Same mode on every camera. The Frame sync column shows *locked ±N µs* per follower.
- uhd60 runs the sensor at the driver's minimum frame length, so followers can only lengthen
  frames there (a slower follower slips one frame to catch up; while recording it drifts instead).
  full45 can correct both ways. Fix pending: allow the UHD mode down to 61 fps in imx477.c.

## Ports
- **7070 control**: newline-delimited JSON. Request `{"id": 1, "cmd": "start", "mode": "uhd60"}`
  → reply `{"id": 1, "ok": true, "result": {...}}`. The daemon pushes `{"event": "status", "data": {...}}`
  every second while active (5 s idle) and on every change.
- **8080 HTTP**: `/preview.jpg`, `/preview.mjpg` (open it in a browser), `/recordings/<name>`, `/` (status JSON).

## Commands
| cmd | args | notes |
|---|---|---|
| `hello` | | camera_id (`NEW` if unconfigured), configured, ip, configured_ip, hostname, mac, serial, version |
| `status` | | state idle/starting/active/stopping/error, fps, drops, recording, temps, disk |
| `get_config` | | modes, IQ presets, saved settings |
| `start` | `mode`, `preset`, `preview`, `shutter_us`, `tnr` (all optional) | ~6 s: rkaiq, mainpath, frame-rate check |
| `stop` | | finishes a recording first |
| `set_mode` / `set_preset` | `mode` / `preset` | restarts the chain if active (rkaiq reads IQ only at start); refused while recording |
| `set_preview` | `enabled` | live, no restart |
| `set_tnr` | `tnr` = `preset` / `low` / `off` | ISP temporal noise reduction (bayertnr): `low` halves its strength at every ISO, `off` disables it. At high gain it makes moving things see-through (ghosting). Restarts the chain if active; refused while recording |
| `set_shutter` | `us` (0 = auto) | fixed exposure time in µs; gain stays automatic. Restarts the chain if active (written into the IQ file); refused while recording. Capped at one frame (uhd60 16166 µs, full45 21555 µs). Status `exposure.us` / `exposure.gain` = what the sensor actually runs |
| `set_bitrate` | `mbps` | used by the next recording |
| `set_rec_buffer` | `buffer` = `direct` / `ram` | `ram`: record into RAM instead of letting the page cache flush to the NVMe in bursts (bursts make the left ISP core late/freeze); every 1.5 GB the take is moved to the NVMe in one steady paced run and freed from RAM while recording continues, the rest after the take. Length limited by NVMe space. Status `recording.ram_mb` / `moves` / `seconds_left`, `ram.saving`; `list_recordings` marks unsaved files `in_ram` (not downloadable until saved) |
| `record_start` | `bitrate_mbps`, `take`, `at_ts` (optional) | H.265 MKV in `~/rec`; `take` = shared file name for a rig scene; `at_ts` = first frame, on the sync master's clock |
| `record_stop` | `at_ts` (optional) | last frame at `at_ts`; returns file, frames, dropped |
| `set_sync` | `role` off/master/follower | live, refused while recording |
| `sync_time` | `lead_ms` (default 600) | master only: time of a frame that far ahead, for `at_ts` |
| `list_recordings` / `delete_recording` | `name` | |
| `set_network` | `ip` | 192.168.0.0/23, not .0.5; saves the config and switches address |
| `reboot` / `shutdown` | | finishes a recording first |

## Examples
    python tools/camcli.py 192.168.1.145 start mode=full45 preset=shop
    python tools/camcli.py 192.168.1.145 watch
    ffplay http://192.168.1.145:8080/preview.mjpg
