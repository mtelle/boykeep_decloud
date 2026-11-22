import sys
# Add an automatic day/night thread to ak_rtsp_stream.c.
# Day/night is decided from the ISP luminance index (ak_vpss_get_cur_lumi, high = dark).
# Day -> night: lumi > DN_D2N (3584) for DN_D2N_S (10) consecutive seconds.
# Night -> day: in night mode the index is useless (night ISP profile + our own IR LEDs read ~9 in a
# pitch-dark room - lower than daylight ~100). The decision is an IR-off probe: the thread briefly
# turns the IR LEDs off and loads the day ISP profile, samples lumi for up to DN_PROBE_MS (4000) ms
# and goes to day only if the HIGHEST reading stays below DN_N2D (1536). Taking the maximum defeats AE
# lag (the first frames after IR-off still carry the IR-lit exposure); it stops early as soon as
# lumi > DN_D2N (clearly dark), so a night probe is usually a sub-second blip.
# WHEN to probe (the stock SoftPhotosensitive idea): under pure IR the sensor's R, G and B averages
# are nearly equal (rg = bg = 104 measured); visible light shifts that ratio (a warm lamp: bg -> 88).
# A shift of DN_COLOR (8) points from the colour learnt after entering night, sustained DN_COLOR_S
# (20) s, triggers a probe at once - so in real darkness there is no blip at all. A probe that finds
# it still dark re-learns the colour (a dim lamp probes once, not every 20 s). A fallback probe runs
# every DN_PROBE (1800) s in case some light does not shift the colour. DN_COLOR=0 disables the
# colour trigger (then set DN_PROBE=300); DN_PROBE=0 disables the timer. DN_HOLD (60) s minimum per state.
# While switching/probing /tmp/daynight.busy exists; the motion thread ignores motion meanwhile.
# On a switch: IR-cut pulse (GPIO 65/66), IR LED (GPIO 48), ISP day/night profile (ak_vi_switch_mode).
# Mode: /tmp/daynight.mode = auto | day | night (default auto). State in /tmp/daynight.state,
# trace in /tmp/daynight.log (last probe in it too). DN=0 in the environment disables it.
p = sys.argv[1]
s = open(p, encoding='latin-1').read()

