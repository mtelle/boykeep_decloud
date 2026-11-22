# Video pipeline

The RTSP server is built from the `platform/sample/rtsp` sample in Anyka's SDK, **linked against
the libraries of your own dump** (`libplat_vi/vpss/common`, `libmpi_venc`, `librtsp_server`,
`libapp_video`, `libmpi_osd`, …) with the `arm-unknown-linux-uclibcgnueabi` toolchain and
`-march=armv5te -D__CHIP_AK3918EV300L_SERIES`.

## Flow

```
GC2083 sensor ──(MIPI)──► VI ──► VPSS (ISP: AE/AWB, day/night, MD) ──► VENC (H.264/H.265 HW)
                                                                        │
                                           ┌────────────────────────────┤
                                           ▼                            ▼
                                   librtsp_server (RTP)          MP4 muxer (recording)
```

- **VI** captures from the sensor; **VPSS** applies the ISP (and exposes statistics: luminance,
  **MD**, AF…); **VENC** encodes to H.264 or H.265 in hardware.
- The `video_encode_from_vi_th` loop pulls each frame with `ak_app_video_venc_get_stream()` →
  `struct video_stream` (see [03-motion-recording.md](03-motion-recording.md)) and pushes it to
  `librtsp_server`. The recording patch **tees** that same frame.

## Patches applied to `rtspd`

Injected into the `.c` **before compiling** (scripts in [src/rtsp-patches/](../src/rtsp-patches/)
and those of the upstream project):

| Patch | Purpose |
|---|---|
| `patch_motion.py` | **(this repo)** motion detection + MP4 muxer to the SD |
| `patch_snapshot.py` | JPEG thread (snapshot ~N fps) for the web live view |
| `patch_daynight.py` | automatic day/night (IR-cut, IR LEDs, ISP profile) - see below |
| `patch_noaudio.py` | no audio track (a dead track stalls `librtsp_server`) |
| `patch_enc_type.py` | codec override from the `.ini` |
| `patch_ip.py` | announce the `wlan0` IP instead of a hard-coded one |

### Day/night (`patch_daynight.py`)

This board has no light sensor: the ADC channel the stock `base_para.ini` points at
(`in_voltage1_raw`) is the unpopulated battery divider and reads a constant ~3115. The SDK's
`ak_vpss_get_auto_day_night_level` never proposed night on this unit. So the thread decides from
the ISP itself:

- **Day → night:** the ISP luminance index (`ak_vpss_get_cur_lumi`, high = dark; daylight ~100,
  dark ~54575, which is the ceiling) above `DN_D2N` (3584) for `DN_D2N_S` (10) s.
- **Night → day:** in night mode that index is useless - the night ISP profile plus our own IR
  LEDs read ~9 in a pitch-dark room, lower than daylight. The decision is an **IR-off probe**: IR
  LEDs off and the day profile loaded for up to `DN_PROBE_MS` (4000) ms. It goes to day only if the
  *highest* reading stays below `DN_N2D` (1536). Taking the maximum defeats AE lag, and the probe
  stops as soon as it reads > `DN_D2N`, so in the dark it is a sub-second blip.
- **When to probe:** this is the stock firmware's `SoftPhotosensitive` idea. Under pure IR the
  sensor's R, G and B averages (`ak_vpss_get_rgb_average`) are nearly equal (measured R/G =
  B/G = 104-105), and visible light shifts that ratio (a warm lamp: B/G 105 → 89). After entering
  night the thread learns the current colour. A shift of `DN_COLOR` (8) points sustained for
  `DN_COLOR_S` (20) s probes at once, so in real darkness there is no blip at all. A probe that
  finds it still dark re-learns the colour, so a dim lamp probes once, not every 20 s. A fallback
  probe runs every `DN_PROBE` (1800) s, for light that does not shift the colour.
- `DN_HOLD` (60) s minimum per state; the boot-time decision does not start it.
- While switching or probing, `/tmp/daynight.busy` exists and motion detection ignores the frames
  (plus 4 s), so the image jump does not start a recording.

Manual override: `echo day|night|auto > /tmp/daynight.mode`. `/tmp/daynight.log` shows `lumi`,
`rg`/`bg`, the learnt `base`, `dev`/`ccnt` (colour shift and how long it has lasted) and the last
`probe` value. `/tmp/rtsp.log` has one `DAYNIGHT: probe (color|timer, ...)` line per probe.
`DN_COLOR=0` disables the colour trigger (then set `DN_PROBE=300`), and `DN=0` disables the whole
thread.

## Streams (`deploy/rtsp.ini`)

| Stream | Codec | Resolution | Use |
|---|---|---|---|
| `vs0` | H.265 | 1920×1080 | live viewing (VLC / Home Assistant) |
| `vs1` | H.265 ⚠️ | 1280×720 | motion recording + in-browser live |

`isp_conf` points to the **GC2083** profile. If the CPU struggles with dual-stream, lower `vs1`.

> ⚠️ **Neither `vs0` nor `vs1` can really do H.264 on this chip/SDK - confirmed on hardware,
> tried both channels.** `libmpi_venc V2.0.44`:
> - On `vs1`: `venc_check_and_format_param` rejects `enc_out_type=0` (H.264) outright, with an
> explicit `enc_out_type error 0` in the log, and `ak_venc_open_ex` fails (`start_venc` /
> `open venc failed`).
> - On `vs0`: no error at all - `ak_venc_open_ex` reports success - but the encoder **silently
> falls back to MJPEG**. Confirmed live with a temporary debug `printf` on every keyframe of
> `md_record_feed`: with `video0`'s `code_type=h.264` in `rtsp.ini` and zero errors in the log,
> `chn=0` still reported `enc=1` (`MJPEG_ENC_TYPE`) on every single frame, never `enc=0`.
>
> Both channels therefore run H.265 (`vs0` natively via `code_type=h.265`; `vs1` via the
> `enc_type=2`/`profile_type=4` override in `deploy/rtsp.ini`, since its own `code_type=h.264`
> gets rejected as above). This is a real, apparently intentional hardware/firmware limitation
> for this chip tier, not a configuration mistake - don't retry this without new evidence.
>
> Consequence for the web UI's in-browser playback of recordings: H.265 is **not** reliably
> supported by `<video>` in every browser (Safari/hardware-accelerated Chrome tend to be fine,
> plain desktop Chrome/Firefox often are not, and clips can stall partway through rather than
> finish) - VLC/`ffplay`/Home Assistant always work regardless. A real fix would mean
> re-encoding to MJPEG (a sequence of JPEGs, universally browser-compatible, no video decoder
> needed) with a custom JS player - bigger files, not yet implemented - or just
> downloading/opening in VLC.

---
*Produced with AI assistance; API and structs taken from the SDK/device.*
