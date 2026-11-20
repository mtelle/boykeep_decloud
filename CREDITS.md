# Credits

This project builds on prior community work and open-source software.

- **jonathan-gtd/anyka-ak3918ev300l-decloud** — the upstream de-cloud project for Anyka
  AK3918EV300L cameras: the RTSP build approach, the patch framework (`patch_daynight.py`,
  `patch_snapshot.py`, `patch_noaudio.py`, `patch_enc_type.py`, `patch_ip.py`) and the overall
  method. This repo adds motion detection + a native MP4 muxer, a router NTP client, a new web UI,
  and adapts everything to the `Z21S_MB_V2_0B` board layout (app on `mtd3`, libs at `/opt/vicam`).
- **roleoroleo/onvif_simple_server** — ONVIF + WS-Discovery servers used for NVR/Home Assistant
  integration.
- **BusyBox**, **Dropbear SSH**, **zlib**, **json-c** — standard userspace components.
- **Anyka** — the AK3918 SDK and libraries (proprietary; **not** redistributed here — supplied by
  each user from their own device/dump).
- The **segotari** security write-up on this class of IoT cameras, which informed the firmware
  update-mechanism analysis.

Original code in this repository (`patch_motion.py`, `sntpc.c`, `motorctl.c`, the web UI, the
scripts) is released under the MIT license (see `LICENSE`). Third-party components retain their
respective licenses.

---
*Produced with AI assistance and verified on the hardware.*
