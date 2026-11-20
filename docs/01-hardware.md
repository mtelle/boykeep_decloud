# Hardware

| Component | Detail |
|---|---|
| Board | `Z21S_MB_V2_0B` |
| SoC | Anyka **AK3918EV300L** (ARM926EJ-S, ARMv5TE, little-endian, soft-float) |
| Sensor | GalaxyCore **GC2083** (MIPI 2-lane), ISP config `isp_gc2083_mipi_2lane_h3b.conf` |
| WiFi | **ATBM6132** (stock `atbm` driver) |
| Flash | SPI NOR 8 MiB |
| Userspace | uClibc-ng 1.0.31, BusyBox, squashfs (xz) + jffs2 |
| Motors | 2× stepper (pan/tilt), `ak_motor.ko` driver |

## UART (serial console)

- **Pads on the left edge** of the board, top to bottom: **GND / TXD / RXD**.
- **3.3 V** logic. Port: `ttySAK0`, **115200 8N1**, no flow control.
- **Cross** wiring: board **TXD → RX** of the adapter · board **RXD → TX** · **GND ↔ GND**.
- **Do not** connect the adapter's power (VCC/3V3/5V): the camera is powered separately.

Listen first: power on and confirm you see the U-Boot log. Garbage on screen → wrong baud or
TX/RX swapped. A clean log with the board's own RX (its TX → your adapter) does **not** prove
your TX (your adapter → its RX) works - on one unit, RX was perfect for an entire session while
TX never got a single byte through (loopback-tested on the adapter itself: fine). If typing
into the console does nothing, don't keep resoldering - see **§4-alt** in
[docs/INSTALL.md](INSTALL.md) for a root path that needs no UART at all.

## Root via U-Boot

Watch for a line like `[[ <16 hex chars> ]]` just before `autoboot in 3 seconds`. If it's there,
autoboot is keyed and a random keypress does nothing - you must paste back that exact string
(it was fixed across reboots on one unit) within the 3 s window, then Enter. Only boards without
that line accept a plain keypress. Once at the `=>` prompt, boot to a password-less shell:

```
setenv bootargs 'console=ttySAK0,115200n8 root=/dev/mtdblock2 rootfstype=squashfs rw init=/bin/sh'
boot
```

Then mount the config partition (jffs2) to set the password and edit the boot script:

```
mount -t jffs2 /dev/mtdblock4 /etc
```

Full detail in [INSTALL.md §4](INSTALL.md).

---
*Produced with AI assistance.*
