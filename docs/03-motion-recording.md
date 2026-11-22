# Motion detection + MP4 recording

The stock firmware detected motion and recorded clips to the SD. Here that function is recovered
**inside the de-cloud `rtspd`**, reusing the camera's own hardware.

## Key finding: hardware MD

The camera's libraries expose the reader for the **ISP motion grid**:

```
# nm -D libplat_vpss.so | grep md
00015ca8 T ak_vpss_md_get_stat
```
```c
/* ak_vpss.h */
int ak_vpss_md_get_stat(int dev, struct vpss_md_info *md);
```

`ak_vpss_md_get_stat()` returns a **grid of per-block motion magnitudes** that the ISP computes
continuously while the VPSS is running. **No image is decoded** → near-zero CPU. It is the same
source the stock `viCam` used (its calls are visible in the binary):

```
# strings viCam | grep -E 'ak_vpss_md|ak_mrd'
ak_vpss_md_get_stat
ak_mrd_init
ak_mrd_set_filters
ak_mrd_set_motion_region_size
```

### Grid dimensions

The chip has no dedicated branch in the header (`__CHIP_AK3918EV300L_SERIES`), but the SDK's own
analyzer fixes the width:

```c
/* ak_mrd.h */
#define MD_SIZE 32   // horizontal and vertical size of vpss_md matrix
```

⇒ the matrix is **32 wide** (av130-style variant). The patch declares the prototype with an
**opaque, oversized** buffer (`unsigned short grid[48][32]`, zeroed) and **sums the whole grid**:
the result is correct whatever the real height is, and there is **never an overflow**.

### Detection logic (patch)

Every `MD_PERIOD` ms (100 by default) the grid goes through **`libak_mrd`**, the same region
analyzer the stock `viCam` uses (linked against the camera's own `/opt/vicam/libak_mrd.so`,
nothing vendor-made is shipped). It groups active cells into regions and drops small, isolated or
flickering ones, using the day/night parameter sets from the SDK's own sample
(`platform/sample/mt`), switched by `/tmp/daynight.state`. Motion = at least one region of
`MD_MRD_MIN` cells or more (default 6). The grid is passed to it as `MD_ROWS` rows × 32 (default
24, the av130-family layout); `/tmp/motion.level` reports the rows actually seen as `rows=X/24`.
The SDK filter thresholds are tunable without rebuilding: `MD_MRD_BIG`/`MD_MRD_SMALL` (day,
12000/6000) and `MD_MRD_BIG_N`/`MD_MRD_SMALL_N` (night, 15000/10000).

`MD_MRD=0` falls back to the original plain count: blocks above `MD_CELL_TH`, `MD_MIN_CELLS` or
more → motion. Either way a **tail** (`MD_HOLD` s) keeps recording after motion stops.
`/tmp/motion.level` shows both: `level`/`peak` (plain count) and `boxes`/`area` (regions).
While `/tmp/daynight.busy` exists (a day/night switch or IR-off probe, see
[05-video-pipeline.md](05-video-pipeline.md#daynight-patch_daynightpy)), plus 4 s, motion is
ignored, because the whole image jumps.

### Tuning: a static scene can still trigger constantly

The grid is **32×48 = 1536 cells**. The defaults (`MD_CELL_TH=80`, `MD_MIN_CELLS=6`) were far
too sensitive on one unit: pointed at a plain, static wall, `/tmp/motion.level` showed
`level=68 peak=1123` (68 of 1536 cells simultaneously "active", one cell scoring 1123 against an
80 threshold) - a constant false trigger with nothing actually moving. The likely cause is
**auto-exposure/AGC hunting or unsynced lighting flicker** (fluorescent/LED not matched to the
sensor's shutter): it changes the brightness of a large part of the frame at once, which a plain
frame-difference detector reads as widespread "motion". Raising `MD_CELL_TH` to `1200` and
`MD_MIN_CELLS` to `25` fixed it on that unit (real motion - a person - still triggers reliably).

There is no universal good default - the background noise floor (the `peak` value) depends on
the specific lighting. To tune on a fresh install: point the camera at the actual scene, read
`cat /tmp/motion.level` a few times while nothing is moving, and set `MD_CELL_TH` comfortably
above the `peak` you see at rest; then raise `MD_MIN_CELLS` until small/localised noise stops
triggering but a person walking through still does. Both are editable from the web settings page
(see [docs/08](08-audio-osd-config.md#3-settings-page)) - but the motion thread only reads these
env vars **once, at startup**, not per tick, so a plain "Save" has no live effect: use "Apply"
(reboots and relaunches `rtspd`) to actually test a new value, or `killall -9 rtspd` + relaunch
`start.sh` over SSH for a faster iteration loop while tuning.

## Recording: a hand-written MP4 muxer

In the encoder loop, `rtspd` already has each **already-encoded** frame:

```c
/* ak_common_video.h */
struct video_stream {
    unsigned char     *data;       // stream data (Annex-B from the HW encoder)
    unsigned int       len;
    unsigned long long ts;         // timestamp (ms)
    unsigned long      seq_no;
    enum video_frame_type frame_type;
};
enum video_frame_type { FRAME_TYPE_P, FRAME_TYPE_I, FRAME_TYPE_PI };   // I = 1
enum encode_output_type { H264_ENC_TYPE, MJPEG_ENC_TYPE, HEVC_ENC_TYPE };
```

The patch **tees** that stream into a **hand-written ISO-BMFF muxer** (no `ffmpeg`, no `ak_mux`):

- `ftyp` + a streamed `mdat` (Annex-B NALs rewritten as 4-byte **length-prefixed**).
- On close it writes a full `moov`: `stsd` with **`avcC`** (H.264) or **`hvcC`** (H.265), `stts`
  with **real durations** from the encoder timestamps, and `stss` at the I-frames ⇒ a `.mp4` with
  **duration and seeking**, playable in a browser (H.264), VLC and Home Assistant.
- Rotates every `MD_MAXCLIP` s at an I-frame, and prunes the oldest clips when the SD drops below
  `MD_MINFREE` MB.

### Why not `ak_mux` (the stock muxer)?

`ak_mux_*` exists in the SDK, but:

```
# the MP4 muxer lives in libmpi_mux.so, which is NOT in /opt/vicam (only in the SDK)
# nm -D libmpi_mux.so | grep ak_mux_ ... ak_mux_open/start/add_video/stop
# readelf -d libmpi_mux.so → NEEDED libc.so.0, plus external symbol mux_mux (in libakmedia.so)
```

Using it would force shipping `libmpi_mux.so` + `libakmedia.so` from the SDK and getting their
(undocumented) buffer semantics right. The hand-written muxer is **self-contained, auditable and
dependency-free**, and is validated by opening a clip with `ffprobe`/VLC.

## Codec and web

Both stock streams were H.265; H.265 **does not play in browsers**. So `vs1` is switched to
**H.264 720p** and is the one recorded (`MD_CHN=1`), so clips play in the web UI. `vs0` stays
**H.265 1080p** for high-quality live viewing.

Code: [src/rtsp-patches/patch_motion.py](../src/rtsp-patches/patch_motion.py).
Tuning variables: see [INSTALL.md §10](INSTALL.md).

---
*Produced with AI assistance; symbols and structs taken from the device/SDK.*
