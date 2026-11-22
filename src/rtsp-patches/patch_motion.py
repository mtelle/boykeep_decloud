import sys
# patch_motion.py - HW motion detection + event recording (native MP4, browser-friendly H.264)
#                   + AAC AUDIO track (mic), interleaved A/V, 2-track ISO-BMFF muxer.
#
# Detection: ak_vpss_md_get_stat(VIDEO_DEV0) returns a per-block u16 motion grid (ISP hardware,
#   no enable needed - same source the stock viCam used). By default (MD_MRD=1) the grid goes
#   through libak_mrd - the stock viCam's own region analyzer: it groups active cells into regions
#   and drops small, isolated or flickering ones (day/night parameter sets from the SDK sample,
#   switched with /tmp/daynight.state); motion = at least one region of >= MD_MRD_MIN cells.
#   MD_MRD=0 falls back to the plain count: blocks over MD_CELL_TH, >= MD_MIN_CELLS => motion.
#   A post-motion tail (MD_HOLD s) keeps recording after it stops.
#
# Recording: tees the already-encoded VIDEO frames of ONE stream (MD_CHN) AND the encoded AAC
#   audio frames (md_audio_feed, fed from the rtsp audio thread) into a self-contained .mp4.
#   A small ISO-BMFF muxer is written here (no ffmpeg, no ak_mux): ftyp + a single streamed mdat
#   (video NALs -> 4-byte length-prefixed; audio = raw AAC with the ADTS header stripped) + a moov
#   built at close. Codec aware: H.264 -> avc1/avcC (browsers), H.265 -> hev1/hvcC (VLC/HA). Audio
#   -> mp4a/esds (AAC-LC). Because A/V are interleaved in one mdat, BOTH tracks use one-sample
#   chunks (stco lists every sample offset) so the layout is correct regardless of interleave.
#   Real stts from frame timestamps (video) / 1024 samples per frame (audio) => duration + seeking.
#   Files: MD_DIR/<YYYYMMDD>/mo_<ts>.mp4. Rotation every MD_MAXCLIP s at an I frame.
#   Retention: oldest pruned when free < MD_MINFREE MB, AND (if MD_KEEPDAYS>0) clips older than
#   that many days are deleted.  MD_FMT=raw writes a raw .h264/.h265 (diagnostic, no audio).
# Flags in /tmp: motion.active (while recording), motion.rec (current file), motion.level (score).
# Env: MD=0 disables. MD_FMT(mp4|raw) MD_CHN(0|1) MD_CELL_TH MD_MIN_CELLS MD_HOLD MD_PERIOD(ms)
#      MD_MRD(1) MD_MRD_MIN(6) MD_ROWS(24, grid rows) MD_MRD_BIG/_SMALL (day filters, 12000/6000)
#      MD_MRD_BIG_N/_SMALL_N (night filters, 15000/10000)
#      MD_MAXCLIP(s) MD_MINFREE(MB) MD_KEEPDAYS(0=off) MD_AUDIO(1) MD_DIR MD_HOOK(cmd on motion).
p = sys.argv[1]
s = open(p, encoding='latin-1').read()

proto_anchor = '#include "aas_ini_parser.h"\n'
assert s.count(proto_anchor) == 1, ("proto", s.count(proto_anchor))
s = s.replace(proto_anchor, proto_anchor +
    '\n/* --- motion detection + recording (decloud) --- */\n'
    'struct video_stream;\n'
    'struct audio_stream;\n'
    'void md_record_feed(int dev_id, int chn, int enc_type, int w, int h, struct video_stream *stream);\n'
    'void md_audio_feed(struct audio_stream *as);\n', 1)

