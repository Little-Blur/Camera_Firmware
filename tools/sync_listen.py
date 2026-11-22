#!/usr/bin/env python3
"""Print the frame-sync master's packets (run on any machine on the rig network).

  python sync_listen.py [seconds]
"""
import json
import socket
import struct
import sys
import time

GROUP, PORT = "239.255.70.71", 7071
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("", PORT))
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
    probe.connect(("192.168.1.1", 9))
    me = probe.getsockname()[0]
s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(GROUP) + socket.inet_aton(me))
s.settimeout(1)
end = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 5)
last = None
while time.time() < end:
    try:
        data, (ip, _) = s.recvfrom(2048)
    except socket.timeout:
        continue
    m = json.loads(data)
    if m.get("t") != "sync":
        continue
    step = ""
    if last:  # master frames between packets, and how far ts is from k * period
        dk = m["k"] - last["k"]
        step = f"dk {dk}  ts - (last + dk*period) {(m['ts'] - last['ts'] - dk * m['period']) / 1e3:+7.1f} us"
    print(f"{ip} {m['cam']} {m['mode']} k {m['k']:6} period {m['period'] / 1e3:.3f} us  {step}")
    last = m
