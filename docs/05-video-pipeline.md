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
| `patch_daynight.py` | automatic day/night (IR-cut, IR LEDs, ISP profile) by luminance |
| `patch_noaudio.py` | no audio track (a dead track stalls `librtsp_server`) |
| `patch_enc_type.py` | codec override from the `.ini` |
| `patch_ip.py` | announce the `wlan0` IP instead of a hard-coded one |

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