func = r'''
/* ===== HW motion detection + native-MP4 event recording (anyka-ak3918ev300l-decloud) ===== */
#include <time.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/statvfs.h>

/* EV300L has no struct vpss_md_info for its chip define; declare the entry point with an opaque
 * buffer. The ISP fills a u16 grid (32 wide per ak_mrd.h MD_SIZE). We zero a generous buffer and
 * sum it, so the score is correct whatever the real height is, and never overflows. */
int ak_vpss_md_get_stat(int dev, void *md);

#define MD_W 32
#define MD_H 48
#ifndef VIDEO_DEV0
#define VIDEO_DEV0 0
#endif
#define MD_MAXSAMP 18000

/* libak_mrd (/opt/vicam/libak_mrd.so, the camera's own copy). Prototypes from ak_mrd.h. */
typedef struct { int left, top, right, bottom, count; } MD_MRD_RECT;
#define MD_MRD_CAP 20
void *ak_mrd_init(void);
int ak_mrd_set_filters(void *h, unsigned short flt_big, unsigned short flt_small);
int ak_mrd_set_motion_region_size(void *h, int size_min, int size_max);
int ak_mrd_set_mode(void *h, unsigned char mode);
int ak_mrd_set_floating_wadding_params(void *h, int m_frames, int s_frames, int extra_frames,
                                       int dis, int m_width, int m_height);
int ak_mrd(void *h, unsigned short md[][MD_W], int len, MD_MRD_RECT boxes[MD_MRD_CAP]);

static volatile long long g_md_rec_until = 0;    /* monotonic ms: record while now < this */
static volatile int       g_md_level     = 0;

static long long md_now_ms(void)
{
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
static int md_env_i(const char *k, int d)
{
    const char *v = getenv(k);
    return (v && *v) ? atoi(v) : d;
}

/* MP4 is big-endian */
static void be32(FILE *f, unsigned v) { unsigned char b[4] = { v>>24, v>>16, v>>8, v }; fwrite(b,1,4,f); }
static void be16(FILE *f, unsigned v) { unsigned char b[2] = { v>>8, v }; fwrite(b,1,2,f); }
static void tag4(FILE *f, const char *t){ fwrite(t,1,4,f); }
static void mtx (FILE *f){ be32(f,0x10000);be32(f,0);be32(f,0);be32(f,0);be32(f,0x10000);be32(f,0);be32(f,0);be32(f,0);be32(f,0x40000000); }

/* ---------------- recorder state ---------------- */
static ak_mutex_t g_mux_lock;               /* guards the muxer: video (venc thread) + audio thread */
static int   g_mux_init = 0;
static FILE *g_fp = NULL;
static int   g_on = 0;
static int   g_raw = 0;
static int   g_rec_chn = 1;
static int   g_codec = 0;                   /* 0 = H.264 (avc1), 1 = H.265 (hev1) */
static long long g_rec_start = 0;
static char  g_dir[128] = "";

static long g_mdat_pos = 0;
static unsigned g_mdat_bytes = 0;
static char g_path[240] = "";          /* current recording path, for the faststart remux at close */
static int   g_ns = 0;
static unsigned long long g_ts_base = 0;
static int   g_w = 0, g_h = 0;
static unsigned      *g_off = NULL;
static unsigned      *g_sz  = NULL;
static unsigned      *g_ts  = NULL;
static unsigned char *g_key = NULL;
static unsigned char g_vps[320], g_sps[320], g_pps[320];
static int g_vpsn = 0, g_spsn = 0, g_ppsn = 0;

/* audio track state */
static int   g_aud_want = 1;                /* MD_AUDIO: mux audio into the mp4 */
static int   g_a_ns = 0;
static unsigned *g_a_off = NULL;
static unsigned *g_a_sz  = NULL;
static int   g_has_audio = 0;
static int   g_a_rate = 16000;
static int   g_a_chan = 1;
static unsigned char g_asc[8];              /* AudioSpecificConfig */
static int   g_ascn = 0;

static void md_prune(void)
{
    /* 1) time-based retention: delete clips older than MD_KEEPDAYS (0 = disabled) */
    int keepdays = md_env_i("MD_KEEPDAYS", 0);
    if (keepdays > 0) {
        time_t cutoff = time(NULL) - (time_t)keepdays * 86400;
        DIR *d0 = opendir(g_dir);
        if (d0) {
            struct dirent *e0;
            while ((e0 = readdir(d0))) {
                if (e0->d_name[0] == '.') continue;
                char sub[200]; snprintf(sub, sizeof(sub), "%s/%s", g_dir, e0->d_name);
                DIR *d1 = opendir(sub); if (!d1) continue;
                struct dirent *e1; int remain = 0;
                while ((e1 = readdir(d1))) {
                    if (e1->d_name[0] == '.') continue;
                    char fp[320]; snprintf(fp, sizeof(fp), "%s/%s", sub, e1->d_name);
                    struct stat st;
                    if (stat(fp, &st) == 0 && S_ISREG(st.st_mode)) {
                        if (st.st_mtime < cutoff) remove(fp); else remain++;
                    }
                }
                closedir(d1);
                if (remain == 0) rmdir(sub);
            }
            closedir(d0);
        }
    }
    /* 2) space-based retention: delete the oldest until free >= MD_MINFREE MB */
    int minfree = md_env_i("MD_MINFREE", 200);
    struct statvfs vs;
    if (statvfs(g_dir, &vs) != 0) return;
    long long freemb = (long long)vs.f_bavail * vs.f_frsize / (1024*1024);
    int guard = 0;
    while (freemb < minfree && guard++ < 128) {
        char oldest[256] = ""; time_t omt = 0; int found = 0;
        DIR *d0 = opendir(g_dir); if (!d0) break;
        struct dirent *e0;
        while ((e0 = readdir(d0))) {
            if (e0->d_name[0] == '.') continue;
            char sub[200]; snprintf(sub, sizeof(sub), "%s/%s", g_dir, e0->d_name);
            DIR *d1 = opendir(sub); if (!d1) continue;
            struct dirent *e1;
            while ((e1 = readdir(d1))) {
                if (e1->d_name[0] == '.') continue;
                char fp[320]; snprintf(fp, sizeof(fp), "%s/%s", sub, e1->d_name);
                struct stat st;
                if (stat(fp, &st) != 0 || !S_ISREG(st.st_mode)) continue;
                if (!found || st.st_mtime < omt) { omt = st.st_mtime; strncpy(oldest, fp, sizeof(oldest)-1); found = 1; }
            }
            closedir(d1);
        }
        closedir(d0);
        if (!found) break;
        remove(oldest);
        if (statvfs(g_dir, &vs) != 0) break;
        freemb = (long long)vs.f_bavail * vs.f_frsize / (1024*1024);
    }
}

/* rewrite one Annex-B frame as 4-byte length-prefixed NALs into f; capture parameter sets.
 * codec: 0=H.264 (SPS=7,PPS=8), 1=H.265 (VPS=32,SPS=33,PPS=34). returns bytes written. */
static unsigned md_write_nals(FILE *f, const unsigned char *d, unsigned n)
{
    unsigned i = 0, written = 0;
    while (i + 3 <= n) {
        unsigned sc = 0;
        if (d[i]==0 && d[i+1]==0 && d[i+2]==1) sc = 3;
        else if (i+4 <= n && d[i]==0 && d[i+1]==0 && d[i+2]==0 && d[i+3]==1) sc = 4;
        if (!sc) { i++; continue; }
        unsigned start = i + sc, j = start;
        while (j + 3 <= n) {
            if (d[j]==0 && d[j+1]==0 && (d[j+2]==1 || (j+4<=n && d[j+2]==0 && d[j+3]==1))) break;
            j++;
        }
        unsigned end = (j + 3 <= n) ? j : n;
        unsigned len = end - start;
        if (len > 0) {
            if (g_codec == 0) {
                int t = d[start] & 0x1f;                      /* H.264 */
                if      (t==7 && g_spsn==0 && len<sizeof(g_sps)) { memcpy(g_sps,d+start,len); g_spsn=len; }
                else if (t==8 && g_ppsn==0 && len<sizeof(g_pps)) { memcpy(g_pps,d+start,len); g_ppsn=len; }
            } else {
                int t = (d[start] >> 1) & 0x3f;               /* H.265 */
                if      (t==32 && g_vpsn==0 && len<sizeof(g_vps)) { memcpy(g_vps,d+start,len); g_vpsn=len; }
                else if (t==33 && g_spsn==0 && len<sizeof(g_sps)) { memcpy(g_sps,d+start,len); g_spsn=len; }
                else if (t==34 && g_ppsn==0 && len<sizeof(g_pps)) { memcpy(g_pps,d+start,len); g_ppsn=len; }
            }
            be32(f, len); fwrite(d+start, 1, len, f);
            written += 4 + len;
        }
        i = end;
    }
    return written;
}

/* avcC (H.264) decoder config into buf; returns length */
static int md_avcc(unsigned char *buf)
{
    int o = 0;
    buf[o++] = 1;                                   /* configurationVersion */
    buf[o++] = (g_spsn > 1) ? g_sps[1] : 0x42;      /* AVCProfileIndication */
    buf[o++] = (g_spsn > 2) ? g_sps[2] : 0x00;      /* profile_compatibility */
    buf[o++] = (g_spsn > 3) ? g_sps[3] : 0x1f;      /* AVCLevelIndication */
    buf[o++] = 0xFF;                                /* 111111 + lengthSizeMinusOne=3 */
    buf[o++] = 0xE1;                                /* 111 + numOfSPS=1 */
    buf[o++] = g_spsn >> 8; buf[o++] = g_spsn & 0xff;
    memcpy(buf+o, g_sps, g_spsn); o += g_spsn;
    buf[o++] = 1;                                   /* numOfPPS */
    buf[o++] = g_ppsn >> 8; buf[o++] = g_ppsn & 0xff;
    memcpy(buf+o, g_pps, g_ppsn); o += g_ppsn;
    return o;
}
/* hvcC (H.265) decoder config into buf; returns length */
static int md_hvcc(unsigned char *buf)
{
    int o = 0;
    buf[o++] = 1;
    if (g_spsn >= 15) memcpy(buf+o, g_sps+3, 12);               /* PTL, byte-aligned at SPS+3 */
    else { memset(buf+o, 0, 12); buf[o]=0x01; buf[o+11]=93; }
    o += 12;
    buf[o++] = 0xF0; buf[o++] = 0x00; buf[o++] = 0xFC; buf[o++] = 0xFD;
    buf[o++] = 0xF8; buf[o++] = 0xF8; buf[o++] = 0x00; buf[o++] = 0x00; buf[o++] = 0x0B;
    buf[o++] = 3;
    int types[3] = { 32, 33, 34 };
    unsigned char *ps[3] = { g_vps, g_sps, g_pps };
    int pn[3] = { g_vpsn, g_spsn, g_ppsn };
    for (int a = 0; a < 3; a++) {
        buf[o++] = 0x80 | types[a];
        buf[o++] = 0; buf[o++] = 1;
        buf[o++] = pn[a] >> 8; buf[o++] = pn[a] & 0xff;
        if (pn[a] > 0) { memcpy(buf+o, ps[a], pn[a]); o += pn[a]; }
    }
    return o;
}

/* esds box for AAC-LC (39 bytes) using the learned AudioSpecificConfig g_asc (2 bytes) */
static void md_write_esds(FILE *f)
{
    int br = 24000;
    be32(f, 39); tag4(f,"esds"); be32(f,0);            /* version/flags */
    fputc(0x03,f); fputc(25,f);                        /* ES_Descriptor */
    be16(f,0);                                         /* ES_ID */
    fputc(0x00,f);                                     /* flags */
    fputc(0x04,f); fputc(17,f);                        /* DecoderConfigDescriptor */
    fputc(0x40,f);                                     /* objectTypeIndication = Audio ISO/IEC 14496-3 */
    fputc(0x15,f);                                     /* streamType=audio, upStream=0, reserved=1 */
    fputc(0,f); fputc(0,f); fputc(0,f);                /* bufferSizeDB */
    be32(f, br);                                       /* maxBitrate */
    be32(f, br);                                       /* avgBitrate */
    fputc(0x05,f); fputc(2,f);                         /* DecoderSpecificInfo, len 2 */
    fwrite(g_asc, 1, 2, f);
    fputc(0x06,f); fputc(1,f); fputc(0x02,f);          /* SLConfigDescriptor */
}

static void md_open(int enc_type)
{
    md_prune();
    g_codec = (enc_type == HEVC_ENC_TYPE) ? 1 : 0;
    time_t tt = time(NULL); struct tm tmv; localtime_r(&tt, &tmv);
    char day[16], ts[24], dir[160], path[240];
    strftime(day, sizeof(day), "%Y%m%d", &tmv);
    strftime(ts,  sizeof(ts),  "%Y%m%d_%H%M%S", &tmv);
    mkdir(g_dir, 0777);
    snprintf(dir, sizeof(dir), "%s/%s", g_dir, day); mkdir(dir, 0777);
    const char *ext = g_raw ? (g_codec ? "h265" : "h264") : "mp4";
    snprintf(path, sizeof(path), "%s/mo_%s.%s", dir, ts, ext);
    snprintf(g_path, sizeof(g_path), "%s", path);
    g_fp = fopen(path, "wb");
    if (!g_fp) return;

    g_ns = 0; g_mdat_bytes = 0; g_ts_base = 0; g_vpsn = g_spsn = g_ppsn = 0; g_w = g_h = 0;
    g_a_ns = 0; g_has_audio = 0; g_ascn = 0;
    g_on = 1; g_rec_start = md_now_ms();
    if (!g_raw) {
        be32(g_fp, 24); tag4(g_fp, "ftyp"); tag4(g_fp, "isom"); be32(g_fp, 0x200);
        tag4(g_fp, "isom"); tag4(g_fp, "mp41");
        g_mdat_pos = ftell(g_fp);
        be32(g_fp, 0); tag4(g_fp, "mdat");
    }
    printf("MOTION: REC start %s\n", path);
    FILE *r = fopen("/tmp/motion.rec", "w"); if (r) { fputs(path, r); fputc('\n', r); fclose(r); }
}

static void md_finalize_mp4(void)
{
    if (g_ns < 1 || g_spsn == 0) return;
    int ts_scale = 1000;

    static unsigned dur[MD_MAXSAMP];
    int i;
    for (i = 0; i < g_ns; i++) {
        int d = (i < g_ns-1) ? (int)(g_ts[i+1] - g_ts[i]) : (i>0 ? (int)dur[i-1] : 50);
        if (d < 1 || d > 2000) d = (i>0 ? (int)dur[i-1] : 50);
        dur[i] = d;
    }
    unsigned total = 0; for (i = 0; i < g_ns; i++) total += dur[i];
    int nkey = 0; for (i = 0; i < g_ns; i++) if (g_key[i]) nkey++;
    int nrle = 0; for (i = 0; i < g_ns; ) { int j=i+1; while (j<g_ns && dur[j]==dur[i]) j++; nrle++; i=j; }

    int have_aud = (g_has_audio && g_a_ns > 0 && g_ascn == 2);
    int arate = g_a_rate ? g_a_rate : 16000;
    unsigned a_mdur = (unsigned)g_a_ns * 1024;                     /* audio media duration (timescale=arate) */
    unsigned a_total_ms = (unsigned)((unsigned long long)a_mdur * 1000 / arate);
    unsigned movie_total = total; if (have_aud && a_total_ms > movie_total) movie_total = a_total_ms;

    unsigned char cfg[700];
    const char *fourcc = g_codec ? "hev1" : "avc1";
    const char *cfgtag = g_codec ? "hvcC" : "avcC";
    int cfgn = g_codec ? md_hvcc(cfg) : md_avcc(cfg);

    /* --- video track sizes (one-sample chunks because mdat is A/V interleaved) --- */
    unsigned vse  = 8 + 78 + (8 + cfgn);
    unsigned stsd = 16 + vse;
    unsigned stts = 16 + nrle*8;
    unsigned stss = 16 + nkey*4;
    unsigned stsc = 28;
    unsigned stsz = 20 + g_ns*4;
    unsigned stco = 16 + g_ns*4;
    unsigned stbl = 8 + stsd + stts + stss + stsc + stsz + stco;
    unsigned vmhd = 20, dinf = 36, minf = 8 + vmhd + dinf + stbl;
    unsigned hdlr = 8 + 4 + 4 + 4 + 12 + 13;
    unsigned mdhd = 32;
    unsigned mdia = 8 + mdhd + hdlr + minf;
    unsigned tkhd = 92;
    unsigned trak = 8 + tkhd + mdia;

    /* --- audio track sizes --- */
    unsigned a_mp4a = 8 + 28 + 39;
    unsigned a_stsd = 16 + a_mp4a;
    unsigned a_stts = 24;
    unsigned a_stsc = 28;
    unsigned a_stsz = 20 + g_a_ns*4;
    unsigned a_stco = 16 + g_a_ns*4;
    unsigned a_stbl = 8 + a_stsd + a_stts + a_stsc + a_stsz + a_stco;
    unsigned smhd = 16, a_dinf = 36, a_minf = 8 + smhd + a_dinf + a_stbl;
    unsigned a_hdlr = 8 + 4 + 4 + 4 + 12 + 13;
    unsigned a_mdhd = 32;
    unsigned a_mdia = 8 + a_mdhd + a_hdlr + a_minf;
    unsigned a_tkhd = 92;
    unsigned a_trak = 8 + a_tkhd + a_mdia;

    unsigned mvhd = 108;
    unsigned moov = 8 + mvhd + trak + (have_aud ? a_trak : 0);
    FILE *f = g_fp;

    be32(f,moov); tag4(f,"moov");
    be32(f,mvhd); tag4(f,"mvhd"); be32(f,0); be32(f,0); be32(f,0);
    be32(f,ts_scale); be32(f,movie_total); be32(f,0x00010000); be16(f,0x0100); be16(f,0);
    be32(f,0); be32(f,0); mtx(f);
    for (i=0;i<6;i++) be32(f,0);
    be32(f, have_aud ? 3 : 2);                             /* next_track_ID */

    /* ---------- video trak ---------- */
    be32(f,trak); tag4(f,"trak");
    be32(f,tkhd); tag4(f,"tkhd"); be32(f,7); be32(f,0); be32(f,0);
    be32(f,1); be32(f,0); be32(f,total); be32(f,0); be32(f,0);
    be16(f,0); be16(f,0); be16(f,0); be16(f,0); mtx(f);
    be32(f, (unsigned)g_w<<16); be32(f, (unsigned)g_h<<16);
    be32(f,mdia); tag4(f,"mdia");
    be32(f,mdhd); tag4(f,"mdhd"); be32(f,0); be32(f,0); be32(f,0);
    be32(f,ts_scale); be32(f,total); be16(f,0x55c4); be16(f,0);
    be32(f,hdlr); tag4(f,"hdlr"); be32(f,0); be32(f,0); tag4(f,"vide");
    be32(f,0); be32(f,0); be32(f,0); fwrite("VideoHandler\0",1,13,f);
    be32(f,minf); tag4(f,"minf");
    be32(f,vmhd); tag4(f,"vmhd"); be32(f,1); be16(f,0); be16(f,0); be16(f,0); be16(f,0);
    be32(f,dinf); tag4(f,"dinf");
    be32(f,28); tag4(f,"dref"); be32(f,0); be32(f,1);
    be32(f,12); tag4(f,"url "); be32(f,1);
    be32(f,stbl); tag4(f,"stbl");
    be32(f,stsd); tag4(f,"stsd"); be32(f,0); be32(f,1);
    be32(f,vse); tag4(f,fourcc);
    { unsigned char z[6]={0,0,0,0,0,0}; fwrite(z,1,6,f); } be16(f,1);
    be16(f,0); be16(f,0); be32(f,0); be32(f,0); be32(f,0);
    be16(f,g_w); be16(f,g_h);
    be32(f,0x00480000); be32(f,0x00480000); be32(f,0); be16(f,1);
    { unsigned char cn[32]; memset(cn,0,32); fwrite(cn,1,32,f); }
    be16(f,0x18); be16(f,0xffff);
    be32(f, 8+cfgn); tag4(f,cfgtag); fwrite(cfg,1,cfgn,f);
    be32(f,stts); tag4(f,"stts"); be32(f,0); be32(f,nrle);
    for (i=0;i<g_ns;) { int j=i+1; while (j<g_ns && dur[j]==dur[i]) j++; be32(f, j-i); be32(f, dur[i]); i=j; }
    be32(f,stss); tag4(f,"stss"); be32(f,0); be32(f,nkey);
    for (i=0;i<g_ns;i++) if (g_key[i]) be32(f, i+1);
    be32(f,stsc); tag4(f,"stsc"); be32(f,0); be32(f,1); be32(f,1); be32(f,1); be32(f,1);
    be32(f,stsz); tag4(f,"stsz"); be32(f,0); be32(f,0); be32(f,g_ns);
    for (i=0;i<g_ns;i++) be32(f, g_sz[i]);
    be32(f,stco); tag4(f,"stco"); be32(f,0); be32(f,g_ns);
    /* shifted by moov (its own size): md_close() moves mdat to right after moov for
       faststart, so offsets must already point to mdat's NEW position, not its current
       one right after ftyp. */
    for (i=0;i<g_ns;i++) be32(f, g_off[i] + moov);

    if (!have_aud) return;

    /* ---------- audio trak ---------- */
    be32(f,a_trak); tag4(f,"trak");
    be32(f,a_tkhd); tag4(f,"tkhd"); be32(f,7); be32(f,0); be32(f,0);
    be32(f,2); be32(f,0); be32(f,a_total_ms); be32(f,0); be32(f,0);
    be16(f,0); be16(f,0); be16(f,0x0100); be16(f,0); mtx(f);
    be32(f,0); be32(f,0);                                  /* width=height=0 */
    be32(f,a_mdia); tag4(f,"mdia");
    be32(f,a_mdhd); tag4(f,"mdhd"); be32(f,0); be32(f,0); be32(f,0);
    be32(f,arate); be32(f,a_mdur); be16(f,0x55c4); be16(f,0);
    be32(f,a_hdlr); tag4(f,"hdlr"); be32(f,0); be32(f,0); tag4(f,"soun");
    be32(f,0); be32(f,0); be32(f,0); fwrite("SoundHandler\0",1,13,f);
    be32(f,a_minf); tag4(f,"minf");
    be32(f,smhd); tag4(f,"smhd"); be32(f,0); be16(f,0); be16(f,0);
    be32(f,a_dinf); tag4(f,"dinf");
    be32(f,28); tag4(f,"dref"); be32(f,0); be32(f,1);
    be32(f,12); tag4(f,"url "); be32(f,1);
    be32(f,a_stbl); tag4(f,"stbl");
    be32(f,a_stsd); tag4(f,"stsd"); be32(f,0); be32(f,1);
    be32(f,a_mp4a); tag4(f,"mp4a");
    { unsigned char z6[6]={0,0,0,0,0,0}; fwrite(z6,1,6,f); } be16(f,1);
    be32(f,0); be32(f,0);
    be16(f, g_a_chan?g_a_chan:1); be16(f,16);
    be16(f,0); be16(f,0);
    be32(f, (unsigned)arate << 16);
    md_write_esds(f);
    be32(f,a_stts); tag4(f,"stts"); be32(f,0); be32(f,1); be32(f,g_a_ns); be32(f,1024);
    be32(f,a_stsc); tag4(f,"stsc"); be32(f,0); be32(f,1); be32(f,1); be32(f,1); be32(f,1);
    be32(f,a_stsz); tag4(f,"stsz"); be32(f,0); be32(f,0); be32(f,g_a_ns);
    for (i=0;i<g_a_ns;i++) be32(f, g_a_sz[i]);
    be32(f,a_stco); tag4(f,"stco"); be32(f,0); be32(f,g_a_ns);
    for (i=0;i<g_a_ns;i++) be32(f, g_a_off[i] + moov);
}

static void md_remux_faststart(const char *path, unsigned moov_size, unsigned mdat_total)
{
    /* Physically swap moov and mdat so the file becomes ftyp+moov+mdat ("faststart").
     * Confirmed on this project: a clip played all 17.7s end to end from a local copy, and
     * every HTTP Range request against it (including the tail bytes where moov lives)
     * answered correctly - yet streamed inline in a browser it still cut off around 7s. The
     * server and the file are both fine; browsers that only buffer the start of a
     * progressively-streamed file need the full sample table (moov) up front, and some just
     * stop once their initial buffer runs out rather than fetching the tail for it. stco
     * offsets were already written shifted by moov_size in md_finalize_mp4() above, so they
     * already point to mdat's position AFTER this move, not its current one. */
    char tmp[260];
    snprintf(tmp, sizeof(tmp), "%s.tmp", path);
    FILE *in = fopen(path, "rb");
    if (!in) return;
    FILE *out = fopen(tmp, "wb");
    if (!out) { fclose(in); return; }

    static unsigned char buf[8192];   /* static: keep it off the thread's small stack */
    size_t n;

    /* ftyp (24 bytes), unchanged */
    fseek(in, 0, SEEK_SET);
    n = fread(buf, 1, 24, in);
    fwrite(buf, 1, n, out);

    /* moov currently sits right after mdat, at offset 24 + mdat_total */
    fseek(in, 24 + (long)mdat_total, SEEK_SET);
    {
        long rem = (long)moov_size;
        while (rem > 0) {
            size_t chunk = (size_t)(rem > (long)sizeof(buf) ? (long)sizeof(buf) : rem);
            n = fread(buf, 1, chunk, in);
            if (n == 0) break;
            fwrite(buf, 1, n, out);
            rem -= (long)n;
        }
    }

    /* mdat currently sits right after ftyp, at offset 24, size mdat_total */
    fseek(in, 24, SEEK_SET);
    {
        long rem = (long)mdat_total;
        while (rem > 0) {
            size_t chunk = (size_t)(rem > (long)sizeof(buf) ? (long)sizeof(buf) : rem);
            n = fread(buf, 1, chunk, in);
            if (n == 0) break;
            fwrite(buf, 1, n, out);
            rem -= (long)n;
        }
    }

    fclose(in); fclose(out);
    rename(tmp, path);
}

static void md_close(void)
{
    if (!g_fp) { g_on = 0; return; }
    unsigned moov_size = 0, mdat_total = 0;
    if (!g_raw) {
        long endp = ftell(g_fp);
        fseek(g_fp, g_mdat_pos, SEEK_SET);
        be32(g_fp, 8 + g_mdat_bytes);
        fseek(g_fp, endp, SEEK_SET);
        md_finalize_mp4();
        mdat_total = 8 + g_mdat_bytes;
        long total_sz = ftell(g_fp);
        moov_size = (unsigned)(total_sz - 24 - (long)mdat_total);
    }
    fclose(g_fp); g_fp = NULL; g_on = 0;
    if (!g_raw && moov_size > 0) md_remux_faststart(g_path, moov_size, mdat_total);
    remove("/tmp/motion.rec");
    printf("MOTION: REC stop (%d video / %d audio frames)\n", g_ns, g_a_ns);
}

/* audio tee: encoded AAC frames arrive here from the rtsp audio thread (ADTS framed). */
void md_audio_feed(struct audio_stream *as)
{
    if (!g_aud_want || !g_mux_init || g_raw) return;
    if (!as || !as->data || !as->len) return;
    ak_thread_mutex_lock(&g_mux_lock);
    if (g_on && g_fp && g_a_off && g_a_ns < MD_MAXSAMP) {
        const unsigned char *d = as->data;
        unsigned n = as->len;
        int hdr = 0;
        if (n > 7 && d[0]==0xFF && (d[1]&0xF0)==0xF0) {       /* ADTS syncword */
            int crc_absent = d[1] & 1;                        /* 1 => 7-byte header */
            hdr = crc_absent ? 7 : 9;
            if (g_ascn == 0) {
                int profile  = (d[2] >> 6) & 0x3;             /* ADTS profile = AOT-1 */
                int freq_idx = (d[2] >> 2) & 0xF;
                int chan     = ((d[2] & 1) << 2) | ((d[3] >> 6) & 0x3);
                int aot = profile + 1;
                static const int frq[16] = {96000,88200,64000,48000,44100,32000,24000,22050,
                                            16000,12000,11025,8000,7350,0,0,0};
                g_asc[0] = (unsigned char)((aot << 3) | ((freq_idx >> 1) & 0x7));
                g_asc[1] = (unsigned char)(((freq_idx & 1) << 7) | ((chan & 0xF) << 3));
                g_ascn = 2;
                g_a_chan = chan ? chan : 1;
                if (freq_idx < 16 && frq[freq_idx]) g_a_rate = frq[freq_idx];
            }
        } else if (g_ascn == 0) {
            /* no ADTS header: assume AAC-LC mono at MD-configured rate */
            int rate = g_a_rate ? g_a_rate : 16000;
            int freq_idx = (rate==8000)?11:(rate==16000)?8:(rate==44100)?4:(rate==48000)?3:8;
            g_asc[0] = (unsigned char)((2 << 3) | ((freq_idx >> 1) & 0x7));
            g_asc[1] = (unsigned char)(((freq_idx & 1) << 7) | (1 << 3));
            g_ascn = 2; g_a_chan = 1;
        }
        if ((unsigned)hdr < n) {
            long off = ftell(g_fp);
            fwrite(d + hdr, 1, n - hdr, g_fp);
            g_a_off[g_a_ns] = (unsigned)off;
            g_a_sz[g_a_ns]  = n - hdr;
            g_a_ns++;
            g_mdat_bytes += (n - hdr);
            g_has_audio = 1;
        }
    }
    ak_thread_mutex_unlock(&g_mux_lock);
}

void md_record_feed(int dev_id, int chn, int enc_type, int w, int h, struct video_stream *stream)
{
    if (!g_dir[0]) {
        const char *dd = getenv("MD_DIR");
        snprintf(g_dir, sizeof(g_dir), "%s", (dd && *dd) ? dd : "/mnt/sdcard/rec");
        g_raw = (getenv("MD_FMT") && !strcmp(getenv("MD_FMT"), "raw"));
        g_rec_chn = md_env_i("MD_CHN", 1);
        g_aud_want = md_env_i("MD_AUDIO", 1);
        if (!g_off) {
            g_off = ak_mem_alloc(MODULE_ID_APP, sizeof(unsigned)*MD_MAXSAMP);
            g_sz  = ak_mem_alloc(MODULE_ID_APP, sizeof(unsigned)*MD_MAXSAMP);
            g_ts  = ak_mem_alloc(MODULE_ID_APP, sizeof(unsigned)*MD_MAXSAMP);
            g_key = ak_mem_alloc(MODULE_ID_APP, MD_MAXSAMP);
            g_a_off = ak_mem_alloc(MODULE_ID_APP, sizeof(unsigned)*MD_MAXSAMP);
            g_a_sz  = ak_mem_alloc(MODULE_ID_APP, sizeof(unsigned)*MD_MAXSAMP);
        }
    }
    /* enc_type==1 is MJPEG: belt-and-braces filter. Confirmed on hardware (AK3918EV300L,
       libmpi_venc V2.0.44) that neither venc channel can do real H.264 here - vs1 rejects
       it outright (venc_check_and_format_param: enc_out_type error 0), vs0 silently falls
       back to MJPEG without raising any error (traced live via a temporary debug printf on
       every keyframe: chn=0 always reported enc=1 regardless of the h.264 ini request). So
       HEVC on vs1 (MD_CHN=1) is the only working combination - see docs/05-video-pipeline.md. */
    if (dev_id != 0 || chn != g_rec_chn || enc_type == 1) return;   /* record one stream only, never MJPEG */

    int want = md_now_ms() < g_md_rec_until;
    int is_key = (stream->frame_type == FRAME_TYPE_I || stream->frame_type == FRAME_TYPE_PI);
    int maxclip = md_env_i("MD_MAXCLIP", 120);

    ak_thread_mutex_lock(&g_mux_lock);
    if (g_on && is_key &&
        ((md_now_ms() - g_rec_start) > (long long)maxclip*1000 || g_ns >= MD_MAXSAMP-4 || g_a_ns >= MD_MAXSAMP-4)) {
        md_close();
        if (want) md_open(enc_type);
    }
    if (want && !g_on) {
        if (is_key) md_open(enc_type);
        else { ak_thread_mutex_unlock(&g_mux_lock); return; }
    }
    if (g_on && stream->data && stream->len) {
        if (g_raw) {
            fwrite(stream->data, 1, stream->len, g_fp);
        } else if (g_off && g_ns < MD_MAXSAMP) {
            if (g_w == 0) { g_w = w; g_h = h; }
            if (g_ns == 0) g_ts_base = stream->ts;
            long off = ftell(g_fp);
            unsigned wr = md_write_nals(g_fp, stream->data, stream->len);
            if (wr) {
                g_off[g_ns] = (unsigned)off; g_sz[g_ns] = wr;
                g_ts[g_ns] = (unsigned)(stream->ts - g_ts_base);
                g_key[g_ns] = is_key ? 1 : 0;
                g_ns++; g_mdat_bytes += wr;
            }
        }
    }
    if (!want && g_on) md_close();
    ak_thread_mutex_unlock(&g_mux_lock);
}

/* parameter sets from the SDK's own mrd sample (platform/sample/mt), day vs night */
static void md_mrd_config(void *h, int night, int size_min, int size_max)
{
    if (night) {
        ak_mrd_set_mode(h, 0);
        ak_mrd_set_floating_wadding_params(h, 4, 2, 4, 9, 2, 3);
        ak_mrd_set_filters(h, md_env_i("MD_MRD_BIG_N", 15000), md_env_i("MD_MRD_SMALL_N", 10000));
    } else {
        ak_mrd_set_mode(h, 1);
        ak_mrd_set_floating_wadding_params(h, 3, 2, 4, 36, 2, 3);
        ak_mrd_set_filters(h, md_env_i("MD_MRD_BIG", 12000), md_env_i("MD_MRD_SMALL", 6000));
    }
    ak_mrd_set_motion_region_size(h, size_min, size_max);
}
static int md_is_night(void)
{
    char b[8] = "";
    FILE *f = fopen("/tmp/daynight.state", "r");
    if (f) { if (!fgets(b, sizeof(b), f)) b[0] = 0; fclose(f); }
    return !strncmp(b, "night", 5);
}

static void *motion_th(void *arg)
{
    (void)arg;
    ak_thread_set_name("motion_th");
    unsigned short (*grid)[MD_W] = ak_mem_alloc(MODULE_ID_APP, sizeof(unsigned short)*MD_W*MD_H);
    if (!grid) return NULL;

    int cell_th  = md_env_i("MD_CELL_TH", 80);
    int min_cell = md_env_i("MD_MIN_CELLS", 6);
    int hold_s   = md_env_i("MD_HOLD", 8);
    int use_mrd  = md_env_i("MD_MRD", 1);
    int rows     = md_env_i("MD_ROWS", 24);
    int mrd_min  = md_env_i("MD_MRD_MIN", 6);
    int period   = md_env_i("MD_PERIOD", use_mrd ? 100 : 300);   /* mrd filters across frames */
    if (rows < 1 || rows > MD_H) rows = 24;
    const char *hook = getenv("MD_HOOK");
    void *mrd = use_mrd ? ak_mrd_init() : NULL;
    if (use_mrd)
        printf("MOTION: mrd %s (rows=%d min=%d)\n", mrd ? "on" : "init FAILED, plain count", rows, mrd_min);
    int mrd_night = -1, seen_rows = 0;
    int was = 0;
    ak_sleep_ms(4000);
    for (;;) {
        memset(grid, 0, sizeof(unsigned short)*MD_W*MD_H);
        int r = ak_vpss_md_get_stat(VIDEO_DEV0, grid);
        int active = 0, peak = 0, nbox = 0, area = 0, i, j;
        if (r == 0) {
            for (i = 0; i < MD_H; i++)
                for (j = 0; j < MD_W; j++) {
                    int v = grid[i][j];
                    if (v > peak) peak = v;
                    if (v > cell_th) active++;
                    if (v && i >= seen_rows) seen_rows = i + 1;
                }
        }
        int motion = 0;
        if (r == 0 && mrd) {
            int night = md_is_night();
            if (night != mrd_night) { md_mrd_config(mrd, night, mrd_min, MD_W * rows); mrd_night = night; }
            MD_MRD_RECT boxes[MD_MRD_CAP];
            memset(boxes, 0, sizeof(boxes));
            nbox = ak_mrd(mrd, grid, rows, boxes);
            for (i = 0; i < nbox && i < MD_MRD_CAP; i++) {
                int a = (boxes[i].right - boxes[i].left) * (boxes[i].bottom - boxes[i].top);
                if (a > area) area = a;
            }
            motion = nbox > 0;
        } else if (r == 0) {
            motion = active >= min_cell;
        }
        g_md_level = active;
        if (motion)
            g_md_rec_until = md_now_ms() + (long long)hold_s*1000;

        int on = md_now_ms() < g_md_rec_until;
        if (on && !was) {
            FILE *f = fopen("/tmp/motion.active", "w"); if (f) { fputs("1\n", f); fclose(f); }
            if (hook && *hook) { char c[256]; snprintf(c, sizeof(c), "%s &", hook); system(c); }
        }
        if (!on && was) remove("/tmp/motion.active");
        was = on;

        FILE *lg = fopen("/tmp/motion.level", "w");
        if (lg) { fprintf(lg, "level=%d peak=%d th=%d need=%d state=%s ret=%d mrd=%d boxes=%d area=%d rows=%d/%d\n",
                          active, peak, cell_th, min_cell, on ? "REC" : "idle", r,
                          mrd ? 1 : 0, nbox, area, seen_rows, rows); fclose(lg); }
        ak_sleep_ms(period);
    }
    return NULL;
}
'''

