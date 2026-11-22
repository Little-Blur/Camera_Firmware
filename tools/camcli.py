#!/usr/bin/env python3
"""Send one command to a camera daemon and print the reply (no dependencies).

  python camcli.py HOST CMD [key=value ...]
  python camcli.py 192.168.1.36 status
  python camcli.py 192.168.1.36 start mode=uhd60 preview=true
  python camcli.py 192.168.1.36 record_start bitrate_mbps=60
  python camcli.py 192.168.1.36 watch          # print pushed status events
"""
import json
import socket
import sys


def value(v):
    try:
        return json.loads(v)  # numbers, true/false
    except ValueError:
        return v


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    host, cmd = sys.argv[1], sys.argv[2]
    s = socket.create_connection((host, 7070), timeout=60)
    f = s.makefile("rwb")
    if cmd != "watch":
        req = {"id": 1, "cmd": cmd, **{k: value(v) for k, v in (a.split("=", 1) for a in sys.argv[3:])}}
        f.write(json.dumps(req).encode() + b"\n")
        f.flush()
    s.settimeout(None)
    for line in f:
        msg = json.loads(line)
        if cmd == "watch":
            d = msg.get("data", {})
            st, rec = d.get("stream") or {}, d.get("recording")
            print(f"{d.get('state'):9} fps {st.get('fps', 0):6.2f} frames {st.get('frames', 0):6} "
                  f"drop {st.get('dropped', 0):3} temp {d.get('temp_c')} "
                  f"{'REC ' + rec['file'] + ' ' + str(rec['seconds']) + 's drop ' + str(rec['dropped']) if rec else ''}"
                  f"{' ERROR ' + d['error'] if d.get('error') else ''}", flush=True)
        elif msg.get("id") == 1:
            print(json.dumps(msg, indent=1))
            return


if __name__ == "__main__":
    main()
