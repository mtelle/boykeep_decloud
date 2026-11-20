# Audio, OSD timestamp and the web settings page

This covers three features added on top of the motion-recording stack:

1. **Audio** — the microphone, in the live RTSP stream and inside the MP4 recordings (AAC).
2. **OSD** — a burned-in date/time stamp on the video.
3. **Settings page** — edit the tunables from the browser.

All three are built into `rtspd` by the patch chain in [build_rtsp.sh](../scripts/) and must be
**recompiled** (see the rebuild note at the end).

---

## 1. Audio (AAC)

> **Update: disabled again on this unit, for a different reason than before.** Everything
> below up to “Why it was off, and what changed” got the pipeline built and running - the mic
> opened, the AAC encoder opened, frames were read - but the **software AAC codec itself
> refuses this specific chip**: `libakaudiocodec.so` reads a hardware ID register via
> `/dev/mem` and prints `## ERROR: this is not CHIP(%d)` before failing the open. This is a
> vendor licensing/compatibility gate baked into the codec, not a bug in these patches - it is
> presumably why the stock firmware used G.711 instead of AAC in the first place. Audio is
> cleanly disabled at the source (`audio_enable=0` set before `init_ai`/`init_aenc` ever run,
> so the RTSP SDP never advertises a track that would otherwise never deliver data and stall
> the stream after one frame - see `src/rtsp-patches/patch_audio.py`). `MD_AUDIO` is forced to
> `0` in `scripts/start.sh`. If you want to revisit this, the registers/plumbing below are
> still relevant; the path forward would be a free AAC encoder (e.g. fdk-aac) feeding the
> existing MP4 muxer directly, bypassing the vendor's gated codec entirely.

### Why it was off, and what changed
The stock `rtsp` SDK sample already has a full audio pipeline (`init_ai` opens the ADC mic with
noise reduction, `init_aenc` encodes it, two threads push frames to the RTSP ring buffer). The
de-cloud build used to strip all of it out (`patch_noaudio.py`) because the **AAC encoder is not on
the stock camera** — the original firmware used G.711, which is not playable in a browser.

The evidence: `viCam` links `libplat_ai/ao/vqe/akaudiofilter` and its strings show `g711a/g711u`,
never AAC. The AAC encoder (`AENC_OpenAac`) lives in **`libakaac.so`**, and the encode API
(`ak_aenc_*`) in **`libmpi_aenc.so`** — **neither is present on the camera**.

So this build:

- replaces `patch_noaudio.py` with **[`patch_audio.py`](../src/rtsp-patches/patch_audio.py)**: keeps
  the mic pipeline and tees each encoded AAC frame into the recorder (`md_audio_feed`);
- keeps the AAC codec registration (`patch_enc_type.py` no longer zeroes it);
- links `-lmpi_aenc -lakaac` and **ships `libmpi_aenc.so` + `libakaac.so`** to the camera
  (`cam/bin/`, on `LD_LIBRARY_PATH`);
- sets `[audio] code_type = aac`, `sample_rate = 16000` in `rtsp.ini` (16 kHz mono).

### Live audio
The RTSP stream (`rtsp://IP:554/vs0`) now carries an AAC audio track. VLC, Home Assistant and
ONVIF clients play it. The **web live view is JPEG polling and has no sound** — live audio is via
RTSP only, by design.

### Audio in the recordings
`md_audio_feed()` in [`patch_motion.py`](../src/rtsp-patches/patch_motion.py) muxes the AAC frames
into the `.mp4` as a second `mp4a`/`esds` (AAC-LC) track. Because audio and video are interleaved in
one `mdat`, **both tracks use one-sample chunks** (every sample offset is listed in `stco`), which
is what makes the interleaved layout valid. The AAC frames arrive with an ADTS header
(`AENC_AAC_SAVE_FRAME_HEAD`); the muxer strips the 7-byte header and builds the MP4
`AudioSpecificConfig` from it. Disable with `MD_AUDIO=0`.

> **CPU note:** AAC is a *software* encoder on this SoC (the stock G.711 was near-free). 16 kHz mono
> is a few percent CPU — fine alongside the two video encoders. Audio is in the muxer only, so a
> problem never corrupts the video track: if audio never arrives the clip is a valid video-only MP4.

### A/V sync
Video sample durations come from real frame timestamps; audio is a constant 1024 samples/frame at
16 kHz. Recording begins at a video keyframe and the first audio frame follows within a few tens of
ms, so the tracks are aligned closely enough for surveillance. No edit list is used.

