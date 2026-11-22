# Install guide — Z21S / Anyka AK3918EV300L de-cloud

Turn the camera into a **100 % local** device: no cloud, a self-hosted RTSP server, a web UI with
**live view + PTZ + motion recordings (MP4)**, time from your router, and no auto-update.

> **Board:** `Z21S_MB_V2_0B` · **SoC:** AK3918EV300L (ARM926, ARMv5TE) · **Sensor:** GC2083 ·
> **WiFi:** ATBM6132 · **Base firmware:** `z21s-2_f6m05_LV261117.11163`.
> Go slow and read §14 (rescue) before you start. **UART is not actually required** - on at
> least one unit the TX line (adapter→board) never worked reliably over several sessions,
> while RX (board→adapter, i.e. reading boot logs) was fine throughout. Root was reached
> entirely via the **unsigned SD `factorytest` hook** instead - see
> [docs/09-root-access.md](09-root-access.md). Treat §4 below as the path to try first if your
> UART wiring is solid; if typing into the console does not work, do not fight it - go straight
> to §4-alt.

---

## 0. What you will get

| Feature | Detail |
|---|---|
| **All-in-one web** | `http://camera-IP/` (port 80) |
| **Live view** | in the web (~5 fps) · full 1080p via `rtsp://IP:554/vs0` (VLC / Home Assistant) |
| **PTZ** | arrows in the web (hold = move, release = stop) |
| **Motion recording** | browser-playable H.264 `.mp4` on the SD |
| **Time** | router NTP (no RTC battery), configurable TZ, hourly resync |
| **SSH** | `root@IP` (port 22), password set with `passwd` once you have a root shell (UART or SD) |
| **No cloud** | viCam stopped, auto-update disabled, camera isolated from the internet |

**Ports:** 80 web · 554 RTSP · 22 SSH.
**Streams:** `vs0` = H.265 1080p (live) · `vs1` = H.264 720p (recording + web).

---

## 1. Requirements

**Hardware**
- **USB-TTL 3.3 V** adapter (CP2102/FT232/CH340), dupont wires, fine-tip soldering iron.
- microSD card reader + a microSD (FAT32).
- (Recommended, as rescue) CH341A SPI programmer + SOIC8 clip.

**Software (on your Linux/WSL PC)**
- Docker (to build) and this repo cloned.
- Your own **firmware dump** of the camera and the **Anyka AK3918 SDK** (not shipped here — see §2).
- A serial terminal: `minicom`, `picocom` or PuTTY.

> This repo contains only original/open-source work. The Anyka SDK, the proprietary `.so`
> libraries, `viCam` and the firmware dump come **from your own device**.

---

## 2. Build the binaries

The RTSP server is compiled from the Anyka `rtsp` SDK sample, patched with the scripts in
`src/rtsp-patches/`, and **linked against the libraries extracted from your dump**. Using the
upstream build container (`ak39-build`):

```bash
# inside your build tree (SDK + extracted vendor libs in work/)
sudo docker run --rm -t -v "$PWD":/repo -v "$PWD/work":/work ak39-build bash -lc '
  rtsp/build_rtsp.sh &&
  XT=/opt/xt/x-tools/arm-unknown-linux-uclibcgnueabi/bin &&
  $XT/arm-unknown-linux-uclibcgnueabi-gcc -march=armv5te -marm -static -O2 -o /work/out/sntpc /repo/tools/sntpc.c &&
  $XT/arm-unknown-linux-uclibcgnueabi-gcc -march=armv5te -marm -static -O2 -o /work/out/motorctl /repo/tools/motorctl.c &&
  echo BUILD_OK'
```

You should see `BUILD_RTSP_OK` and `BUILD_OK`. Outputs land in `work/out/`:
`rtspd`, `librtsp_server.so`, `sntpc`, `motorctl`.

> `build_rtsp.sh` copies the SDK `rtsp` sample, runs the Python patches (motion detection + MP4
> muxer, day/night, snapshot, no-audio, IP announce), then compiles and links against your
> `work/vendor/app/vicam/*.so`.

---

## 3. Assemble the SD package

Lay out a `cam/` folder:

```
cam/
├── start.sh                 # scripts/start.sh
├── timesync.sh              # scripts/timesync.sh
├── bin/
│   ├── rtspd  librtsp_server.so   # from work/out
│   ├── sntpc  motorctl             # from work/out
│   ├── ptz                         # scripts/ptz
│   ├── dropbearmulti               # Dropbear (multi-call)
│   └── onvif_simple_server  wsd_simple_server  templates/   # roleoroleo (optional)
├── etc/
│   └── rtsp.ini             # deploy/rtsp.ini
└── www/
    ├── index.html           # src/web/index.html
    └── cgi-bin/ptz.cgi rec.cgi      # src/web/cgi-bin/
```

> Strip CRLF on every shell script (`sed -i 's/\r$//' …`) — the camera's `/bin/sh` chokes on
> `#!/bin/sh␍`.

