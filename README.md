# Z21S / Anyka AK3918EV300L — De-cloud

Full de-cloud of a cheap WiFi PTZ camera built on the **Anyka AK3918EV300L** SoC
(board `Z21S_MB_V2_0B`, **GC2083** sensor, **ATBM6132** WiFi). It replaces the stock cloud app
with a **fully local** stack: a self-hosted RTSP server, **hardware motion detection** with
**MP4** recording, **PTZ** control, a **web UI** (live view + camera control + recordings),
**time from your router**, and **no auto-update or telemetry**.

This repository documents the **whole process**: the firmware analysis, the evidence found inside
the device's own binaries and libraries, the software that is added, and reproducible install
instructions.

> ⚠️ **Educational / research project on your own hardware.** Modifying firmware can brick the
> device. Do it at your own risk and have UART recovery ready (and ideally an SPI programmer).
> See [docs/INSTALL.md](docs/INSTALL.md) §14 (rescue).

---

## What you get

| Feature | Detail |
|---|---|
| **All-in-one web** | `http://IP/` (port 80): live view + PTZ + recordings |
| **Local RTSP** | `rtsp://IP:554/vs0` (H.265 1080p) and `/vs1` (H.265 720p - see
  [docs/05](docs/05-video-pipeline.md)) |
| **Motion recording** | browser-playable H.264 MP4 on the SD, using the **ISP motion grid** (~0 CPU) |
| **PTZ** | pan/tilt motor control (stock `ak_motor.ko` driver) |
| **Time** | NTP from your router (no RTC battery), configurable TZ, hourly resync |
| **SSH + scp** | dropbear (port 22), password set with `passwd`; `scp -O` for real file transfer |
| **Watchdog** | supervises all services and restarts crashes; optional HW watchdog for full hangs |
| **Optional login** | HTTP Basic auth for the whole web UI (file-gated, off by default) - an
  RTSP equivalent was attempted but confirmed not to actually restrict access
  ([docs/13-auth.md](docs/13-auth.md)) |
| **No cloud** | `viCam` stopped, auto-update disabled, camera can be isolated from the internet |
| **Home Assistant** | motion binary_sensor, recordings list, single-tap PTZ - no extra camera code needed |

## How it works (technical summary)

- The **RTSP** server is built from Anyka's `rtsp` SDK sample **linked against the libraries of
  your own dump** (`libplat_*`, `libmpi_venc`, `librtsp_server`…), patched with Python scripts
  that inject functions into the source before compiling
  (see [src/rtsp-patches/](src/rtsp-patches/)).
- **Motion detection** reuses `ak_vpss_md_get_stat()` — the **per-block motion grid the ISP
  computes in hardware**, the same source the stock app used. No image is decoded ⇒ near-zero CPU
  ([docs/03-motion-recording.md](docs/03-motion-recording.md)).
- **MP4 recording** is a **hand-written ISO-BMFF muxer** inside `rtspd`: no `ffmpeg`, no `ak_mux`.
  It tees the already-encoded frames → a real `.mp4` (duration + seek).
- **PTZ** uses the reverse-engineered ioctls of the stock motor driver
  ([docs/04-ptz.md](docs/04-ptz.md)).
- **Time** is set by a **minimal SNTP client** (`sntpc`) pointed at the router, because the board
  has no battery-backed RTC and the stock BusyBox has no `ntpd`
  ([docs/06-time-web.md](docs/06-time-web.md)).
- The **web server** is a standalone static **BusyBox** cross-compiled just for its `httpd`
  applet (CGI + byte-range support) - the stock BusyBox on this board has none at all
  ([docs/10-web-server.md](docs/10-web-server.md)).
- **Audio is disabled.** The mic/AAC pipeline is fully wired up in the SDK sample, but the
  software AAC codec refuses this specific chip (a `/dev/mem` hardware-ID check, a vendor
  licensing gate, not a bug here) - likely why the stock firmware used G.711 instead
  ([docs/08-audio-osd-config.md](docs/08-audio-osd-config.md#1-audio-aac)).
- **Root access needs no UART.** An unsigned factory-test hook already present in the stock
  boot script, plus a weak (not chip-bound) checksum tool, gets you a root shell entirely
  offline from the SD card - used successfully on a unit where UART TX never worked
  ([docs/09-root-access.md](docs/09-root-access.md)).

## Repository layout

```
docs/            The study, the evidence and the install guide
  01-hardware.md            Board, UART, pinout (and why you may not need UART at all)
  02-firmware-analysis.md   Firmware: update signature/encryption, partitions
  03-motion-recording.md    Hardware MD + MP4 muxer + sensitivity tuning
  04-ptz.md                 Motor ioctls (corrected against the real header)
  05-video-pipeline.md      VI/VENC/VPSS, venc_stream
  06-time-web.md            Time (SNTP) + web UI
  07-watchdog.md            Service supervision + optional HW watchdog
  08-audio-osd-config.md    Audio (disabled, why), OSD timestamp, settings page
  09-root-access.md         Root via the unsigned SD factorytest hook, no UART needed
  10-web-server.md          The standalone static BusyBox httpd
  11-ptz-fix.md             The real ak_motor.ko ioctls + why the fd must stay open
  12-home-assistant.md      Motion sensor, recordings list, single-tap PTZ in HA
  INSTALL.md                Full step-by-step guide
src/
  rtsp-patches/                  All the ak_rtsp_stream.c injections (motion, OSD, audio...)
  tools/sntpc.c                  Minimal SNTP client
  tools/motorctl.c               Motor control via ioctl
  web/                           UI (index.html, config.html) + CGIs (ptz, rec, health, config)
scripts/
  start.sh                       Stack launcher
  camwatch.sh                    Watchdog / service supervisor
  timesync.sh                    Time from router
  ptz                            PTZ script (uses motorctl)
  build_rtsp.sh                  Patch + cross-compile rtspd
  build_httpd.sh                 Cross-compile the standalone BusyBox httpd
  build_dropbear.sh              Cross-compile dropbear with a usable PATH for scp
deploy/
  rtsp.ini                       Stream config (vs0 H.265 / vs1 H.265 - see docs/05)
  factorytest/                   Root-via-SD hook: signed wrapper + editable run.sh
```

## What is **not** here (and why)

This repo contains **only original and open-source work**. No Anyka proprietary blobs or device
material are redistributed:

- The **Anyka SDK**, `librtsp_server.so`, `libplat_*.so`, `libmpi_*.so`, `viCam` and the
  **firmware** (dump) → each user supplies these from their own device. The build pulls them from
  your dump.
- **No credentials** (passwords, hashes, keys) are included.

See [docs/INSTALL.md](docs/INSTALL.md) for how to obtain/use your own binaries.

## Quick start

1. Get root over **UART** and set your own password ([INSTALL §4](docs/INSTALL.md)).
2. **Build** `rtspd` (with the patches) and `sntpc` against your dump/SDK ([INSTALL §2](docs/INSTALL.md)).
3. Copy the package to the **microSD** as `cam/` and run `start.sh` ([INSTALL §5–6](docs/INSTALL.md)).
4. Open `http://IP/`, disable auto-update and **isolate the camera** on your router ([INSTALL §8](docs/INSTALL.md)).

## Credits

Built on top of community work; see [CREDITS.md](CREDITS.md) (the `jonathan-gtd` de-cloud project,
`onvif_simple_server` by `roleoroleo`, dropbear, BusyBox, and the segotari security-analysis
write-up).

## License

Original code under [MIT](LICENSE). Third-party components keep their own licenses.

---
*Documentation and code produced with AI assistance and verified on the hardware.*
