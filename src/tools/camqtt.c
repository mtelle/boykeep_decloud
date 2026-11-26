/*
 * camqtt - minimal MQTT 3.1.1 bridge between the de-cloud camera and Home Assistant.
 *
 * Self-contained (no libmosquitto, no TLS): one static binary of a few tens of KB that talks
 * plain MQTT to a LAN broker (e.g. the Home Assistant Mosquitto add-on) and announces itself
 * with MQTT Discovery, so every entity appears under one HA device with no YAML.
 *
 * It only reads the state files the rest of the stack already writes, and writes the control
 * files those components already read:
 *   /tmp/motion.rec        exists while a clip is recording        -> binary_sensor "motion"
 *   /tmp/daynight.state    day | night                             -> sensor "day/night"
 *   /tmp/daynight.mode     auto | day | night (read by rtspd)      <- select "day/night mode"
 *   /tmp/motion.off        present = never record (read by rtspd)  <- switch "motion detection"
 *   $SNAP_PATH             JPEG written atomically by rtspd        -> camera "snapshot"
 *   $CAM/bin/ptz           left|right|up|down <steps>, home        <- buttons "PTZ ..."
 *   $MD_DIR/YYYYMMDD/mo_*  read only, when a clip closes (/tmp/motion.rec changes or goes)
 *                          and every 10 min                        -> sensors "last recording",
 *                                                                     "recordings today" (+ list)
 * The mode and the switch are persisted in $CAM/etc (daynight.mode, motion.off) so a reboot
 * keeps them.
 *
 * Env (from $CAM/etc/mqtt.env, never in git - see deploy/mqtt.env.example):
 *   MQTT_HOST (required)  MQTT_PORT(1883)  MQTT_USER  MQTT_PASS
 *   MQTT_ID(cam_<wlan mac>)  MQTT_NAME(Camera)  MQTT_BASE(decloud/<id>)  MQTT_DISCOVERY(homeassistant)
 *   MQTT_LANG(en|es)  MQTT_SNAP_INT(60 s, 0 = only on motion/request)  MQTT_PTZ_STEPS(300)
 *   SNAP_PATH(/tmp/www/snapshot.jpg)  MD_DIR(/mnt/sdcard/rec)  MQTT_URL_BASE(http://<wlan ip>)
 *
 * Topics (BASE = MQTT_BASE):
 *   BASE/availability            online | offline (retained, offline = broker Last Will)
 *   BASE/motion/state            ON | OFF                      BASE/snapshot/image   JPEG bytes
 *   BASE/daynight/state          day | night                   BASE/daynight_mode/state + /set
 *   BASE/motion_detection/state  ON | OFF  + /set              BASE/ptz/set  left|right|up|down|home
 *   BASE/snapshot/set            (any) publish a snapshot now  BASE/reboot/set  PRESS
 *   BASE/sd_free/state  BASE/uptime/state  BASE/wifi/state   (diagnostics, every 60 s)
 *   BASE/last_clip/state  ISO time + /attributes {day,file,url,size_kb}
 *   BASE/clips_today/state count + BASE/clips/attributes {"clips":[newest 20 ...]}
 *
 * Build: arm-unknown-linux-uclibcgnueabi-gcc -march=armv5te -marm -static -O2 -o camqtt camqtt.c
 */
#include <stdio.h>
#include <stdarg.h>
#include <time.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <netdb.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <dirent.h>
#include <sys/ioctl.h>
#include <net/if.h>

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif
#define CAM "/mnt/sdcard/cam"
#define KEEPALIVE 60
#define SNAP_MAX (1024 * 1024)

#define CLIPS_MAX 20                                 /* recordings listed in the HA attributes */
#define DAYS_MAX 512
#define FILES_MAX 4096

static const char *g_host, *g_user, *g_pass, *g_name, *g_disc, *g_snap, *g_rec;
static char g_id[64], g_base[128], g_avail[160], g_wlan[IFNAMSIZ];
static long g_clips_due;                             /* when to re-publish the recordings list */
static int g_port, g_snap_int, g_ptz_steps, g_es;
static int g_fd = -1;
static long g_last_tx, g_last_rx;

