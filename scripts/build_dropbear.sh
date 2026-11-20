#!/bin/bash
# build_dropbear.sh - cross-compile a custom static dropbearmulti (dropbear+dbclient+
# dropbearkey+scp) with a DEFAULT_PATH that actually includes this project's own bin
# directory, so an incoming "scp" request can find it.
#
# Why: dropbear hardcodes the session PATH at COMPILE TIME (default_options.h's
# DEFAULT_PATH, set via addnewvar("PATH", DEFAULT_PATH) on every new session -
# src/svr-chansession.c) - exporting PATH before launching the daemon has NO effect on
# incoming sessions, dropbear overwrites it unconditionally. The stock dropbearmulti
# shipped with this firmware was built with DEFAULT_PATH="/system/bin:/bin:/sbin:/usr/bin:
# /usr/sbin" - none of which are writable on this board (squashfs), so there was nowhere
# to put an "scp" symlink that an incoming session would actually find. Fix: rebuild
# dropbear with our own directory appended to DEFAULT_PATH.
#
# Needs : internet access (fetch the dropbear tarball, once) and the ak39-build cross
#         toolchain container.
# Output: $WORK/out/dropbearmulti (static ARM binary, replaces cam/bin/dropbearmulti)
set -e
REPO=${REPO:-$(cd "$(dirname "$0")/.." && pwd)}
WORK=${WORK:-/work}
DB_VER=${DB_VER:-2024.86}
DB_SRC_DIR=$WORK/dropbear-src
DB_SRC=$DB_SRC_DIR/dropbear-$DB_VER
XT=${XT:-/opt/xt/x-tools/arm-unknown-linux-uclibcgnueabi/bin}
CROSS=$XT/arm-unknown-linux-uclibcgnueabi-
OUT=$WORK/out; mkdir -p $OUT

# the extra directory an incoming SSH/SCP session's PATH should include - a real (jffs2)
# filesystem the camera's start.sh populates at boot (cam/bin lives on the FAT32 SD, which
# has no symlink support, so "scp" cannot be exposed there directly)
DECLOUD_BIN=${DECLOUD_BIN:-/etc/decloud-bin}

if [ ! -d "$DB_SRC" ]; then
    mkdir -p "$DB_SRC_DIR"
    cd "$DB_SRC_DIR"
    [ -f dropbear-$DB_VER.tar.bz2 ] || wget -q "https://matt.ucc.asn.au/dropbear/releases/dropbear-$DB_VER.tar.bz2"
    tar xjf dropbear-$DB_VER.tar.bz2
fi

B=/tmp/db_build; rm -rf $B && cp -r "$DB_SRC" $B && cd $B

# dropbear has TWO separate compiled-in PATH constants: DEFAULT_PATH for normal users and
# DEFAULT_ROOT_PATH specifically for root (addnewvar("PATH", DEFAULT_ROOT_PATH) in
# svr-chansession.c when pw_uid==0) - logging in as root (the only account on this camera)
# uses the ROOT one, so both must be patched or the fix silently does nothing for root logins.
sed -i "s|^#define DEFAULT_PATH .*|#define DEFAULT_PATH \"/usr/bin:/bin:/sbin:/usr/sbin:$DECLOUD_BIN\"|" src/default_options.h
sed -i "s|^#define DEFAULT_ROOT_PATH .*|#define DEFAULT_ROOT_PATH \"/usr/sbin:/usr/bin:/sbin:/bin:$DECLOUD_BIN\"|" src/default_options.h
grep "^#define DEFAULT_PATH\|^#define DEFAULT_ROOT_PATH" src/default_options.h

CC=${CROSS}gcc \
CFLAGS="-march=armv5te -mtune=arm926ej-s -Os" \
./configure --host=arm-unknown-linux-uclibcgnueabi --enable-static --disable-zlib --disable-harden

make clean >/dev/null 2>&1 || true
make PROGRAMS="dropbear dbclient dropbearkey scp" MULTI=1 -j"$(nproc)"

cp dropbearmulti $OUT/dropbearmulti
${CROSS}strip $OUT/dropbearmulti 2>/dev/null || true
ls -la $OUT/dropbearmulti
md5sum $OUT/dropbearmulti
echo BUILD_DROPBEAR_OK
