#!/bin/sh
# camwatch.sh - supervisor for the de-cloud stack.
# Checks the critical services every WD_INT seconds and restarts any that died.
# Writes a health snapshot to /tmp/watchdog.status and a log to /tmp/watchdog.log.
# Optional: with WATCHDOG_HW=1 it also feeds the SoC hardware watchdog (/dev/watchdog) so a TOTAL
# system hang (kernel / WiFi lockup) reboots the camera. OFF by default (avoids reboot loops).
#
# Started by start.sh, so it inherits its environment (LD_LIBRARY_PATH, MD_*, TZ, ONVIF_CONF...).
CAM=/mnt/sdcard/cam
INT=${WD_INT:-10}
WLAN=$(cat /etc/wlanname 2>/dev/null || echo wlan0)
log(){ echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> /tmp/watchdog.log; }

# /tmp is tmpfs (RAM, not the SD) - with no rotation, rtspd's own periodic prints
# ("encode fps:NN" every ~2s per channel) would grow /tmp/rtsp.log forever and can
# eat into this board's small 64MB RAM over a long uptime. Truncating in place (not
# deleting) is safe even while a process still has the file open for writing - it frees
# the old content immediately and the writer's next write just creates a harmless sparse
# hole up to its old offset (same trick as `logrotate`'s copytruncate).
MAXLOGKB=${MAXLOGKB:-512}
rotate_log(){   # $1 = path
  [ -f "$1" ] || return 0
  sz=$(stat -c %s "$1" 2>/dev/null || echo 0)
  if [ "${sz:-0}" -gt $((MAXLOGKB*1024)) ]; then : > "$1"; fi
}

alive(){ pidof "$1" >/dev/null 2>&1 || ps 2>/dev/null | grep -v grep | grep -q "[ /]$1"; }

start_rtsp(){ $CAM/bin/rtspd -i $CAM/etc/rtsp.ini >/tmp/rtsp.log 2>&1 & }
start_httpd(){ $CAM/bin/busybox httpd -p 80 -h /tmp/www -c /tmp/httpd.conf; }  # generated once by start.sh from auth.env
start_ssh(){
  [ -f /tmp/hk ] || $CAM/bin/dropbearmulti dropbearkey -t ed25519 -f /tmp/hk >/dev/null 2>&1
  $CAM/bin/dropbearmulti dropbear -r /tmp/hk -p 22
}
start_wsd(){
  [ -x $CAM/bin/wsd_simple_server ] || return 0
  SCOPES=$(grep '^scope=' /tmp/onvif.conf 2>/dev/null | cut -d= -f2- | tr '\n' ' ')
  $CAM/bin/wsd_simple_server -i $WLAN -x "http://%s/onvif/device_service" -m AK3918EV300L-PTZ -n Anyka -s "$SCOPES" -p /tmp/wsd.pid -t $CAM/bin/templates/wsd_files &
}

HW=""
if [ "${WATCHDOG_HW:-0}" = 1 ] && [ -e /dev/watchdog ]; then
  if exec 9>/dev/watchdog; then HW=1; printf 'x' >&9; log "HW watchdog armed (/dev/watchdog)"; fi
fi

log "camwatch started (interval ${INT}s, HW=${HW:-off})"
while :; do
  alive rtspd    || { log "rtspd down -> restart";    start_rtsp;  }
  alive httpd    || { log "httpd down -> restart";    start_httpd; }
  alive dropbear || { log "dropbear down -> restart"; start_ssh;   }
  alive wsd_simple_server || start_wsd

  for f in /tmp/rtsp.log /tmp/watchdog.log /tmp/osd.log /tmp/timesync.log; do rotate_log "$f"; done

  # feed the HW watchdog only while this supervisor loop is actually running
  [ -n "$HW" ] && printf 'x' >&9 2>/dev/null

  # health snapshot (used by the web /cgi-bin/health.cgi)
  {
    echo "time=$(date '+%Y-%m-%d %H:%M:%S')"
    for p in rtspd httpd dropbear wsd_simple_server; do
      alive "$p" && echo "$p=up" || echo "$p=DOWN"
    done
    echo "recording=$( [ -f /tmp/motion.rec ] && echo yes || echo no )"
    echo "motion_level=$(sed -n 's/.*level=\([0-9]*\).*/\1/p' /tmp/motion.level 2>/dev/null)"
    echo "sd_free_mb=$(df -m /mnt/sdcard 2>/dev/null | awk 'NR==2{print $4}')"
    echo "uptime_s=$(cut -d. -f1 /proc/uptime 2>/dev/null)"
    echo "hw_watchdog=${HW:-off}"
  } > /tmp/watchdog.status 2>/dev/null

  sleep "$INT"
done