static void mqtt_close(void);
static void logm(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void logm(const char *fmt, ...)
{
    va_list ap; time_t t = time(NULL); struct tm tm; char ts[32];
    localtime_r(&t, &tm); strftime(ts, sizeof ts, "%Y-%m-%d %H:%M:%S", &tm);
    fprintf(stderr, "%s camqtt: ", ts);
    va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap);
    fputc('\n', stderr);
}

/* monotonic seconds: /proc/uptime does not jump when timesync sets the clock */
static long now_s(void)
{
    FILE *f = fopen("/proc/uptime", "r"); double u = 0;
    if (f) { if (fscanf(f, "%lf", &u) != 1) u = 0; fclose(f); }
    return (long)u;
}

static const char *envd(const char *k, const char *d) { const char *v = getenv(k); return (v && *v) ? v : d; }

static int read_word(const char *path, char *out, int n)
{
    FILE *f = fopen(path, "r"); int i = 0, c;
    out[0] = 0;
    if (!f) return -1;
    while (i < n - 1 && (c = fgetc(f)) != EOF && c != '\n' && c != '\r' && c != ' ') out[i++] = (char)c;
    out[i] = 0; fclose(f);
    return i;
}

static void write_word(const char *path, const char *w)
{
    FILE *f = fopen(path, "w");
    if (f) { fprintf(f, "%s\n", w); fclose(f); }
}

static void touch(const char *path) { FILE *f = fopen(path, "a"); if (f) fclose(f); }

/* ---------------------------------------------------------------- MQTT wire format */
static int send_all(const void *buf, size_t n)
{
    const char *p = buf;
    if (g_fd < 0) return -1;
    while (n) {
        ssize_t w = send(g_fd, p, n, MSG_NOSIGNAL);
        if (w < 0) { if (errno == EINTR) continue; logm("send failed: %s", strerror(errno)); mqtt_close(); return -1; }
        p += w; n -= (size_t)w;
    }
    g_last_tx = now_s();
    return 0;
}

static int recv_all(void *buf, size_t n)
{
    char *p = buf;
    while (n) {
        ssize_t r = recv(g_fd, p, n, 0);
        if (r == 0) return -1;
        if (r < 0) { if (errno == EINTR) continue; return -1; }
        p += r; n -= (size_t)r;
    }
    return 0;
}

static int enc_len(unsigned char *o, unsigned long len)
{
    int i = 0;
    do { unsigned char b = len % 128; len /= 128; if (len) b |= 0x80; o[i++] = b; } while (len && i < 4);
    return i;
}

static int put_str(unsigned char *o, const char *s)
{
    size_t n = strlen(s);
    o[0] = (unsigned char)(n >> 8); o[1] = (unsigned char)n; memcpy(o + 2, s, n);
    return (int)n + 2;
}

static int mqtt_publish(const char *topic, const void *data, size_t len, int retain)
{
    unsigned char hdr[8]; int h = 0; size_t tl = strlen(topic);
    if (g_fd < 0) return -1;
    hdr[h++] = 0x30 | (retain ? 1 : 0);
    h += enc_len(hdr + h, 2 + tl + len);
    hdr[h++] = (unsigned char)(tl >> 8); hdr[h++] = (unsigned char)tl;
    if (send_all(hdr, h) || send_all(topic, tl) || (len && send_all(data, len))) return -1;
    return 0;
}

static int pub(const char *sub, const char *val, int retain)
{
    char t[256]; snprintf(t, sizeof t, "%s/%s", g_base, sub);
    return mqtt_publish(t, val, strlen(val), retain);
}

static int mqtt_subscribe(const char *topic, unsigned short id)
{
    unsigned char b[300]; int n = 0, h;
    unsigned char body[290];
    body[n++] = (unsigned char)(id >> 8); body[n++] = (unsigned char)id;
    n += put_str(body + n, topic);
    body[n++] = 0;                                   /* QoS 0 */
    b[0] = 0x82; h = 1 + enc_len(b + 1, n);
    memcpy(b + h, body, n);
    return send_all(b, h + n);
}

