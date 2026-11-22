#!/bin/bash
# Install camd + the network helper on a camera Pi (run on the Pi, from this folder):
#   sudo bash install.sh          keep this camera's current IP as its static IP
#   sudo bash install.sh --new    no config: the camera comes up as NEW at 192.168.0.5
# Logs: journalctl -u camd -f      Restart: sudo systemctl restart camd
set -e
cd "$(dirname "$0")"
install -d -o cam1 -g cam1 /home/cam1/camd
# Copy only when run from somewhere else (running it from ~/camd itself is fine too)
if [ "$(realpath camd.py)" != "$(realpath /home/cam1/camd/camd.py 2>/dev/null)" ]; then
	install -o cam1 -g cam1 -m 755 camd.py /home/cam1/camd/camd.py
	install -o cam1 -g cam1 -m 644 camsync.py /home/cam1/camd/camsync.py
	install -o cam1 -g cam1 -m 644 hide_nm12.c /home/cam1/camd/hide_nm12.c
fi
# v4l2 shim: single-plane NV12 so the encoder imports ISP buffers without a CPU copy
gcc -O2 -Wall -shared -fPIC -o /home/cam1/camd/hide_nm12.so /home/cam1/camd/hide_nm12.c -ldl
chown cam1:cam1 /home/cam1/camd/hide_nm12.so

# Network helper + boot unit + the narrow sudo rule camd needs
install -m 755 camrig-net /usr/local/sbin/camrig-net
install -m 644 camrig-net.service /etc/systemd/system/camrig-net.service
install -m 440 camrig.sudoers /etc/sudoers.d/camrig.tmp
visudo -cf /etc/sudoers.d/camrig.tmp && mv /etc/sudoers.d/camrig.tmp /etc/sudoers.d/camrig

# Camera config: adopt the current IP unless --new (or a config already exists)
install -d -o cam1 -g cam1 /etc/camrig
if [ ! -f /etc/camrig/camera.json ] && [ "$1" != "--new" ]; then
	ip=$(ip -4 -o route get 192.0.2.1 | grep -o 'src [0-9.]*' | cut -d' ' -f2)
	echo "{\"ip\": \"$ip\"}" > /etc/camrig/camera.json
	echo "adopted current IP $ip as this camera's static IP"
fi
[ -f /etc/camrig/camera.json ] && chown cam1:cam1 /etc/camrig/camera.json

install -m 644 camd.service /etc/systemd/system/camd.service
pkill -f '^python3 camd.py' || true  # a copy started by hand
systemctl daemon-reload
systemctl enable camrig-net camd
systemctl restart camd
systemctl --no-pager status camd | head -5
# Last: switching to the static profile can blip the network (and this ssh session)
echo "applying network config (the connection may drop for a moment)..."
systemctl start camrig-net