anchor_main = 'int main(int argc, char **argv)'
assert s.count(anchor_main) == 1, ("main", s.count(anchor_main))
s = s.replace(anchor_main, func + '\n' + anchor_main, 1)

anchor_start = '    signal (SIGPIPE, SIG_IGN);\n'
assert s.count(anchor_start) == 1, ("sigpipe", s.count(anchor_start))
s = s.replace(anchor_start, anchor_start +
    '    ak_thread_mutex_init(&g_mux_lock, NULL); g_mux_init = 1;\n'
    '    if (md_env_i("MD", 1) != 0) {\n'
    '        ak_pthread_t md_tid;\n'
    '        ak_thread_create(&md_tid, motion_th, NULL, ANYKA_THREAD_MIN_STACK_SIZE, 18);\n'
    '    }\n', 1)

anchor_tee = '                ak_thread_mutex_unlock(&rtsp_param.lock_buffer_video[dev_id][chn]);\n'
assert s.count(anchor_tee) == 1, ("tee", s.count(anchor_tee))
s = s.replace(anchor_tee, anchor_tee +
    '                md_record_feed(dev_id, chn, venc_th->ve_param.enc_out_type, venc_th->ve_param.width, venc_th->ve_param.height, stream);\n', 1)

open(p, 'w', encoding='latin-1').write(s)
print("patch motion (mp4, codec-aware, AAC audio, interleaved) OK")