static int mqtt_connect(void)
{
    struct addrinfo hints, *ai = NULL, *a;
    char port[8]; int fd = -1;
    struct timeval tv = { 10, 0 };
    unsigned char pkt[1024], body[1000]; int n = 0, h;
    unsigned char flags = 0x02 | 0x04 | 0x20;        /* clean session, will, will retain */

    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_INET; hints.ai_socktype = SOCK_STREAM;
    snprintf(port, sizeof port, "%d", g_port);
    if (getaddrinfo(g_host, port, &hints, &ai) != 0 || !ai) { logm("cannot resolve %s", g_host); return -1; }
    for (a = ai; a; a = a->ai_next) {
        fd = socket(a->ai_family, a->ai_socktype, a->ai_protocol);
        if (fd < 0) continue;
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
        if (connect(fd, a->ai_addr, a->ai_addrlen) == 0) break;
        close(fd); fd = -1;
    }
    freeaddrinfo(ai);
    if (fd < 0) { logm("cannot connect to %s:%d", g_host, g_port); return -1; }
    { int one = 1; setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one); }
    g_fd = fd;

    if (g_user && *g_user) flags |= 0x80;
    if (g_user && *g_user && g_pass && *g_pass) flags |= 0x40;
    n += put_str(body + n, "MQTT");
    body[n++] = 4; body[n++] = flags;
    body[n++] = KEEPALIVE >> 8; body[n++] = KEEPALIVE & 0xff;
    n += put_str(body + n, g_id);
    n += put_str(body + n, g_avail);
    n += put_str(body + n, "offline");
    if (flags & 0x80) n += put_str(body + n, g_user);
    if (flags & 0x40) n += put_str(body + n, g_pass);
    pkt[0] = 0x10; h = 1 + enc_len(pkt + 1, n);
    memcpy(pkt + h, body, n);
    if (send_all(pkt, h + n)) goto fail;

    {   /* CONNACK: 20 02 00 rc */
        unsigned char ack[4];
        if (recv_all(ack, 4) || ack[0] != 0x20) { logm("bad CONNACK"); goto fail; }
        if (ack[3] != 0) {
            logm("broker refused connection (rc=%d%s)", ack[3], ack[3] == 4 || ack[3] == 5 ? ": check MQTT_USER/MQTT_PASS" : "");
            goto fail;
        }
    }
    g_last_rx = now_s();
    logm("connected to %s:%d as %s", g_host, g_port, g_id);
    return 0;
fail:
    mqtt_close();
    return -1;
}

static void mqtt_close(void)
{
    if (g_fd >= 0) close(g_fd);
    g_fd = -1;
}

/* ---------------------------------------------------------------- Home Assistant discovery */
static const char *tr(const char *en, const char *es) { return g_es ? es : en; }

static void disc(const char *comp, const char *obj, const char *name, const char *extra)
{
    char t[256], j[1400];
    snprintf(t, sizeof t, "%s/%s/%s/%s/config", g_disc, comp, g_id, obj);
    snprintf(j, sizeof j,
        "{\"name\":\"%s\",\"unique_id\":\"%s_%s\","
        "\"availability_topic\":\"%s\",%s,"
        "\"device\":{\"identifiers\":[\"%s\"],\"name\":\"%s\",\"manufacturer\":\"de-cloud\","
        "\"model\":\"AK3918EV300L PTZ\"}}",
        name, g_id, obj, g_avail, extra, g_id, g_name);
    mqtt_publish(t, j, strlen(j), 1);
}