---

## 4. Root over UART (one time)

### 4.1 Wiring
- Pads on the **left edge** of the board, top to bottom: **GND / TXD / RXD**.
- **Cross** wiring: board **TXD → RX** adapter · board **RXD → TX** · **GND ↔ GND**.
- **Do not** connect the adapter's VCC/3V3/5V.
- Terminal at **115200 8N1**, no flow control.

### 4.2 Boot to a root shell
1. Power on and watch for `[[ <hex> ]]` right before `autoboot in 3 seconds`. On builds that
   print this, autoboot is **keyed**: any random keypress does nothing, you must paste back
   that exact hex string (observed **fixed** across reboots on one unit, so copy it once and
   reuse it) within the 3 s window, then Enter, to reach the U-Boot prompt. On older builds
   without that line, mashing a key is enough.
2. Record the original value: `printenv bootargs`
3. Boot to a password-less shell:
   ```
   setenv bootargs 'console=ttySAK0,115200n8 root=/dev/mtdblock2 rootfstype=squashfs rw init=/bin/sh'
   boot
   ```

### 4.3 Set YOUR root password
```sh
mount -t proc proc /proc
mount -t jffs2 /dev/mtdblock4 /etc
passwd root        # set your own
sync
```
Reboot normally (drop `init=/bin/sh`): in U-Boot `setenv bootargs '<the original>'` and `boot`,
or power-cycle.

---

## 4-alt. Root via SD card, no UART at all

If your UART TX never works reliably (cheap USB-TTL adapter, a cold solder joint, or the
keyed-autoboot token above is too tight a window to paste into), skip UART entirely. The stock
boot script (`rcS`) has an **unsigned** hook that runs before the cloud app starts, meant for
factory testing, and it only needs a plain MD5 chain plus a weak (not chip-bound) checksum
tool called `ukey` - both are satisfiable entirely offline, with files you build yourself and
drop on the SD card. No UART, no firmware signing key, no flashing. Full writeup, including how
the `ukey` checksum was reverse engineered and verified safe to forge:
**[docs/09-root-access.md](09-root-access.md)**.

---

## 5. Copy to the microSD

Format FAT32 and copy the package as a folder named **`cam`**:

```bash
cp -r cam /mnt/d/cam && sync      # adjust /mnt/d to your card reader
```
On the camera it will be at `/mnt/sdcard/cam`.

---

## 6. First boot (test, no persistence yet)

With the SD inserted, in the root shell (UART or SSH):

```sh
sh /mnt/sdcard/cam/start.sh
sleep 8
cat /tmp/rtsp.log            # encoder should start with no errors
```

**Verify:**
- **Web:** open `http://camera-IP/` → live view, the arrow pad and the recordings section.
- **RTSP:** `ffplay -rtsp_transport tcp rtsp://IP:554/vs0`
- **PTZ:** press an arrow in the web (or `/mnt/sdcard/cam/bin/ptz left` / `stop`).
- **Motion:** walk in front → a `.mp4` appears under `/mnt/sdcard/rec/<date>/` and in the web.
- **Time:** `date` (correct) and `cat /tmp/timesync.log`.

> **If a binary gives "Permission denied"** (FAT drops +x): usually fine for executables (vfat
> mounts them executable), but if it happens:
> `mkdir -p /tmp/cam && cp -r /mnt/sdcard/cam/bin/* /tmp/cam/ && chmod +x /tmp/cam/*`
> and edit `start.sh` to `CAM=/tmp/cam` (the web CGIs are already copied to tmpfs automatically).

---

## 7. Persistence (auto-start)

To launch everything at boot **without** the cloud app:

```sh
mount -t jffs2 /dev/mtdblock4 /etc        # if not already mounted
vi /etc/init.d/rcS
```
In `rcS`:
- **Comment out** the line that starts viCam/autoboot:
  `#[ -x /opt/autoboot.sh ] && /opt/autoboot.sh`
- **Comment out** the auto-update lines (§8).
- **Append**:
  ```sh
  [ -x /mnt/sdcard/cam/start.sh ] && sh /mnt/sdcard/cam/start.sh &
  ```
Save and `sync`. Reboot and confirm it comes up with RTSP + web + PTZ and **no** viCam
(`ps | grep viCam` returns nothing).

---

## 8. Disable auto-update and isolate the network

In `/etc/init.d/rcS` comment out (if present):
- `#/usr/local/tool/sdupdate_fwenc.sh`
- the `check_auth.sh /mnt/sdcard && sh /mnt/sdcard/fwup_autorun.sh` line

And in the shell:
```sh
: > /etc/conf/upgrade.url
rm -f /etc/conf/upgrading
touch /etc/closewdog
sync
```

**On your router:** keep the camera **LAN-only** (no internet). For the clock it uses your
**router** over NTP, so it needs no internet. If your router doesn't serve NTP, see §11.

---

## 9. Using the web