func = r'''
/* ---- automatic day/night (added by anyka-ak3918ev300l-decloud) ---- */
struct dn_threshold {             /* = struct ak_auto_day_night_threshold of the vendor SDK */
    int day_to_night_lum, night_to_day_lum;
    int night_cnt[5], day_cnt[10];
    int lock_time, quick_switch_mode, day2night_sleep_time, night2day_sleep_time;
};
int ak_vpss_set_auto_day_night_param(int dev, void *param);
int ak_vpss_get_auto_day_night_level(int dev, int pre_ir_level, int *new_ir_level);
int ak_vpss_get_cur_lumi(int dev, int *lumi);
/* colour statistics the stock SoftPhotosensitive path reads for night->day (logged only, for now) */
int ak_vpss_get_rgb_average(int dev, unsigned int *r_avr, unsigned int *g_avr, unsigned int *b_avr);
int ak_vpss_get_ev(int dev, unsigned int *ev);
int ak_vpss_get_rgb_dis(int dev, unsigned int gain_r, int offset_r, unsigned int gain_b, int offset_b, int *dis);

/* Real board wiring (vendor rootfs/system/system.env, f6m05 board - the generic 17/18/56
   this started with were never actually right for this hardware, and 18 may double as the
   SoC reset line (system.env has GPIO_RESET=-18) - this stops pulsing that too. */
#define DN_IRCUT_NIGHT 66   /* GPIO_IR_NIGHT: pulse = filter out (night) */
#define DN_IRCUT_DAY   65   /* GPIO_IR_DAY: pulse = filter in (day) */
#define DN_IR_LED      48   /* GPIO_IR_LED: 1 = IR LEDs on (56 is GPIO_LED_RED, the status LED, not IR) */

static void dn_write(const char *path, const char *val)
{
    FILE *f = fopen(path, "w");
    if (f) { fputs(val, f); fclose(f); }
}
static void dn_gpio(int g, int v)
{
    char p[64];
    snprintf(p, sizeof(p), "/sys/class/gpio/gpio%d/value", g);
    dn_write(p, v ? "1" : "0");
}
static void dn_gpio_out(int g)
{
    char p[64], n[8];
    snprintf(p, sizeof(p), "/sys/class/gpio/gpio%d", g);
    if (access(p, F_OK) != 0) { snprintf(n, sizeof(n), "%d", g); dn_write("/sys/class/gpio/export", n); }
    snprintf(p, sizeof(p), "/sys/class/gpio/gpio%d/direction", g);
    dn_write(p, "low");
}
static void dn_apply(int night)
{
    if (night) {
        ak_vi_switch_mode(VIDEO_DEV0, VI_MODE_NIGHTTIME);
        dn_gpio(DN_IR_LED, 1);
        dn_gpio(DN_IRCUT_DAY, 0); dn_gpio(DN_IRCUT_NIGHT, 1);
        ak_sleep_ms(150);
        dn_gpio(DN_IRCUT_NIGHT, 0);
    } else {
        dn_gpio(DN_IRCUT_NIGHT, 0); dn_gpio(DN_IRCUT_DAY, 1);
        ak_sleep_ms(150);
        dn_gpio(DN_IRCUT_DAY, 0);
        dn_gpio(DN_IR_LED, 0);
        ak_vi_switch_mode(VIDEO_DEV0, VI_MODE_DAY_OUTDOOR);
    }
    dn_write("/tmp/daynight.state", night ? "night\n" : "day\n");
    printf("DAYNIGHT: -> %s\n", night ? "night" : "day");
}
/* The vendor SDK detector (ak_vpss_get_auto_day_night_level) never proposed night on this unit
   (rr=0 lvl=0 even at lumi=54575). The raw ISP luminance is decisive in day mode (day ~100,
   dark ~54000), but in night mode (night profile + IR on) a dark room reads ~9, so night->day
   needs an IR-off probe - see the header of patch_daynight.py. */
#include <time.h>
static int dn_env(const char *k, int d) { const char *v = getenv(k); return v ? atoi(v) : d; }
static long dn_now_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec; }
static void dn_busy(int on) { if (on) dn_write("/tmp/daynight.busy", "1\n"); else remove("/tmp/daynight.busy"); }
static void dn_switch(int night) { dn_busy(1); dn_apply(night); ak_sleep_ms(3000); dn_busy(0); }
/* IR off + day profile, return the highest lumi seen within ms (early exit above stop) */
static int dn_probe(int ms, int stop)
{
    int mx = -1, t;
    dn_busy(1);
    dn_gpio(DN_IR_LED, 0);
    ak_vi_switch_mode(VIDEO_DEV0, VI_MODE_DAY_OUTDOOR);
    for (t = 0; t < ms; t += 250) {
        ak_sleep_ms(250);
        int l = -1;
        ak_vpss_get_cur_lumi(VIDEO_DEV0, &l);
        if (l > mx) mx = l;
        if (mx > stop) break;
    }
    return mx;
}
static void *daynight_th(void *arg)
{
    (void)arg;
    ak_thread_set_name("daynight_th");
    dn_gpio_out(DN_IRCUT_NIGHT); dn_gpio_out(DN_IRCUT_DAY); dn_gpio_out(DN_IR_LED);
    dn_busy(0);

    int d2n = dn_env("DN_D2N", 3584), n2d = dn_env("DN_N2D", 1536);
    int d2n_s = dn_env("DN_D2N_S", 10), hold = dn_env("DN_HOLD", 60);
    int probe_s = dn_env("DN_PROBE", 1800), probe_ms = dn_env("DN_PROBE_MS", 4000);
    int col_th = dn_env("DN_COLOR", 8), col_s = dn_env("DN_COLOR_S", 20);
    printf("DAYNIGHT: lumi d2n=%d/%ds n2d=%d probe=%ds/%dms color=%d/%ds hold=%ds\n",
           d2n, d2n_s, n2d, probe_s, probe_ms, col_th, col_s, hold);

    int cur = -1, cnt = 0, last_probe = -1;
    int base_rg = -1, base_bg = -1, ccnt = 0;
    long last_sw = -100000, last_pt = dn_now_s(), base_at = 0;
    ak_sleep_ms(5000);                /* let the AE settle before the first decision */
    for (;;) {
        char mode[16] = "auto";
        FILE *f = fopen("/tmp/daynight.mode", "r");
        if (f) { if (!fgets(mode, sizeof(mode), f)) strcpy(mode, "auto"); fclose(f); }
        long now = dn_now_s();
        int lumi = -1;
        ak_vpss_get_cur_lumi(VIDEO_DEV0, &lumi);

        /* Colour signature (the stock SoftPhotosensitive idea): under pure IR the R, G and B
           channels respond almost equally (measured rg = bg = 104); visible light shifts them
           (a warm lamp: bg 104 -> 88). In night state we learn the current colour as a baseline
           once things settle, and a sustained shift of DN_COLOR points for DN_COLOR_S s triggers
           an IR-off probe right away. The probe still decides: a dim lamp shifts the colour but
           the probe sees it is still dark - then the baseline is re-learnt with that lamp on, so
           it does not probe again until the light changes once more. */
        unsigned int ra = 0, ga = 0, ba = 0;
        ak_vpss_get_rgb_average(VIDEO_DEV0, &ra, &ga, &ba);
        int rg = ga ? (int)(ra * 100 / ga) : -1, bg = ga ? (int)(ba * 100 / ga) : -1, dev = 0;
        if (cur == 1 && rg >= 0) {
            if (base_rg < 0 && now >= base_at) { base_rg = rg; base_bg = bg; ccnt = 0; }
            if (base_rg >= 0) {
                int a = abs(rg - base_rg), b = abs(bg - base_bg);
                dev = a > b ? a : b;
            }
        }

        int want = cur;
        if (!strncmp(mode, "day", 3))        { want = 0; cnt = 0; }
        else if (!strncmp(mode, "night", 5)) { want = 1; cnt = 0; }
        else if (cur < 0) {
            if (lumi >= 0) want = lumi > d2n ? 1 : 0;
        } else if (cur == 0) {
            cnt = (lumi > d2n) ? cnt + 1 : 0;
            if (cnt >= d2n_s && now - last_sw >= hold) want = 1;
        } else {
            ccnt = (col_th > 0 && dev >= col_th) ? ccnt + 1 : 0;
            int by_color = col_th > 0 && ccnt >= col_s;
            int by_timer = probe_s > 0 && now - last_pt >= probe_s;
            if ((by_color || by_timer) && now - last_sw >= hold) {
                last_pt = now;
                last_probe = dn_probe(probe_ms, d2n);
                printf("DAYNIGHT: probe (%s, rg/bg %d/%d base %d/%d) max=%d (n2d %d)\n",
                       by_color ? "color" : "timer", rg, bg, base_rg, base_bg, last_probe, n2d);
                if (last_probe >= 0 && last_probe < n2d) want = 0;
                else {                        /* still dark: restore night, re-learn the colour */
                    ak_vi_switch_mode(VIDEO_DEV0, VI_MODE_NIGHTTIME);
                    dn_gpio(DN_IR_LED, 1);
                    ak_sleep_ms(3000);
                    dn_busy(0);
                    base_rg = -1; base_at = dn_now_s() + 5; ccnt = 0;
                }
            }
        }
        if (want != cur && want >= 0) {
            int first = cur < 0;          /* the boot-time decision does not start DN_HOLD */
            dn_switch(want); cur = want; cnt = 0; last_sw = last_pt = dn_now_s();
            base_rg = -1; base_at = last_sw + 10; ccnt = 0;
            if (first) last_sw = -100000;
        }
        unsigned int ev = 0;
        ak_vpss_get_ev(VIDEO_DEV0, &ev);
        char line[224];
        snprintf(line, sizeof(line), "mode=%s state=%s lumi=%d cnt=%d probe=%d next=%lds"
                 " rg=%d bg=%d base=%d/%d dev=%d ccnt=%d ev=%u\n",
                 strtok(mode, "\r\n") ? mode : "auto", cur == 1 ? "night" : "day", lumi, cnt, last_probe,
                 cur == 1 && probe_s > 0 ? probe_s - (dn_now_s() - last_pt) : 0L,
                 rg, bg, base_rg, base_bg, dev, ccnt, ev);
        dn_write("/tmp/daynight.log", line);
        ak_sleep_ms(1000);
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
    '    if (!getenv("DN") || atoi(getenv("DN")) != 0) {\n'
    '        ak_pthread_t dn_tid;\n'
    '        ak_thread_create(&dn_tid, daynight_th, NULL, ANYKA_THREAD_MIN_STACK_SIZE, 19);\n'
    '    }\n', 1)

open(p, 'w', encoding='latin-1').write(s)
print("patch daynight OK")