static void publish_discovery(void)
{
    char x[600];
    snprintf(x, sizeof x, "\"state_topic\":\"%s/motion/state\",\"device_class\":\"motion\"", g_base);
    disc("binary_sensor", "motion", tr("Motion", "Movimiento"), x);

    snprintf(x, sizeof x, "\"topic\":\"%s/snapshot/image\"", g_base);
    disc("camera", "snapshot", tr("Snapshot", "Captura"), x);

    snprintf(x, sizeof x, "\"state_topic\":\"%s/daynight/state\",\"device_class\":\"enum\","
        "\"options\":[\"day\",\"night\"],\"icon\":\"mdi:theme-light-dark\"", g_base);
    disc("sensor", "daynight", tr("Day/night", "Día/noche"), x);

    snprintf(x, sizeof x, "\"state_topic\":\"%s/daynight_mode/state\",\"command_topic\":\"%s/daynight_mode/set\","
        "\"options\":[\"auto\",\"day\",\"night\"],\"icon\":\"mdi:weather-night\"", g_base, g_base);
    disc("select", "daynight_mode", tr("Day/night mode", "Modo día/noche"), x);

    snprintf(x, sizeof x, "\"state_topic\":\"%s/motion_detection/state\",\"command_topic\":\"%s/motion_detection/set\","
        "\"icon\":\"mdi:motion-sensor\"", g_base, g_base);
    disc("switch", "motion_detection", tr("Motion detection", "Detección de movimiento"), x);

    {
        static const char *d[5][4] = {
            { "left",  "PTZ left",  "PTZ izquierda", "mdi:arrow-left-bold" },
            { "right", "PTZ right", "PTZ derecha",   "mdi:arrow-right-bold" },
            { "up",    "PTZ up",    "PTZ arriba",    "mdi:arrow-up-bold" },
            { "down",  "PTZ down",  "PTZ abajo",     "mdi:arrow-down-bold" },
            { "home",  "PTZ home",  "PTZ centrar",   "mdi:home" } };
        int i; char obj[32];
        for (i = 0; i < 5; i++) {
            snprintf(obj, sizeof obj, "ptz_%s", d[i][0]);
            snprintf(x, sizeof x, "\"command_topic\":\"%s/ptz/set\",\"payload_press\":\"%s\",\"icon\":\"%s\"",
                     g_base, d[i][0], d[i][3]);
            disc("button", obj, tr(d[i][1], d[i][2]), x);
        }
    }
    snprintf(x, sizeof x, "\"command_topic\":\"%s/snapshot/set\",\"icon\":\"mdi:camera\"", g_base);
    disc("button", "snapshot_now", tr("Take snapshot", "Hacer captura"), x);

    snprintf(x, sizeof x, "\"command_topic\":\"%s/reboot/set\",\"device_class\":\"restart\","
        "\"entity_category\":\"config\"", g_base);
    disc("button", "reboot", tr("Reboot", "Reiniciar"), x);

    snprintf(x, sizeof x, "\"state_topic\":\"%s/last_clip/state\",\"device_class\":\"timestamp\","
        "\"json_attributes_topic\":\"%s/last_clip/attributes\",\"icon\":\"mdi:filmstrip\"", g_base, g_base);
    disc("sensor", "last_clip", tr("Last recording", "Última grabación"), x);

    snprintf(x, sizeof x, "\"state_topic\":\"%s/clips_today/state\",\"json_attributes_topic\":\"%s/clips/attributes\","
        "\"state_class\":\"measurement\",\"icon\":\"mdi:filmstrip-box-multiple\"", g_base, g_base);
    disc("sensor", "clips_today", tr("Recordings today", "Grabaciones hoy"), x);

    snprintf(x, sizeof x, "\"state_topic\":\"%s/sd_free/state\",\"unit_of_measurement\":\"MB\","
        "\"state_class\":\"measurement\",\"icon\":\"mdi:sd\",\"entity_category\":\"diagnostic\"", g_base);
    disc("sensor", "sd_free", tr("SD free", "SD libre"), x);

    snprintf(x, sizeof x, "\"state_topic\":\"%s/uptime/state\",\"unit_of_measurement\":\"s\","
        "\"device_class\":\"duration\",\"entity_category\":\"diagnostic\"", g_base);
    disc("sensor", "uptime", tr("Uptime", "Tiempo encendida"), x);

    snprintf(x, sizeof x, "\"state_topic\":\"%s/wifi/state\",\"unit_of_measurement\":\"dBm\","
        "\"device_class\":\"signal_strength\",\"state_class\":\"measurement\",\"entity_category\":\"diagnostic\"", g_base);
    disc("sensor", "wifi", tr("WiFi signal", "Señal WiFi"), x);
}

/* ---------------------------------------------------------------- recordings */
static int cmp_desc(const void *a, const void *b) { return strcmp(*(char *const *)b, *(char *const *)a); }

static int has_ext(const char *s, const char *ext)
{
    size_t l = strlen(s), e = strlen(ext);
    return l > e && !strcmp(s + l - e, ext);
}

