#!/bin/sh
# timesync.sh - set the clock from the LAN router (no RTC battery, no internet).
# Primary: SNTP (UDP/123) to the default gateway or $NTP_SERVER. Fallback: HTTP Date header.
# Resyncs hourly. Timezone defaults to Spain (CET/CEST).
CAM=/mnt/sdcard/cam
export TZ=${TZ:-CET-1CEST,M3.5.0,M10.5.0/3}

gw() {
  G=$NTP_SERVER
  [ -z "$G" ] && G=$(ip route 2>/dev/null | awk '/default/{print $3; exit}')
  [ -z "$G" ] && G=$(route -n 2>/dev/null | awk '/^0.0.0.0/{print $2; exit}')
  echo "$G"
}
log(){ echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> /tmp/timesync.log; }

sync_once() {
  G=$(gw)
  [ -z "$G" ] && { log "no gateway"; return 1; }
  if [ -x "$CAM/bin/sntpc" ] && "$CAM/bin/sntpc" "$G" --set >/dev/null 2>&1; then
    hwclock -w 2>/dev/null; log "sntp $G -> $(date)"; return 0
  fi
  D=$(wget -S -q -O /dev/null "http://$G/" 2>&1 | sed -n 's/^ *[Dd]ate: //p' | head -1)
  if [ -n "$D" ] && date -u -s "$D" >/dev/null 2>&1; then
    hwclock -w 2>/dev/null; log "http-date $G -> $(date)"; return 0
  fi
  log "sync failed (gw=$G)"; return 1
}

sync_once
( while :; do sleep 3600; sync_once; done ) &
