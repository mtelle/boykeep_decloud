import sys
# Add an automatic day/night thread to ak_rtsp_stream.c.
# Day/night is decided from the ISP luminance index (ak_vpss_get_cur_lumi, high = dark) with plain
# hysteresis and no IR-off probing: day->night when lumi > DN_D2N (3584) for DN_D2N_S (10) s,
# night->day when lumi < DN_N2D (1536) for DN_N2D_S (30) s, minimum DN_HOLD (60) s per state.
# Measured on this unit: daylight ~100, covered ~54000, dark room lit by our own IR ~10700 - so the IR
# LEDs never pull a dark scene below DN_N2D and no probing is needed. On a switch: IR-cut pulse
# (GPIO 65/66), IR LED (GPIO 48), ISP day/night profile (ak_vi_switch_mode).
# Mode: /tmp/daynight.mode = auto | day | night (default auto). State in /tmp/daynight.state,
# trace in /tmp/daynight.log. DN=0 in the environment disables it.
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
   (rr=0 lvl=0 even at lumi=54575), and the old IR-off probe gave transient false "day" readings.
   The raw ISP luminance is decisive on its own (day ~100, dark+IR ~10700, covered ~54000), so a
   plain sustained-threshold hysteresis is enough. */
static int dn_env(const char *k, int d) { const char *v = getenv(k); return v ? atoi(v) : d; }
static void *daynight_th(void *arg)
{
    (void)arg;
    ak_thread_set_name("daynight_th");
    dn_gpio_out(DN_IRCUT_NIGHT); dn_gpio_out(DN_IRCUT_DAY); dn_gpio_out(DN_IR_LED);

    int d2n = dn_env("DN_D2N", 3584), n2d = dn_env("DN_N2D", 1536);
    int d2n_s = dn_env("DN_D2N_S", 10), n2d_s = dn_env("DN_N2D_S", 30), hold = dn_env("DN_HOLD", 60);
    printf("DAYNIGHT: lumi d2n=%d/%ds n2d=%d/%ds hold=%ds\n", d2n, d2n_s, n2d, n2d_s, hold);

    int cur = -1, cnt = 0, tick = 0, last_sw = -100000;
    ak_sleep_ms(5000);                /* let the AE settle before the first decision */
    for (;;) {
        char mode[16] = "auto";
        FILE *f = fopen("/tmp/daynight.mode", "r");
        if (f) { if (!fgets(mode, sizeof(mode), f)) strcpy(mode, "auto"); fclose(f); }
        int lumi = -1;
        ak_vpss_get_cur_lumi(VIDEO_DEV0, &lumi);
        int want = cur;
        if (!strncmp(mode, "day", 3))        { want = 0; cnt = 0; }
        else if (!strncmp(mode, "night", 5)) { want = 1; cnt = 0; }
        else if (lumi >= 0) {
            if (cur < 0) want = lumi > d2n ? 1 : 0;
            else {
                int toward = (cur == 0) ? (lumi > d2n) : (lumi < n2d);
                cnt = toward ? cnt + 1 : 0;
                if (cnt >= (cur == 0 ? d2n_s : n2d_s) && tick - last_sw >= hold) want = !cur;
            }
        }
        if (want != cur && want >= 0) { dn_apply(want); cur = want; cnt = 0; last_sw = tick; }
        if (++tick % 2 == 0) {
            char line[96];
            snprintf(line, sizeof(line), "mode=%s state=%s lumi=%d cnt=%d\n", strtok(mode, "\r\n") ? mode : "auto",
                     cur == 1 ? "night" : "day", lumi, cnt);
            dn_write("/tmp/daynight.log", line);
        }
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
