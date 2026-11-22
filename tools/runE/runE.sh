#!/bin/bash
# Run E: NVMe write pattern vs left ISP core. Runs on the Pi as cam1, alongside a recording to RAM.
#   runE.sh MODE SECONDS [MBPS]     MODE = burst (64 MB cached write + fsync every 2 s)
#                                         dburst (same, O_DIRECT: no page cache / writeback)
#                                         dburst1m (same, as 64 x 1 MB O_DIRECT writes)
#                                         paced (MBPS MB/s steady, 1 MB O_DIRECT writes)
#                                         none  (stats only)
# Logs to ~/runE/<MODE>_<time>/: stat.log (1 Hz unite_mi_stat, unite_isp_stat, Dirty, Writeback),
# bursts.log (start/end of each burst) or paced.log.
MODE=$1; DUR=$2; MBPS=${3:-32}
D=~/runE; OUT=$D/${MODE}_$(date +%H%M%S); mkdir -p "$OUT"
P=/sys/module/video_rkisp/parameters
[ -f /dev/shm/rand64 ] || head -c 64M /dev/urandom > /dev/shm/rand64
END=$((SECONDS + DUR))
echo "$OUT"

( while [ $SECONDS -lt $END ]; do
    echo "$(date +%s.%N) mi=$(cat $P/unite_mi_stat) isp=$(cat $P/unite_isp_stat)" \
         "$(awk '/^(Dirty|Writeback):/{printf "%s%s ", $1, $2}' /proc/meminfo)"
    sleep 1
  done ) > "$OUT/stat.log" &

case $MODE in
  burst)
    while [ $SECONDS -lt $END ]; do
      t0=$(date +%s.%N)
      dd if=/dev/shm/rand64 of=$D/burst.bin bs=1M conv=fsync status=none
      echo "$t0 $(date +%s.%N)" >> "$OUT/bursts.log"
      sleep 2
    done ;;
  dburst)   # same 64 MB burst as 'burst' but O_DIRECT: no page cache, no writeback kworker,
            # still many NVMe requests in flight at once (the bio is split by max_sectors_kb)
    while [ $SECONDS -lt $END ]; do
      t0=$(date +%s.%N)
      dd if=/dev/shm/rand64 of=$D/burst.bin bs=64M oflag=direct conv=fsync status=none
      echo "$t0 $(date +%s.%N)" >> "$OUT/bursts.log"
      sleep 2
    done ;;
  dburst1m) # same 64 MB burst every 2 s, O_DIRECT, but 64 x 1 MB writes from one small buffer
    while [ $SECONDS -lt $END ]; do
      t0=$(date +%s.%N)
      dd if=/dev/shm/rand64 of=$D/burst.bin bs=1M count=64 oflag=direct conv=fsync status=none
      echo "$t0 $(date +%s.%N)" >> "$OUT/bursts.log"
      sleep 2
    done ;;
  paced) $D/pacedwrite $D/paced.bin "$MBPS" "$DUR" > "$OUT/paced.log" ;;
  none)  sleep "$DUR" ;;
esac
wait
rm -f $D/burst.bin $D/paced.bin
echo DONE