/* names in dir, newest first: day folders (8 digits) or clip files; caller frees */
static int list_names(const char *dir, int days, char **out, int max)
{
    DIR *d = opendir(dir); struct dirent *e; int n = 0;
    if (!d) return 0;
    while ((e = readdir(d)) && n < max) {
        const char *nm = e->d_name;
        if (days ? (strlen(nm) != 8 || strspn(nm, "0123456789") != 8)
                 : !(has_ext(nm, ".mp4") || has_ext(nm, ".h264") || has_ext(nm, ".h265"))) continue;
        if ((out[n] = strdup(nm))) n++;
    }
    closedir(d);
    qsort(out, n, sizeof *out, cmp_desc);
    return n;
}

static void url_base(char *out, size_t n)
{
    const char *u = getenv("MQTT_URL_BASE");
    struct ifreq ifr; int s;
    if (u && *u) { snprintf(out, n, "%s", u); return; }
    snprintf(out, n, "http://camera");
    s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) return;
    memset(&ifr, 0, sizeof ifr);
    snprintf(ifr.ifr_name, sizeof ifr.ifr_name, "%s", g_wlan);
    if (ioctl(s, SIOCGIFADDR, &ifr) == 0)
        snprintf(out, n, "http://%s", inet_ntoa(((struct sockaddr_in *)&ifr.ifr_addr)->sin_addr));
    close(s);
}

/* "mo_YYYYMMDD_HHMMSS.mp4" (local time) -> ISO-8601 UTC for an HA timestamp sensor */
static int clip_iso(const char *file, char *out, size_t n)
{
    struct tm tm; time_t t;
    memset(&tm, 0, sizeof tm);
    if (sscanf(file, "mo_%4d%2d%2d_%2d%2d%2d", &tm.tm_year, &tm.tm_mon, &tm.tm_mday,
               &tm.tm_hour, &tm.tm_min, &tm.tm_sec) != 6) return -1;
    tm.tm_year -= 1900; tm.tm_mon -= 1; tm.tm_isdst = -1;
    t = mktime(&tm);
    if (t == (time_t)-1) return -1;
    gmtime_r(&t, &tm);
    strftime(out, n, "%Y-%m-%dT%H:%M:%S+00:00", &tm);
    return 0;
}

/* read-only scan of the SD: the newest CLIPS_MAX clips, today's count, the last clip */
static void publish_clips(void)
{
    char **days = malloc(DAYS_MAX * sizeof *days), **files = malloc(FILES_MAX * sizeof *files);
    char today[16], base[96], json[CLIPS_MAX * 200 + 64], last[512] = "", iso[40] = "None";
    int nd, i, j, total = 0, today_n = 0, jl;
    time_t now = time(NULL); struct tm tm;
    if (!days || !files) { free(days); free(files); return; }
    localtime_r(&now, &tm); strftime(today, sizeof today, "%Y%m%d", &tm);
    url_base(base, sizeof base);
    jl = snprintf(json, sizeof json, "{\"clips\":[");
    nd = list_names(g_rec, 1, days, DAYS_MAX);
    for (i = 0; i < nd; i++) {
        char dp[256]; int nf;
        if (total >= CLIPS_MAX && strcmp(days[i], today)) { free(days[i]); continue; }
        snprintf(dp, sizeof dp, "%s/%s", g_rec, days[i]);
        nf = list_names(dp, 0, files, FILES_MAX);
        if (!strcmp(days[i], today)) today_n = nf;
        for (j = 0; j < nf; j++) {
            if (total < CLIPS_MAX && jl < (int)sizeof json - 400) {
                char fp[320]; struct stat st; long kb = 0;
                snprintf(fp, sizeof fp, "%s/%s", dp, files[j]);
                if (stat(fp, &st) == 0) kb = (long)(st.st_size / 1024);
                jl += snprintf(json + jl, sizeof json - jl, "%s{\"day\":\"%s\",\"file\":\"%s\",\"url\":\"%s/rec/%s/%s\",\"size_kb\":%ld}",
                               total ? "," : "", days[i], files[j], base, days[i], files[j], kb);
                if (total == 0) {
                    snprintf(last, sizeof last, "{\"day\":\"%s\",\"file\":\"%s\",\"url\":\"%s/rec/%s/%s\",\"size_kb\":%ld}",
                             days[i], files[j], base, days[i], files[j], kb);
                    if (clip_iso(files[j], iso, sizeof iso)) strcpy(iso, "None");
                }
                total++;
            }
            free(files[j]);
        }
        free(days[i]);
    }
    free(days); free(files);
    if (jl >= (int)sizeof json - 3) jl = (int)sizeof json - 3;
    snprintf(json + jl, sizeof json - jl, "]}");
    {
        char v[16]; snprintf(v, sizeof v, "%d", today_n);
        pub("clips/attributes", json, 1);
        pub("clips_today/state", v, 1);
        pub("last_clip/attributes", *last ? last : "{}", 1);
        pub("last_clip/state", iso, 1);
    }
}

