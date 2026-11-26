# Video pipeline

The RTSP server is built from the `platform/sample/rtsp` sample in Anyka's SDK, **linked against
the libraries of your own dump** (`libplat_vi/vpss/common`, `libmpi_venc`, `librtsp_server`,
`libapp_video`, `libmpi_osd`, …) with the `arm-unknown-linux-uclibcgnueabi` toolchain and
`-march=armv5te -D__CHIP_AK3918EV300L_SERIES`.

## Flow

```
GC2083 sensor ──(MIPI)──► VI ──► VPSS (ISP: AE/AWB, day/night, MD) ──► VENC (H.265/JPEG HW)
                                                                        │
                                           ┌────────────────────────────┤
                                           ▼                            ▼
                                   librtsp_server (RTP)          MP4 muxer (recording)
```

- **VI** captures from the sensor; **VPSS** applies the ISP (and exposes statistics: luminance,
  **MD**, AF…); **VENC** encodes to H.265 (or JPEG) in hardware; H.264 is not available on this chip.
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

**How the stock `viCam` does it** (`AdcRegionMgr.cpp`, found by disassembly). Boards with a light
sensor read it through the ADC (`AdcAux*` in `base_para.ini`). Otherwise `SoftPhotosensitive` uses
the per-sensor section `[SOFTPHSET_GC2083]` (version 2):

- EV = exposure time × analog gain × digital gain × ISP gain (`ak_vpss_get_ae_run_info`); higher
  means darker. R/G and B/G come from the AWB statistics (`ak_vpss_get_awb_stat_info`).
- Night if EV ≥ 5500. Stay night while EV ≥ 1000.
- Day if EV < 1000 and (R/G < 0.90 or B/G < 0.85), or if EV < 300 and B/G < 0.95.
  Otherwise it keeps the current state.
- `PhotosensitiveLock`: if it goes from night to day more than twice within a minute, it locks the
  state for 30 minutes.
- On a switch it calls `ak_vi_switch_mode`, waits 200 ms and adjusts the encoder fps/GOP.

It never turns the IR off to measure. This patch keeps the same colour idea, but as a change from a
learnt baseline confirmed by an IR-off probe, because the stock fixed thresholds come from AWB
statistics this patch does not read and were never measured on this unit.

## Streams (`deploy/rtsp.ini`)

| Stream | Codec | Resolution | Use |
|---|---|---|---|
| `vs0` | H.265 | 1920×1080 | live viewing (VLC / Home Assistant) |
| `vs1` | H.265 | 1280×720 | motion recording + in-browser live |

`isp_conf` points to the **GC2083** profile. If the CPU struggles with dual-stream, lower `vs1`.

> ⚠️ **No H.264 on this chip, only H.265 (and JPEG). Tested on hardware.**
>
> - `libmpi_venc.so` V2.0.44 rejects it: `venc_check_and_format_param` accepts only
>   `enc_out_type` 1 (JPEG) or 2 (H.265) and logs `enc_out_type error 0` for H.264. The SDK's
>   V3.0.02 is the mirror image (H.264 + JPEG): Anyka builds this library per chip.
> - The kernel driver `ak_venc_adapter.ko` contains the Allegro H.264 code (it is shared with the
>   AK3918AV100), but `AkvEncSetParams` rewrites any H.264 profile to H.265 Main ("AVC profile is
>   not supported, it will be adjusted").
> - With both blocks patched out in private test copies, the encoder produced one ~5 KB H.264
>   I-frame and then stopped raising interrupts. The capture threads blocked and every stream,
>   including the H.265 one, stalled. The H.264 path does not work on the EV300L.
>
> An earlier note here said `vs0` "silently falls back to MJPEG" when asked for H.264. That was a
> config bug, not the encoder: the rtsp.ini parser only treats `#` at the start of a line as a
> comment, so `code_type = h.265  # ...` was read as an unknown codec, and the SDK sample falls
> back to JPEG for unknown codecs. Keep comments on their own lines in `rtsp.ini`.
>
> Consequence for the web UI's in-browser playback of recordings: H.265 is **not** reliably
> supported by `<video>` in every browser (Safari/hardware-accelerated Chrome tend to be fine,
> plain desktop Chrome/Firefox often are not). VLC/`ffplay`/Home Assistant always work.

---
*Produced with AI assistance; API and structs taken from the SDK/device.*
