#!/bin/bash
# Switch the ISP between dual-core (unite) and single-core mode, then reboot (run on the Pi):
#   sudo bash isp_mode.sh single | unite
# Also installs/runs camrig-net from this folder first: it repairs the boot SD's partition
# table if needed, so /boot (with armbianEnv.txt) is mounted.
set -e
cd "$(dirname "$0")"
case "$1" in
	single) OVL=imx477-4lane ;;
	unite) OVL=imx477-4lane-unite ;;
	*) echo "usage: sudo bash isp_mode.sh single|unite"; exit 1 ;;
esac
install -m 755 camrig-net /usr/local/sbin/camrig-net
/usr/local/sbin/camrig-net
E=/boot/armbianEnv.txt
[ -f "$E" ] || { echo "$E not found: /boot is not mounted"; exit 1; }
[ -f /boot/overlay-user/$OVL.dtbo ] || { echo "/boot/overlay-user/$OVL.dtbo missing"; exit 1; }
cp "$E" "$E.bak-$(date +%Y%m%d-%H%M%S)"
sed -i "s/^user_overlays=.*/user_overlays=$OVL/" "$E"
grep -E "^(user_overlays|extraargs)=" "$E"
echo "rebooting into $1 ISP mode..."
systemctl reboot