/* ---------------------------------------------------------------- state */
static char s_motion[8], s_dn[16], s_mode[16], s_md[8], s_recpath[256];

static void publish_snapshot(void)
{
    FILE *f = fopen(g_snap, "rb"); char *buf; long n; char t[200];
    if (!f) return;
    buf = malloc(SNAP_MAX);
    if (!buf) { fclose(f); return; }
    n = (long)fread(buf, 1, SNAP_MAX, f);
    fclose(f);
    if (n > 0 && n < SNAP_MAX) {
        snprintf(t, sizeof t, "%s/snapshot/image", g_base);
        mqtt_publish(t, buf, (size_t)n, 0);
    }
    free(buf);
}

static void publish_diag(void)
{
    char v[32]; struct statvfs sv; FILE *f; char line[256];
    if (statvfs("/mnt/sdcard", &sv) == 0) {
        snprintf(v, sizeof v, "%llu", (unsigned long long)sv.f_bavail * sv.f_frsize / (1024 * 1024));
        pub("sd_free/state", v, 0);
    }
    snprintf(v, sizeof v, "%ld", now_s()); pub("uptime/state", v, 0);
    f = fopen("/proc/net/wireless", "r");
    if (f) {                               /* "wlan0: 0000   70.  -40.  -256 ..." -> level */
        while (fgets(line, sizeof line, f)) {
            char *c = strchr(line, ':'); int st; float q, lvl;
            if (c && sscanf(c + 1, "%x %f %f", &st, &q, &lvl) == 3) {
                snprintf(v, sizeof v, "%d", (int)lvl); pub("wifi/state", v, 0); break;
            }
        }
        fclose(f);
    }
}

/* poll the state files; publish what changed (all of it when force) */
static void poll_state(int force)
{
    char v[16], path[256];
    const char *m = access("/tmp/motion.rec", F_OK) == 0 ? "ON" : "OFF";
    /* /tmp/motion.rec holds the open clip's path: when it changes or goes away, a clip was
       just closed (end of motion, or MD_MAXCLIP rotation) -> refresh the list a moment later */
    if (read_word("/tmp/motion.rec", path, sizeof path) <= 0) path[0] = 0;
    if (strcmp(path, s_recpath)) {
        if (s_recpath[0]) g_clips_due = now_s() + 2;
        snprintf(s_recpath, sizeof s_recpath, "%s", path);
    }
    if (force || strcmp(m, s_motion)) {
        int started = !force && !strcmp(m, "ON");
        strcpy(s_motion, m); pub("motion/state", m, 1);
        if (started) publish_snapshot();
    }
    if (read_word("/tmp/daynight.state", v, sizeof v) > 0 && (force || strcmp(v, s_dn))) {
        strcpy(s_dn, v); pub("daynight/state", v, 1);
    }
    if (read_word("/tmp/daynight.mode", v, sizeof v) <= 0) strcpy(v, "auto");
    if (force || strcmp(v, s_mode)) { strcpy(s_mode, v); pub("daynight_mode/state", v, 1); }
    m = access("/tmp/motion.off", F_OK) == 0 ? "OFF" : "ON";
    if (force || strcmp(m, s_md)) { strcpy(s_md, m); pub("motion_detection/state", m, 1); }
}

