import sys
# patch_osd.py - burn a date/time stamp into the video via the ISP OSD (ak_osd_*).
#
# FIX (2026-11-19), round 2, two separate bugs found on real hardware:
#
# 1) Not visible in recordings. The ak_osd_*.h header is explicit that channel_id is a
#    PER-CHANNEL target ("channel_id: channel number" on every call, create_canvas/
#    draw_bitmap/set_region_to_vi all take it) - it is NOT a single "apply to VI, affects
#    both encoders" knob as originally assumed. channel 0 is the live/main stream; channel 1
#    is what motion recording actually encodes. Fix: create a canvas and draw/set the region
#    on BOTH channel 0 and channel 1 every tick.
#
# 2) Garbled on screen (confirmed visible, but not legible). The bit-packing direction within
#    each byte of the 1bpp bitmap was a guess (MSB-first: X=0 -> bit 7) with no way to verify
#    off the device. Flipped to LSB-first (X=0 -> bit 0) as the next thing to try if MSB-first
#    produced garbage. If this is STILL wrong, the next things to try, in order: swap fg/bg
#    (0 vs 1), try bit_width=4 or 2 instead of 1 (osd_attr.bit_width), or the color_table may
#    need to be non-NULL (a real palette) instead of "use the driver default".
#
# Still defensive: everything runs in its own thread; ak_osd_init/create_canvas failure logs
# to /tmp/osd.log and exits without touching video. OSD=0 disables entirely.
#
# Env: OSD=1 enable. OSD_X/OSD_Y position (px). OSD_SCALE pixel scale (default 2).
#      OSD_FG foreground color index (default 1), OSD_BG bg index (default 0), OSD_ALPHA 0..100.
p = sys.argv[1]
s = open(p, encoding='latin-1').read()

proto_anchor = '#include "aas_ini_parser.h"\n'
assert s.count(proto_anchor) >= 1, ("osd proto", s.count(proto_anchor))
s = s.replace(proto_anchor, proto_anchor + 'void osd_start(void);\n', 1)