---

## 2. OSD burned-in date/time

[`patch_osd.py`](../src/rtsp-patches/patch_osd.py) draws `YYYY-MM-DD HH:MM:SS` with `ak_osd_*`
(the same calls the stock `viCam` used: `ak_osd_init` → `ak_osd_create_canvas` →
`ak_osd_draw_bitmap_to_canvas` → `ak_osd_set_region_to_vi`). The libs (`libmpi_osd.so`,
`libapp_osd_ex.so`) are already on the camera.

**Verified working on hardware** after two fixes to the first attempt, both confirmed on real
hardware, not just "should work":

1. **Live-only, missing from recordings.** `channel_id` on every `ak_osd_*` call is per-channel
   (`ak_osd.h`'s own comments say so), not a single "applies to the whole VI, hits both
   encoders" knob as first assumed. Channel 0 is the live/main stream; channel 1 is what motion
   recording actually encodes. Fix: create a canvas and draw/set the region on **both** channel
   0 and channel 1 every tick.
2. **Visible but garbled** (confirmed by screenshot - not blank, not a clean stamp either): the
   1bpp bitmap's bit order within each byte was a guess (MSB-first). Flipping it to LSB-first
   fixed it. If a different unit still renders it wrong, the next things to try, in order: swap
   `OSD_FG`/`OSD_BG`, try `bit_width=4` or `2` instead of `1` in `osd_attr`, or a non-`NULL`
   `color_table` instead of "use the driver default".

A compact 5×7 font (digits plus `-` and `:`) is embedded and scaled. The thread is **defensive**:
if `ak_osd_init`/`create_canvas` fails on either channel it logs to `/tmp/osd.log` and exits
without touching the video pipeline. Toggle/position with `OSD`, `OSD_X`, `OSD_Y`, `OSD_SCALE`,
`OSD_ALPHA`, `OSD_FG`, `OSD_BG`.

---

## 3. Settings page

`config.html` (linked from the live page, ⚙) reads and writes
[`cam/etc/config.env`](../deploy/config.env) via [`config.cgi`](../src/web/cgi-bin/config.cgi).
`start.sh` sources `config.env`, so it overrides the built-in defaults. All values are numeric and
validated digits-only in the CGI (no shell injection).

- **Save** writes `config.env` (applied on the next reboot).
- **Apply** writes it and reboots the camera (~20 s) so `rtspd` restarts with the new values.

Editable: motion sensitivity (`MD_CELL_TH`, `MD_MIN_CELLS`, `MD_HOLD`, `MD_MAXCLIP`), the
`MD_AUDIO` flag (currently a no-op - audio is disabled at the source, see §1 above),
retention (`MD_MINFREE`, `MD_KEEPDAYS`), and OSD.

> The first version of `config.cgi` used `tr` and `head` to parse `QUERY_STRING` and read
> `config.env` - neither exists on this busybox, so every request silently fell through to
> "dump current config" (even `?action=save`), and that dump itself always returned all
> zeros. Rewritten with pure shell (`IFS` splitting + `case` + parameter expansion) and
> `sed`/`while read`, both confirmed present; verified end to end (load, save, reload) under
> `qemu-arm-static` running this board's own `/bin/ash` before shipping it.

### Retention / auto-delete
`md_prune()` runs before each new clip:

- **space-based** (always): deletes the oldest clips until at least `MD_MINFREE` MB are free;
- **time-based** (`MD_KEEPDAYS` > 0): deletes clips older than N days and removes empty day folders.

---

## Rebuild (required)

These features are compiled into `rtspd`. After pulling the patches you **must rebuild** it in the
`ak39-build` container and copy the outputs to `cam/bin/`:

```sh
# in the SDK/build environment (Docker image ak39-build):
REPO=/path/to/repo WORK=/work bash $REPO/scripts/build_rtsp.sh
# outputs: $WORK/out/{rtspd, librtsp_server.so, libmpi_aenc.so, libakaac.so}
cp $WORK/out/rtspd $WORK/out/librtsp_server.so $WORK/out/libmpi_aenc.so $WORK/out/libakaac.so $WORK/out/libakaudiocodec.so  cam/bin/
```

Then redeploy `cam/` to the SD. Verify the new `rtspd` contains the audio/OSD code:

```sh
strings cam/bin/rtspd | grep -E "md_audio_feed|mp4a|esds|osd_th|AENC_OpenAac" | sort -u
```

---
*Produced with AI assistance; the OSD overlay in particular needs verification on the hardware.*