/* ---------------------------------------------------------------- commands */
static void handle(const char *topic, const char *p)
{
    size_t bl = strlen(g_base);
    const char *sub;
    if (!strcmp(topic, envd("MQTT_HA_STATUS", "homeassistant/status"))) {
        if (!strcmp(p, "online")) { logm("HA came online: re-announcing"); publish_discovery(); poll_state(1); publish_snapshot(); publish_clips(); }
        return;
    }
    if (strncmp(topic, g_base, bl) || topic[bl] != '/') return;
    sub = topic + bl + 1;
    if (!strcmp(sub, "ptz/set")) {
        char cmd[160];
        if (!strcmp(p, "left") || !strcmp(p, "right") || !strcmp(p, "up") || !strcmp(p, "down"))
            snprintf(cmd, sizeof cmd, CAM "/bin/ptz %s %d >/dev/null 2>&1 &", p, g_ptz_steps);
        else if (!strcmp(p, "home") || !strcmp(p, "stop"))
            snprintf(cmd, sizeof cmd, CAM "/bin/ptz %s >/dev/null 2>&1 &", p);
        else { logm("ignored ptz command '%s'", p); return; }
        if (system(cmd) == -1) logm("ptz: system() failed");
    } else if (!strcmp(sub, "daynight_mode/set")) {
        if (strcmp(p, "auto") && strcmp(p, "day") && strcmp(p, "night")) { logm("ignored mode '%s'", p); return; }
        write_word("/tmp/daynight.mode", p);
        write_word(CAM "/etc/daynight.mode", p);
        logm("day/night mode -> %s", p);
    } else if (!strcmp(sub, "motion_detection/set")) {
        if (!strcmp(p, "OFF")) { touch("/tmp/motion.off"); touch(CAM "/etc/motion.off"); }
        else if (!strcmp(p, "ON")) { unlink("/tmp/motion.off"); unlink(CAM "/etc/motion.off"); }
        else return;
        logm("motion detection -> %s", p);
    } else if (!strcmp(sub, "snapshot/set")) {
        publish_snapshot();
        return;
    } else if (!strcmp(sub, "reboot/set")) {
        if (strcmp(p, "PRESS")) return;
        logm("reboot requested over MQTT");
        pub("availability", "offline", 1);
        sync();
        if (system("reboot") == -1) logm("reboot failed");
        return;
    } else return;
    poll_state(0);                                   /* echo the new state right away */
}

/* read and dispatch one packet (select said the socket is readable) */
static int mqtt_read(void)
{
    unsigned char t; unsigned long len = 0, mul = 1; unsigned char b; int i;
    unsigned char *body;
    if (recv_all(&t, 1)) return -1;
    for (i = 0; i < 4; i++) {
        if (recv_all(&b, 1)) return -1;
        len += (b & 127) * mul; mul *= 128;
        if (!(b & 128)) break;
    }
    if (len > 65536) { logm("packet too large (%lu)", len); return -1; }
    body = malloc(len + 1);
    if (!body) return -1;
    if (len && recv_all(body, len)) { free(body); return -1; }
    body[len] = 0;
    g_last_rx = now_s();
    if ((t >> 4) == 3 && len >= 2) {                 /* PUBLISH (QoS 0 only: we subscribe at QoS 0) */
        unsigned tl = (body[0] << 8) | body[1];
        int qos = (t >> 1) & 3;
        unsigned off = 2 + tl + (qos ? 2 : 0);
        if (2 + tl <= len && off <= len) {
            char topic[256];
            unsigned n = tl < sizeof topic - 1 ? tl : sizeof topic - 1;
            memcpy(topic, body + 2, n); topic[n] = 0;
            handle(topic, (const char *)body + off);
        }
    }
    free(body);                                       /* SUBACK / PINGRESP: nothing to do */
    return 0;
}

static void on_connect(void)
{
    char t[200];
    pub("availability", "online", 1);
    snprintf(t, sizeof t, "%s/+/set", g_base);
    mqtt_subscribe(t, 1);
    mqtt_subscribe(envd("MQTT_HA_STATUS", "homeassistant/status"), 2);
    publish_discovery();
    poll_state(1);
    publish_diag();
    publish_snapshot();
    publish_clips();
}

