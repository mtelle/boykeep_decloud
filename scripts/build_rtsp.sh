#!/bin/bash
# build_rtsp.sh - build the RTSP server (vendor SDK "rtsp" sample + our patches) against the libs of YOUR dump.
# Needs : $SDK (AnyCloud39AV130 SDK tree: platform/include, platform/lib/librtsp_server.so, platform/sample/rtsp)
#         $WORK/vendor/app/lib (from firmware/extract_dump.sh)
# Output: $WORK/out/rtspd, $WORK/out/librtsp_server.so      DBG=1 adds frame traces (PUSH/GETV)
set -e
REPO=${REPO:-$(cd "$(dirname "$0")/.." && pwd)}
WORK=${WORK:-/work}
SDK=${SDK:-$WORK/sdk/ANYKA_130L_SDK}
LIBS_DIR=$WORK/vendor/app/vicam
XT=${XT:-/opt/xt/x-tools/arm-unknown-linux-uclibcgnueabi/bin}
GCC=$XT/arm-unknown-linux-uclibcgnueabi-gcc
OUT=$WORK/out; mkdir -p $OUT
B=/tmp/rtsp_build; rm -rf $B && mkdir -p $B/lib && cd $B

cp $SDK/platform/sample/rtsp/*.c $SDK/platform/sample/rtsp/*.h .     # never patch the SDK in place
cp $SDK/platform/lib/librtsp_server.so lib/
cp $SDK/platform/lib/libmpi_aenc.so     lib/  # AAC encode API (ak_aenc_*): not on the stock camera
cp $SDK/platform/lib/libakaac.so        lib/  # AAC software encoder (AENC_OpenAac): not on the stock camera
cp $SDK/platform/lib/libakaudiocodec.so lib/  # codec core (AENC_Open/_SD_*/bitstream_*): libakaac NEEDs it
# NOTE: we do NOT swap libplat_common - the camera's copy (ai_mux/ao_mux/vqe_mux) and the SDK's
# (tAencMux) diverged. We keep the camera's and define tAencMux ourselves (patch_audio.py).
python3 $REPO/rtsp/patch_enc_type.py ak_rtsp_stream.c        # enc_type override in ini (keeps the AAC codec registration)
python3 $REPO/rtsp/patch_ip.py ak_rtsp_server_sample.c       # announce the wlan0 IP instead of a hard-coded one
python3 $REPO/rtsp/patch_rtsp_auth.py ak_rtsp_server_sample.c  # Digest auth from RTSP_USER/RTSP_PASS env (off if unset)
python3 $REPO/rtsp/patch_audio.py ak_rtsp_stream.c           # ENABLE mic: live AAC track + tee into the mp4 recorder
python3 $REPO/rtsp/patch_snapshot.py ak_rtsp_stream.c        # JPEG snapshot thread (1 fps -> /tmp/www/snapshot.jpg)
python3 $REPO/rtsp/patch_daynight.py ak_rtsp_stream.c        # automatic day/night (IR-cut, IR LEDs, ISP profile)
python3 $REPO/rtsp/patch_motion.py ak_rtsp_stream.c          # HW motion detect + event recording (video+AAC) to SD
python3 $REPO/rtsp/patch_osd.py ak_rtsp_stream.c             # burned-in date/time overlay (ISP OSD, both streams)
[ "${DBG:-0}" = 1 ] && python3 $REPO/rtsp/patch_debug.py ak_rtsp_stream.c ak_rtsp_server_sample.c

for f in ak_rtsp_server_sample.c ak_rtsp_stream.c aas_ini_parser.c; do
    $GCC -march=armv5te -mtune=arm926ej-s -Os -std=gnu99 -D_GNU_SOURCE -D__CHIP_AK3918EV300L_SERIES -I. -I$SDK/platform/include -c $f -o ${f%.c}.o
done
L="-lrtsp_server -lplat_common -lplat_vi -lmpi_venc -lmpi_aenc -lakaac -lakaudiocodec -lplat_ai -lplat_ao -lplat_vqe -lplat_vpss -lplat_isp_sdk -lplat_mem -lplat_osal -lplat_thread -lplat_log -lplat_dbg -lplat_timer -lakaudiofilter -lapp_video -lmpi_osd -lapp_osd_ex -lapp_its"
# $LIBS_DIR first (camera libs, incl. its libplat_common with ai/ao/vqe_mux); -rpath-link,lib lets
# ld resolve libakaac's NEEDED libakaudiocodec. tAencMux is provided by our own object (patch_audio).
$GCC -march=armv5te -o rtspd *.o -L$LIBS_DIR -Llib -Wl,-rpath-link,lib -Wl,--start-group $L -Wl,--end-group -lpthread -ldl -lm -lrt -Wl,-rpath,/opt/vicam
cp rtspd lib/librtsp_server.so lib/libmpi_aenc.so lib/libakaac.so lib/libakaudiocodec.so $OUT/   # ship the audio libs too
md5sum $OUT/rtspd
echo BUILD_RTSP_OK