func = r'''
/* ===== burned-in date/time OSD via the ISP (anyka-ak3918ev300l-decloud) ===== */
#include "ak_osd.h"
#include <time.h>

#define OSD_NCHAN 2   /* 0 = live/main stream, 1 = recording/sub stream - both need the region */

/* 5x7 bitmap font: glyphs 0-9, '-', ':', ' ' (low 5 bits per row, bit4 = leftmost pixel) */
static const unsigned char OSD_FONT[13][7] = {
    {0x0E,0x11,0x13,0x15,0x19,0x11,0x0E}, /* 0 */
    {0x04,0x0C,0x04,0x04,0x04,0x04,0x0E}, /* 1 */
    {0x0E,0x11,0x01,0x02,0x04,0x08,0x1F}, /* 2 */
    {0x1F,0x02,0x04,0x02,0x01,0x11,0x0E}, /* 3 */
    {0x02,0x06,0x0A,0x12,0x1F,0x02,0x02}, /* 4 */
    {0x1F,0x10,0x1E,0x01,0x01,0x11,0x0E}, /* 5 */
    {0x06,0x08,0x10,0x1E,0x11,0x11,0x0E}, /* 6 */
    {0x1F,0x01,0x02,0x04,0x08,0x08,0x08}, /* 7 */
    {0x0E,0x11,0x11,0x0E,0x11,0x11,0x0E}, /* 8 */
    {0x0E,0x11,0x11,0x0F,0x01,0x02,0x0C}, /* 9 */
    {0x00,0x00,0x00,0x1F,0x00,0x00,0x00}, /* - */
    {0x00,0x04,0x04,0x00,0x04,0x04,0x00}, /* : */
    {0x00,0x00,0x00,0x00,0x00,0x00,0x00}, /* space */
};
static int osd_glyph(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c == '-') return 10;
    if (c == ':') return 11;
    return 12; /* space */
}

static void osd_log(const char *m)
{
    FILE *f = fopen("/tmp/osd.log", "a");
    if (f) { fprintf(f, "%s\n", m); fclose(f); }
}

static void *osd_th(void *arg)
{
    (void)arg;
    ak_thread_set_name("osd_th");
    ak_sleep_ms(6000);                       /* let VI/VENC settle first */

    int scale = getenv("OSD_SCALE") ? atoi(getenv("OSD_SCALE")) : 2;
    if (scale < 1) scale = 1; if (scale > 4) scale = 4;
    int pos_x = getenv("OSD_X") ? atoi(getenv("OSD_X")) : 16;
    int pos_y = getenv("OSD_Y") ? atoi(getenv("OSD_Y")) : 16;
    int fg    = getenv("OSD_FG") ? atoi(getenv("OSD_FG")) : 1;
    int bg    = getenv("OSD_BG") ? atoi(getenv("OSD_BG")) : 0;
    int alpha = getenv("OSD_ALPHA") ? atoi(getenv("OSD_ALPHA")) : 70;

    const int NCH = 19;                       /* "YYYY-MM-DD HH:MM:SS" */
    int gw = 6 * scale;                        /* 5px glyph + 1px gap */
    int W = NCH * gw;
    int H = 7 * scale + 2;
    int stride = (W + 7) / 8;                  /* 1 bpp, byte-aligned rows */
    int bufsz = stride * H;

    struct osd_attr at;
    memset(&at, 0, sizeof(at));
    at.color_mode_index = AK_OSD_COLOR_FMT_YUV;
    at.color_table = NULL;                     /* use the driver default color table */
    at.region_num  = 1;
    at.color_type  = AK_OSD_TYPE_MONO;
    at.bit_width   = 1;
    if (ak_osd_init(&at) != 0) { osd_log("ak_osd_init failed -> OSD off"); return NULL; }

    int chn;
    for (chn = 0; chn < OSD_NCHAN; chn++) {
        struct osd_canvas_info cv;
        memset(&cv, 0, sizeof(cv));
        cv.channel_id = chn;
        cv.region_id  = 0;
        cv.pos_in_yuv.x = pos_x;
        cv.pos_in_yuv.y = pos_y;
        cv.region_size_width  = W;
        cv.region_size_height = H;
        cv.canvas_color_index = bg;
        cv.alpha = alpha;
        if (ak_osd_create_canvas(&cv) != 0) {
            char m[64]; snprintf(m, sizeof(m), "ak_osd_create_canvas chn%d failed -> OSD off", chn);
            osd_log(m); return NULL;
        }
    }

    unsigned char *bmp = ak_mem_alloc(MODULE_ID_APP, bufsz);
    if (!bmp) { osd_log("osd bmp alloc failed"); return NULL; }
    osd_log("OSD armed (chn 0 + 1)");

    char last[24] = "";
    for (;;) {
        time_t tt = time(NULL); struct tm tmv; localtime_r(&tt, &tmv);
        char str[24];
        strftime(str, sizeof(str), "%Y-%m-%d %H:%M:%S", &tmv);
        if (strcmp(str, last) != 0) {
            memcpy(last, str, sizeof(last));
            memset(bmp, (bg ? 0xFF : 0x00), bufsz);     /* clear to background */
            int c;
            for (c = 0; c < NCH && str[c]; c++) {
                const unsigned char *g = OSD_FONT[osd_glyph(str[c])];
                int col0 = c * gw;
                int row;
                for (row = 0; row < 7; row++) {
                    int bitrow = g[row];
                    int px;
                    for (px = 0; px < 5; px++) {
                        if (!((bitrow >> (4 - px)) & 1)) continue;
                        int sy, sx;
                        for (sy = 0; sy < scale; sy++)
                            for (sx = 0; sx < scale; sx++) {
                                int X = col0 + px*scale + sx;
                                int Y = 1 + row*scale + sy;
                                if (X < 0 || X >= W || Y < 0 || Y >= H) continue;
                                int byte = Y*stride + (X >> 3);
                                int bit  = X & 7;             /* LSB-first (was 7-(X&7): MSB-first) */
                                if (fg) bmp[byte] |=  (1 << bit);
                                else    bmp[byte] &= ~(1 << bit);
                            }
                    }
                }
            }
            for (chn = 0; chn < OSD_NCHAN; chn++) {
                struct osd_drawing_info dw;
                memset(&dw, 0, sizeof(dw));
                dw.channel_id = chn;
                dw.region_id  = 0;
                dw.buffer = bmp;
                dw.buffer_size = bufsz;
                dw.bitmap_size_width  = W;
                dw.bitmap_size_height = H;
                dw.pos_in_region.x = 0;
                dw.pos_in_region.y = 0;
                ak_osd_clean_canvas(chn, 0);
                ak_osd_draw_bitmap_to_canvas(&dw);
                ak_osd_set_region_to_vi(chn, 0);
            }
        }
        ak_sleep_ms(500);
    }
    return NULL;
}

void osd_start(void)
{
    const char *e = getenv("OSD");
    if (e && atoi(e) == 0) return;             /* OSD=0 disables */
    ak_pthread_t tid;
    ak_thread_create(&tid, osd_th, NULL, ANYKA_THREAD_MIN_STACK_SIZE, 20);
}
'''

anchor_main = 'int main(int argc, char **argv)'
assert s.count(anchor_main) == 1, ("osd main", s.count(anchor_main))
s = s.replace(anchor_main, func + '\n' + anchor_main, 1)

anchor_start = '    signal (SIGPIPE, SIG_IGN);\n'
assert s.count(anchor_start) == 1, ("osd sigpipe", s.count(anchor_start))
s = s.replace(anchor_start, anchor_start + '    osd_start();\n', 1)

open(p, 'w', encoding='latin-1').write(s)
print("patch osd (burned timestamp, dual-channel, LSB-first bits) OK")