int main(void)
{
    char mac[32] = "", w[32];
    long next_poll = 0, next_diag = 0, next_snap = 0, next_clips = 0, backoff = 5;

    signal(SIGPIPE, SIG_IGN);                        /* ptz is backgrounded by sh ('&'): no zombies */
    setvbuf(stderr, NULL, _IOLBF, 0);

    g_host = getenv("MQTT_HOST");
    if (!g_host || !*g_host) { logm("MQTT_HOST not set: nothing to do"); return 1; }
    g_port = atoi(envd("MQTT_PORT", "1883"));
    g_user = getenv("MQTT_USER"); g_pass = getenv("MQTT_PASS");
    g_name = envd("MQTT_NAME", "Camera");
    g_disc = envd("MQTT_DISCOVERY", "homeassistant");
    g_snap = envd("SNAP_PATH", "/tmp/www/snapshot.jpg");
    g_snap_int = atoi(envd("MQTT_SNAP_INT", "60"));
    g_ptz_steps = atoi(envd("MQTT_PTZ_STEPS", "300"));
    g_rec = envd("MD_DIR", "/mnt/sdcard/rec");
    if (read_word("/etc/wlanname", g_wlan, sizeof g_wlan) <= 0) strcpy(g_wlan, "wlan0");
    g_es = !strcmp(envd("MQTT_LANG", "en"), "es");

    if (getenv("MQTT_ID") && *getenv("MQTT_ID")) snprintf(g_id, sizeof g_id, "%s", getenv("MQTT_ID"));
    else {
        char path[96], raw[32]; int i, j = 0;
        snprintf(path, sizeof path, "/sys/class/net/%s/address", g_wlan);
        read_word(path, raw, sizeof raw);
        for (i = 0; raw[i] && j < (int)sizeof mac - 1; i++) if (raw[i] != ':') mac[j++] = raw[i];
        mac[j] = 0;
        snprintf(g_id, sizeof g_id, "cam_%s", *mac ? mac : "unknown");
    }
    if (getenv("MQTT_BASE") && *getenv("MQTT_BASE")) snprintf(g_base, sizeof g_base, "%s", getenv("MQTT_BASE"));
    else snprintf(g_base, sizeof g_base, "decloud/%s", g_id);
    snprintf(g_avail, sizeof g_avail, "%s/availability", g_base);

    /* restore persisted controls before anything reads them */
    if (access(CAM "/etc/motion.off", F_OK) == 0) touch("/tmp/motion.off");
    if (read_word(CAM "/etc/daynight.mode", w, sizeof w) > 0) write_word("/tmp/daynight.mode", w);

    logm("starting: broker %s:%d, id %s, base %s", g_host, g_port, g_id, g_base);
    for (;;) {
        fd_set rf; struct timeval tv; long now;
        if (g_fd < 0) {
            if (mqtt_connect() != 0) { sleep((unsigned)backoff); if (backoff < 60) backoff *= 2; continue; }
            backoff = 5;
            on_connect();
            next_poll = now_s() + 1;
            next_diag = now_s() + 60;
            next_clips = now_s() + 600;              /* on_connect() just published them */
            next_snap = g_snap_int > 0 ? now_s() + g_snap_int : 0;
            if (g_fd < 0) continue;                  /* lost while announcing */
        }
        FD_ZERO(&rf); FD_SET(g_fd, &rf);
        tv.tv_sec = 1; tv.tv_usec = 0;
        if (select(g_fd + 1, &rf, NULL, NULL, &tv) > 0 && FD_ISSET(g_fd, &rf)) {
            if (mqtt_read() != 0) { logm("connection lost"); mqtt_close(); continue; }
        }
        now = now_s();
        if (now >= next_poll) { poll_state(0); next_poll = now + 1; }
        if (now >= next_diag) { publish_diag(); next_diag = now + 60; }
        if (next_snap && now >= next_snap) { publish_snapshot(); next_snap = now + g_snap_int; }
        /* new clip closed, or every 10 min to follow old clips pruned by MD_MINFREE/MD_KEEPDAYS */
        if ((g_clips_due && now >= g_clips_due) || now >= next_clips) {
            publish_clips(); g_clips_due = 0; next_clips = now + 600;
        }
        if (now - g_last_tx >= KEEPALIVE / 2) {
            unsigned char ping[2] = { 0xC0, 0 };
            if (send_all(ping, 2)) { logm("ping failed"); mqtt_close(); continue; }
        }
        if (g_fd >= 0 && now - g_last_rx > KEEPALIVE * 3 / 2) { logm("broker silent, reconnecting"); mqtt_close(); }
    }
    return 0;
}
