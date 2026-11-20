# Root access without UART: the SD `factorytest` hook

UART TX never worked reliably on at least one unit (see the caveat in
[docs/01-hardware.md](01-hardware.md)). This page documents the alternative that was actually
used to reach root end to end on that unit: an **unsigned** boot-time hook that the stock `rcS`
already looks for on the SD card, plus a weak (not chip-bound) checksum tool already present in
the firmware.

## 1. Where the hook lives

The stock `/etc/init.d/rcS`, before it ever starts `viCam`, runs this block unconditionally:

```sh
if [ -d /mnt/sdcard/factorytest ]; then
    echo "start factorytest..."
    cd /mnt/sdcard/factorytest
    /opt/sh/safe_exec.sh ./auth.ini ./factorytest.sh && exit 0
fi
```

If `/mnt/sdcard/factorytest/` exists on the SD, `safe_exec.sh` checks it and - if it passes -
runs `factorytest.sh` as **root**, then `rcS` exits (skipping the rest of the normal boot,
including `viCam`). This was clearly meant for factory QA, not for end users, but nothing in the
stock firmware refuses to run it off a plain user-writable FAT32 card.

## 2. What `safe_exec.sh` actually checks

```sh
check_auth(){ ${THISDIR}/../bin/ukey -t "$1" >/dev/null 2>&1; }
check_auth "${AUTH_FILE}" || exit 2      # auth.ini itself must pass `ukey -t`
source "${AUTH_FILE}"                    # gives MD5SUM_FILE, MD5SUM_MD5
# ... plain md5sum checks of test.md5sum and factorytest.sh against each other
md5sum -c "${MD5SUM_FILE}" || exit 6
sh "${SCRIPT_FILE}"
```

So there are two layers:

1. **`ukey -t auth.ini`** - a custom checksum tool, not RSA. This is the only "cryptographic"
   gate on this path.
2. **Plain MD5 chains** - `test.md5sum` lists `factorytest.sh`'s MD5, `auth.ini` records
   `test.md5sum`'s own MD5. These are just hashes, not signatures; anyone can regenerate them for
   content they wrote themselves with plain `md5sum`.

## 3. `ukey` is not chip-bound

`ukey` (`/app/bin/ukey` in the firmware, aka `/opt/sh/../bin/ukey`) needs on `NEEDED`: only
`libc.so.0` - no device file, no `/proc`, no chip-ID syscall, nothing hardware-specific at all.
Disassembly confirms it: the checksum is two CRC-16s over the file's own bytes plus its size,
written back as `KEY=%08x%08d`. It has a documented `-w FILE` mode that **writes** a valid
checksum for whatever file you hand it, and a `-t FILE` mode that verifies it. Since it does not
touch anything chip-specific, it produces an identical result whether run on the camera or under
emulation on a PC - which is the whole reason this is practical offline.

