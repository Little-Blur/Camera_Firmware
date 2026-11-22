#!/usr/bin/env python3
"""Read / set RK3588 NoC QoS registers for the camera path (run on the Pi as root).

    sudo python3 qos.py                 # show priority/mode/bandwidth/saturation/extcontrol
    sudo python3 qos.py isp 0x303       # set PRIORITY of isp0_mro/mwo (left unite core)

Addresses from rk3588s.dtsi (qos_*: qos@...). Register offsets from drivers/soc/rockchip/
pm_domains.c: PRIORITY 0x08, MODE 0x0c, BANDWIDTH 0x10, SATURATION 0x14, EXTCONTROL 0x18.
PRIORITY holds two 2-bit levels (bits 1:0 and 9:8), 3 = highest.
The registers only exist while the power domain is on (stream running), and the power-domain
driver restores its saved values when the domain powers up again, so a write here lasts until
the stream stops. A permanent change belongs in the dts (priority-init on the qos node).
"""
import mmap, os, struct, sys

# Only the ISP ports. Reading qos_vicap_m0 (0xfdf40600) while streaming in unite mode HUNG the
# board (2026-11-15): an unpowered/unclocked NoC block stalls the bus instead of faulting. Do not
# add addresses here unless their power domain and clocks are known to be on.
# ISP0 (left unite core) ports only: the only blocks read safely so far. ISP1 (0xfdf41000/1100)
# is in PD_ISP1 and untested; VICAP shares PD_VI with ISP0 and still hung, so "domain on" is not
# enough to call a block safe.
QOS = {
    "isp0_mro": 0xfdf40400, "isp0_mwo": 0xfdf40500,
}
REGS = (("prio", 0x08), ("mode", 0x0c), ("bw", 0x10), ("sat", 0x14), ("ext", 0x18))
PAGE = mmap.PAGESIZE


def access(addr, value=None):
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    try:
        base = addr & ~(PAGE - 1)
        m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=base)
        off = addr - base
        if value is not None:
            m[off:off + 4] = struct.pack("<I", value)
        v = struct.unpack("<I", m[off:off + 4])[0]
        m.close()
        return v
    finally:
        os.close(fd)


def show():
    print(f"{'master':14s}" + "".join(f"{n:>12s}" for n, _ in REGS))
    for name, a in QOS.items():
        print(f"{name:14s}" + "".join(f"{access(a + o):#12x}" for _, o in REGS))


def streaming():
    """The ISP power domains are on only while the mainpath streams (clocks at their run rate)."""
    try:
        txt = open("/proc/rkisp-unite0").read()
    except OSError:
        return False
    return "clk_isp_core0 702000000" in txt and "clk_isp_core1 702000000" in txt


if __name__ == "__main__":
    if not streaming():
        sys.exit("ISP not streaming (clk_isp_core0/1 not at 702 MHz): refusing to touch QoS registers")
    if len(sys.argv) == 3 and sys.argv[1] == "isp":
        val = int(sys.argv[2], 0)
        for name in QOS:
            print(f"{name}: prio {access(QOS[name] + 0x08):#x} -> {access(QOS[name] + 0x08, val):#x}")
    show()
