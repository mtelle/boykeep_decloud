# Firmware analysis

Summary of the evidence found by examining the firmware package, the rootfs (squashfs) and the
device's own binaries/libraries. The goal was to understand **how the firmware validates updates**
and **what can be enabled/stopped** for a clean de-cloud.

> Methodology: reconstruct the flash image from the update package, extract the squashfs
> (PySquashfsImage), and inspect symbols and strings (`nm -D`, `readelf -d`, `strings`) plus
> targeted disassembly (capstone) of the verification binaries. All on owned hardware.

## Partition map (8 MiB NOR)

| mtd | Offset | Content |
|-----|--------|---------|
| mtd0 | 0x000000 | u-boot |
| mtd1 | 0x040000 | kernel (uImage) |
| mtd2 | 0x220000 | rootfs (squashfs, xz) |
| mtd3 | 0x4E0000 | user / app (squashfs) |
| mtd4 | 0x730000 | config (jffs2 → `/etc`) |
| mtd5 | 0x7F0000 | factory |

> Note: this map is **specific to this board**. Other Anyka boards from the same community project
> place `app` at `mtd4`; that is why the extraction script carries its own offsets.

## Update verification (findings)

The firmware uses **two different integrity mechanisms**; do not conflate them:

### 1. SD update "signature" `ukey` — **weak, keyless**

The SD update-file verifier (`ukey`) uses **no keyed cryptography**: it is an integrity check based
on **two CRC-16s** (over two halves of the file) plus the file size, formatted `KEY=%08x%08d`.
With no secret, it is **forgeable**. The update container is additionally wrapped with a
**symmetric** scheme (`ambulance`, AES-128-CBC) whose **key is embedded in the binary** (derived
with an `EVP_BytesToKey`-style KDF).

**Implication:** the SD update channel provides **no authenticity guarantee**. This repo does
not ship a standalone forging tool and does not need to: `ukey` itself, already present in the
stock firmware dump, has a documented `-w FILE` mode that computes and writes a valid checksum
for any file you point it at. Running the vendor's own tool (under `qemu-arm-static`, offline,
against content you fully control) against an **unsigned factory-test hook** in `rcS` - not
against the signed update path, and not against the camera itself - turned out to be a fully
practical way to reach root, used successfully on real hardware where UART did not cooperate.
See [docs/09-root-access.md](09-root-access.md) for the mechanism and exact steps. The *finding*
about the update channel's weak signature stands regardless (it is documented, not exploited);
what changed is that the unsigned `factorytest` hook turned out to be a real, working path to
root on its own, independent of that finding.

### 2. App / cloud — **real RSA**

The `.sign` files (256 bytes) tied to the app and the cloud path **are** **RSA** signatures,
verified with an embedded public key (`public_key.der`). This path is **not** forgeable and is
independent of the one above. The de-cloud **does not touch it**: instead of signing anything, the
custom stack is launched from the SD and the app is disabled.

### Operational conclusion

No signature is forced. Root is reached either via **UART → root shell**, or - confirmed working
when UART TX is not reliable - via the **unsigned SD `factorytest` hook** (see
[docs/09-root-access.md](09-root-access.md)). Either way, de-cloud itself is just: **launch the
custom stack from the SD**, which is reproducible and reversible, and the camera is **isolated**
on the router.

## What loads and what is stopped

- The stock boot (`/opt/autoboot.sh` → `viCam`) is what talks to the cloud; it is **commented out**
  in `/etc/init.d/rcS` and `start.sh` is launched instead.
- The stock system already loads the **kernel modules, the GC2083 sensor and WiFi** before
  `viCam`, so the custom stack reuses them (no need to reload sensor/ISP).
- Cloud watchdog: `touch /etc/closewdog` and `killall wdog watchdog.sh viCam`.
- Auto-update: comment out the `sdupdate_fwenc.sh` and `check_auth.sh … fwup_autorun.sh` lines, and
  blank `/etc/conf/upgrade.url`.

## Tools used

- `PySquashfsImage` — extract rootfs/app (squashfs xz).
- `nm -D` / `readelf -d` / `strings` — symbols and dependencies of `.so` files and binaries.
- `capstone` — targeted disassembly (ARMv5TE) of the verifiers.

---
*Produced with AI assistance; findings verified on the device.*
