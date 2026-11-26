#!/bin/bash
# build_httpd.sh - cross-compile a standalone static BusyBox with the "httpd" applet for the
# camera. The stock busybox on this board was built without CONFIG_HTTPD, and there is no other
# HTTP server anywhere in the firmware, so this is what serves the web UI/PTZ/CGI/config pages.
#
# Why BusyBox's own httpd: it already has everything the project needs by default -
# FEATURE_HTTPD_CGI (runs cgi-bin/*.cgi) and FEATURE_HTTPD_RANGES (serves "Range:" requests,
# needed for seeking inside recorded .mp4 clips in the browser) - both on by default, confirmed
# against networking/httpd.c's //config: comments in busybox 1.36.1.
#
# IMPORTANT: the output MUST be named literally "busybox", not anything else. BusyBox's
# multi-call dispatcher only falls back to "argv[1] is the applet name" when the executable's
# own basename is exactly "busybox" (hardcoded check in libbb/appletlib.c) - any other name
# (tested: "bb_web") makes EVERY applet, even trivial ones like "echo", fail with
# "applet not found", since there is then no argv[0]->applet match and no special-cased
# fallback either. Confirmed empirically under qemu-arm-static before this script settled on
# shipping the binary as "busybox". It never collides with the system's own /bin/busybox
# because it is always invoked by its full path (e.g. $CAM/bin/busybox httpd ...), never via
# $PATH lookup.
#
# Needs : internet access (to fetch the BusyBox tarball, once) and the ak39-build cross
#         toolchain container (arm-unknown-linux-uclibcgnueabi-gcc).
# Output: $WORK/out/busybox (static ARM binary, ship to cam/bin/busybox on the camera)
set -e
REPO=${REPO:-$(cd "$(dirname "$0")/.." && pwd)}
WORK=${WORK:-/work}
BB_VER=${BB_VER:-1.36.1}
BB_SRC_DIR=$WORK/busybox-src
BB_SRC=$BB_SRC_DIR/busybox-$BB_VER
XT=${XT:-/opt/xt/x-tools/arm-unknown-linux-uclibcgnueabi/bin}
CROSS=$XT/arm-unknown-linux-uclibcgnueabi-
OUT=$WORK/out; mkdir -p $OUT

if [ ! -d "$BB_SRC" ]; then
    mkdir -p "$BB_SRC_DIR"
    cd "$BB_SRC_DIR"
    [ -f busybox-$BB_VER.tar.bz2 ] || wget -q "https://busybox.net/downloads/busybox-$BB_VER.tar.bz2"
    tar xjf busybox-$BB_VER.tar.bz2
fi

B=/tmp/bb_build; rm -rf $B && cp -r "$BB_SRC" $B && cd $B
# /onvif/* as CGI, and no HTTP Basic auth there (ONVIF authenticates itself) - see docs/13-auth.md
python3 "$REPO/src/tools/patch_busybox_httpd.py" networking/httpd.c

make ARCH=arm CROSS_COMPILE=$CROSS distclean >/dev/null 2>&1 || true
make ARCH=arm CROSS_COMPILE=$CROSS defconfig

# static binary + the same chip tuning flags as rtspd (armv5te, arm926ej-s)
sed -i "s/^# CONFIG_STATIC is not set/CONFIG_STATIC=y/" .config
if grep -q "^CONFIG_EXTRA_CFLAGS=" .config; then
    sed -i "s|^CONFIG_EXTRA_CFLAGS=.*|CONFIG_EXTRA_CFLAGS=\"-march=armv5te -mtune=arm926ej-s\"|" .config
else
    echo 'CONFIG_EXTRA_CFLAGS="-march=armv5te -mtune=arm926ej-s"' >> .config
fi

make ARCH=arm CROSS_COMPILE=$CROSS oldconfig </dev/null
make ARCH=arm CROSS_COMPILE=$CROSS -j"$(nproc)"

cp busybox $OUT/busybox          # MUST stay named exactly "busybox" - see note above
${CROSS}strip $OUT/busybox 2>/dev/null || true
ls -la $OUT/busybox
md5sum $OUT/busybox
echo BUILD_HTTPD_OK
