# Time sync + web UI

## Time (no RTC battery)

These cameras have **no battery-backed RTC**: at boot they don't know the time. The stock firmware
got it from the cloud; de-clouded, the time must be set or **every recording would be timestamped
1970**.

- `scripts/timesync.sh` asks your **router** for the time over **SNTP** (`sntpc`, UDP/123). If the
  router doesn't serve NTP, it falls back to the router web UI's **HTTP `Date`** header. It
  resyncs **hourly** and saves to the internal RTC with `hwclock` (helps across warm reboots).
- Timezone: `export TZ=CET-1CEST,M3.5.0,M10.5.0/3` (Spain) in `start.sh` — change to your zone.
- Other time source: `export NTP_SERVER=192.168.1.10` (your HA/NAS) before `timesync.sh`, or allow
  UDP/123 out to `pool.ntp.org`.
- Diagnostics: `cat /tmp/timesync.log`. If clip names show 1970 → the time didn't take.

The stock BusyBox has **no `ntpd`**, which is why a minimal SNTP client
([src/tools/sntpc.c](../src/tools/sntpc.c)) is shipped.

## Web UI

The camera serves a **web page on port 80** (BusyBox `httpd`) with three things:

1. **Live view** — via JPEG refresh (~5 fps, `SNAP_FPS`). Works in any browser. Full 1080p goes
   over RTSP (`rtsp://IP:554/vs0`) for VLC / Home Assistant.
2. **PTZ** — arrows ▲▼◀▶ (press and hold = move, release = stop) and ⌂ (center). They call
   `/cgi-bin/ptz.cgi` → the `ptz` script → `motorctl` → the `ak_motor.ko` driver.
3. **Motion recordings** — listed per day; each **`.mp4` H.264 plays in the browser** (▶ button)
   or downloads (⬇). Files are served from `/rec` (a symlink to the SD) with HTTP `Range`
   support, so you can **seek** inside a clip.

Open: `http://camera-IP/`.

### Served from tmpfs

`httpd` is served from **`/tmp/www`** (tmpfs): the SD is vfat and doesn't keep the CGI execute bit.
`start.sh` copies the web tree to `/tmp/www` and `chmod +x` on each boot. Recordings stay on the
SD and are reached via the `/rec` symlink.

### Security note

Everything is **LAN-only**. There is no auth on the web by default (put it on the router/VLAN, or
add HTTP basic auth to `httpd`). SSH does require the root password you set over UART.

Files: [src/web/index.html](../src/web/index.html), [src/web/cgi-bin/ptz.cgi](../src/web/cgi-bin/ptz.cgi),
[src/web/cgi-bin/rec.cgi](../src/web/cgi-bin/rec.cgi).

---
*Produced with AI assistance.*