Open `http://camera-IP/`:
- **Live view:** ~5 fps image. For full 1080p use `rtsp://IP:554/vs0` in VLC/HA.
- **PTZ:** ▲▼◀▶ hold to move, release to stop; ⌂ centers (homing).
- **Recordings:** per-day list. ▶ plays the `.mp4` in the browser; ⬇ downloads it.

You can also pull recordings over **SFTP**: `sftp root@IP` → `/mnt/sdcard/rec`.

Add to **Home Assistant / NVR**: via ONVIF (IP, port 80) or direct RTSP
(`rtsp://IP:554/vs0` and `/vs1`).

---

## 10. Motion recording — tuning

Configured by environment variables in `start.sh` (section `2b`). Detection uses the ISP
**motion grid** (`ak_vpss_md_get_stat`), with no CPU cost.

| Var | Default | Purpose |
|---|---|---|
| `MD` | 1 | `MD=0` disables everything. |
| `MD_FMT` | mp4 | `mp4` (browsable) or `raw` (.h264/.h265, diagnostic). |
| `MD_CHN` | 1 | Stream to record: 1 = `vs1` (H.264 720p). |
| `MD_CELL_TH` | 80 | Per-block threshold. Raise if false positives. |
| `MD_MIN_CELLS` | 6 | Blocks in motion to trigger. Higher = less sensitive. |
| `MD_HOLD` | 8 | Seconds to keep recording after motion stops. |
| `MD_PERIOD` | 300 | Grid polling period (ms). |
| `MD_MAXCLIP` | 120 | Rotate the clip every N s (at an I-frame). |
| `MD_MINFREE` | 200 | Min free MB on the SD; below this, delete the oldest. |
| `MD_DIR` | /mnt/sdcard/rec | Recordings folder (served at `/rec`). |
| `MD_HOOK` | — | Command run once at each motion start (push/MQTT/email). |

**Signals in `/tmp`:** `motion.active` (exists while recording) · `motion.rec` (current file) ·
`motion.level` (diagnostic: active blocks, peak, state).

**Calibrate (day one):**
1. Scene still: `cat /tmp/motion.level` → note `level`.
2. Walk in front: see how much it rises.
3. Set `MD_MIN_CELLS` midway; raise `MD_CELL_TH` if night IR grain triggers false positives.

---

## 11. Time

- `timesync.sh` asks your **router** for the time over **SNTP** (`sntpc`, UDP/123); otherwise it
  uses the router web UI's **HTTP `Date`** header. It resyncs hourly and saves to the RTC with
  `hwclock`.
- Timezone: `export TZ=CET-1CEST,M3.5.0,M10.5.0/3` (Spain) in `start.sh` — change to your zone.
- Other source: `export NTP_SERVER=192.168.1.10` (your HA/NAS) before `timesync.sh`.
- Diagnostics: `cat /tmp/timesync.log`. Clip names showing 1970 → the time didn't take.

---

## 12. Tuning

- **CPU / codec:** `vs1` at 720p H.264 **on top of** 1080p H.265 may be tight. If `/tmp/rtsp.log`
  complains (drops, low "encode fps"), lower `vs1` in `etc/rtsp.ini` to 640×360.
- **PTZ reversed:** edit `bin/ptz` and flip `PAN_SIGN` / `TILT_SIGN` to `-1`.
- **Green/black image:** it's the GC2083 ISP config; confirm
  `isp_conf = ...isp_gc2083_mipi_2lane_h3b.conf` in `etc/rtsp.ini`.
- **MP4 won't open / play:** force `MD_FMT=raw` to isolate (confirm detection records), then check
  a clip with `ffprobe`.
- **Smoother live:** raise `SNAP_FPS` in `start.sh` (more CPU).

---

## 13. Diagnostics (logs)

```sh
cat /tmp/rtsp.log         # RTSP / encoder / ISP startup
cat /tmp/motion.level     # motion detection state
cat /tmp/timesync.log     # time sync
cat /tmp/daynight.log     # day/night (luminance, colour shift, last IR-off probe)
grep DAYNIGHT /tmp/rtsp.log   # day/night switches and probes
ps | grep -E 'rtspd|httpd|dropbear|viCam'
ls -l /mnt/sdcard/rec/*/  # recordings
```

---

## 14. Rescue (if something breaks)

- **Back to normal boot:** UART → U-Boot → `setenv bootargs '<the original>'` → `boot`.
- **Undo boot changes:** `/etc` is jffs2; edit `/etc/init.d/rcS`, remove your lines and reboot.
- **Worst case:** SPI programmer on the NOR to rewrite the original firmware (back it up first if
  you can).

---

## Appendix — partition map (this board)

`mtd0` uboot · `mtd1` kernel · `mtd2` rootfs (squashfs) · `mtd3` user/app · `mtd4` jffs2 (`/etc`) ·
`mtd5` factory.

---
*Produced with AI assistance; verified on the hardware.*