(This is the same tool and the same weak-checksum finding already noted for the SD *firmware
update* channel in [docs/02-firmware-analysis.md](02-firmware-analysis.md#1-sd-firmware-update--weak-checksum).
That page explains why the update channel's lack of real signing is *not* exploited here, as a
matter of choice: a forging tool for arbitrary signed firmware updates would be dual-use. What
*is* used here is different and much narrower - the vendor's own `-w` option, run against a
factory-test script you wrote yourself, never touching the update/signing path at all.)

## 4. Generating a valid `auth.ini` offline

With `qemu-user-static` and the rootfs dump as a sysroot, `ukey` runs identically to how it would
on the camera:

```bash
sudo apt-get install -y qemu-user-static      # once
Q=qemu-arm-static
SR=/path/to/vendor/rootfs                     # from firmware/extract_dump.sh
U=$SR/app/bin/ukey

mkdir -p factorytest && cd factorytest
# factorytest.sh: your own script - see §6 for the indirection pattern used here
md5sum factorytest.sh > test.md5sum
MD5MD5=$(md5sum test.md5sum | cut -d' ' -f1)
printf 'KEY=0000000000000000\nMD5SUM_FILE=test.md5sum\nMD5SUM_MD5=%s\n' "$MD5MD5" > auth.ini

$Q -L "$SR" "$U" -w auth.ini     # writes a valid KEY= line in place
$Q -L "$SR" "$U" -t auth.ini     # must print "file not changed." (exit 0)
```

Copy `factorytest/` (this folder) plus a `wifi.txt` (see
[deploy/factorytest/wifi.txt.example](../deploy/factorytest/wifi.txt.example)) to the SD card's
root and power on. No UART, no flashing, nothing signed with a real key.

> Non-destructive by design: this only ever touches the FAT32 SD card. Pull the card and the
> camera boots 100 % stock again.

## 5. First boot: recon only

Before trusting this with anything that matters, the very first `factorytest.sh` run should do
nothing but prove it executes as root and dump useful facts - available tools (`telnetd`?
`wpa_supplicant`? `nc`?), writable paths, the stock WiFi scripts, interfaces - to a log file on
the SD (so it survives even if the script never gets network access). Only once that confirms a
safe, informative baseline should the hook be extended to bring up WiFi, open a shell, and launch
the real de-cloud stack.

## 6. Sign once, edit forever: the wrapper indirection

`check_auth` only verifies `factorytest.sh`'s own hash - **not** anything that file calls. So the
shipped `factorytest.sh` is a permanently-stable one-liner, signed exactly once:

```sh
#!/bin/sh
sh /mnt/sdcard/factorytest/run.sh
```

All the real logic - WiFi bring-up, SSH, launching `scripts/start.sh` - lives in
[`deploy/factorytest/run.sh`](../deploy/factorytest/run.sh), which is **plain text with no
signature at all**. Change it, save it, reboot - no `ukey` round-trip ever needed again. The
signed artifacts ([`factorytest.sh`](../deploy/factorytest/factorytest.sh),
[`auth.ini`](../deploy/factorytest/auth.ini), [`test.md5sum`](../deploy/factorytest/test.md5sum))
are committed as-is; only `run.sh` and `wifi.txt` are meant to be edited per deployment.

## 7. Gotchas met along the way

- **This busybox has no `tr`** (nor `head`, `httpd`, or `basename`). `run.sh` parses `wifi.txt`
  with a pure `while IFS= read -r line` loop and strips a trailing `\r` by hand - no `tr` needed.
- **`nc` here is client-only** (`nc IPADDR PORT`, no `-l`). File transfer to/from the camera over
  the network (before SSH was set up) used a one-shot TCP listener on the PC side (PowerShell's
  `TcpListener`, or any `nc -l`) with the camera's `nc` as the client in the other direction.
- **Never call `/opt/vicam/dotstart.sh`**, not even with `stop`. It unconditionally backgrounds a
  `sdcard_check()` loop that polls `netstat -a | grep -c 9008` (a **substring** match, not an
  exact port check) and, once satisfied, runs `umount /mnt/sdcard -l` - with enough concurrent
  network activity (telnet, `nc`, SSH, RTSP clients), some ephemeral port eventually contains
  "9008" as a substring (e.g. `49008`) and the SD gets yanked out from under a live session.
  `scripts/start.sh` stops `viCam` directly with `killall -9` instead.
- **A hard power-cut loses unsynced writes.** Files edited over a live telnet/SSH session and
  tested immediately, then followed by pulling power rather than a clean `reboot`, can revert to
  whatever was last flushed to the FAT32 card - the kernel's own periodic write-back usually
  catches up within tens of seconds, but don't rely on it. Call `sync` (and wait a few seconds)
  after editing anything you want to survive a reboot.

## 8. SSH and file transfer

Once a root shell exists (either path), `scripts/start.sh` sets a password-less SSH up via
`dropbearmulti`. Set a password first (`passwd`) so `dropbear` will actually authenticate you.

- `dropbearmulti` has a working built-in `scp` applet - but **no** working `sftp` (it execs an
  external `sftp-server` at a hardcoded path that is not present, and never was in any dropbear
  version - dropbear has never shipped its own SFTP server, it always relies on OpenSSH's;
  cross-compiling a full OpenSSH for this target was judged not worth it for a feature `scp`
  already covers). Modern OpenSSH clients default `scp` to the SFTP protocol; force the legacy
  one: `scp -O file root@IP:/path`.
- **`scp` did not actually work at first**, and the fix needed two rounds. `cam/bin` lives on the
  FAT32 SD (no symlink support), so the `scp` applet can't be exposed there directly - but the
  real blocker turned out to be that dropbear hardcodes the session `PATH` **at compile time**
  and overwrites it unconditionally on every connection
  (`addnewvar("PATH", DEFAULT_PATH)` / `DEFAULT_ROOT_PATH` for root, in `svr-chansession.c`) -
  exporting `PATH` before launching the daemon has **no effect at all** on incoming sessions.
  Worse, there are **two separate constants**: `DEFAULT_PATH` for normal users and a *different*
  `DEFAULT_ROOT_PATH` for root - patching only the first one silently does nothing when logging
  in as root (the only account here). Fix: [`scripts/build_dropbear.sh`](../scripts/build_dropbear.sh)
  cross-compiles a custom static `dropbearmulti` with *both* constants extended to include
  `/etc/decloud-bin` (a real jffs2 directory `start.sh` populates with an `scp` symlink to
  `cam/bin/dropbearmulti`). Verified end to end: `scp -O file root@IP:/path` now actually
  transfers the file.
- A red herring along the way: checking `$PATH` from an **interactive** login shell (plain
  `ssh user@host`, no command) still shows the *old* fixed path, because `/etc/profile` on this
  firmware unconditionally re-exports `PATH` on login, stepping on whatever dropbear just set.
  That file is only sourced for login shells though - `scp`'s underlying `sh -c "scp -t ..."`
  exec is not one, so it never sees that override. Don't trust `echo $PATH` from an interactive
  session as a test for this - test the actual `scp` transfer instead.
