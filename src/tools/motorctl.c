/* motorctl.c - pan/tilt control for Anyka AK3918 ak_motor.ko
 *
 * ioctl numbers verified against the REAL SDK header
 * (ANYKA_130L_SDK/os/driver/external/include/ak_motor.h), not reverse-engineered guesses.
 * Devices: /dev/motor0 and /dev/motor1  (ioctl magic 'm' = 0x6d).
 *
 * FIX 1 (2026-11-19): nr=0x41 ("MOTOR_MOVE_B"/direction toggle in the old code) is really
 * MOTOR_MOVE_NOLIMIT, a different command (ignores end-stops) - not used here. There is only
 * ONE real "move" ioctl for normal use: MOTOR_MOVE_LIMIT (nr=0x40), and it takes the signed
 * step count directly - the sign IS the direction (val>0 cw, val<0 ccw).
 *
 * FIX 2 (2026-11-19): confirmed on-device that the driver only keeps moving the motor WHILE
 * the device fd stays open - closing it (which happens almost instantly if you open, ioctl,
 * close) stops the motor after just a few steps, no matter how many steps were requested.
 * (move 1000 with fd closed immediately -> pos moved +4; with fd held open 3s -> pos moved the
 * full +1000.) So "move" now writes its own pid to /tmp/motorctl_<dev>.pid and keeps the fd
 * open (sleeping) for up to MAX_HOLD_S seconds - letting press-and-hold work - and "stop"
 * kills that holder (closing its fd, which halts the motor) in addition to sending the
 * explicit MOTOR_STOP ioctl itself.
 *
 * Also note: nr=0x43 ("MOTOR_HOME" in the old code) is actually MOTOR_GET_STATUS in the real
 * driver - it reads status, it does not move the motor anywhere. The real "go to a known
 * position" command would be MOTOR_RESET (nr=0x60); not wired up here yet.
 *
 * Build (static, so no uClibc version dependency on the camera):
 *   arm-unknown-linux-uclibcgnueabi-gcc -march=armv5te -mtune=arm926ej-s -static -O2 -o motorctl motorctl.c
 *
 * Usage:
 *   motorctl /dev/motor0 move  4000   # start moving clockwise, keeps going while held open
 *   motorctl /dev/motor0 move -4000   # start moving anticlockwise
 *   motorctl /dev/motor0 stop         # kill the holder process for THIS device + send MOTOR_STOP
 *   motorctl /dev/motor0 home         # reads status (NOT a real home - see note above)
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <signal.h>
#include <sys/ioctl.h>

/* ioctl command codes, verified against ak_motor.h (type 'm'=0x6d, size 4 = sizeof(int)) */
#define MOTOR_MOVE_LIMIT    0x40046d40UL   /* arg: int steps (signed; val>0 cw, val<0 ccw), respects end-stops */
#define MOTOR_MOVE_NOLIMIT  0x40046d41UL   /* same signed semantics, but IGNORES end-stops - not used here */
#define MOTOR_STOP          0x40046d42UL   /* stop */
#define MOTOR_GET_STATUS    0x40046d43UL   /* arg: int[8] -> read status (does NOT move/home) */

#define MAX_HOLD_S 30   /* safety cap: never keep moving longer than this even if "stop" never arrives */

static void pidfile_path(char *buf, size_t n, const char *dev)
{
    const char *base = strrchr(dev, '/');
    base = base ? base + 1 : dev;
    snprintf(buf, n, "/tmp/motorctl_%s.pid", base);
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr,
            "usage: %s <dev> <cmd> [steps]\n"
            "  dev : /dev/motor0 | /dev/motor1\n"
            "  cmd : move <steps(+/-)> | stop | home\n", argv[0]);
        return 2;
    }

    char pidpath[80];
    pidfile_path(pidpath, sizeof(pidpath), argv[1]);

    int ret = 0;

    if (!strcmp(argv[2], "stop")) {
        /* 1) kill whoever is holding this device's fd open (closing it halts the motor) */
        FILE *pf = fopen(pidpath, "r");
        if (pf) {
            int pid = 0;
            if (fscanf(pf, "%d", &pid) == 1 && pid > 0) kill(pid, SIGKILL);
            fclose(pf);
            remove(pidpath);
        }
        /* 2) also send the explicit stop ioctl via a fresh open, belt-and-suspenders */
        int fd = open(argv[1], O_RDWR);
        if (fd < 0) { perror("open"); return 1; }
        int dummy = 0;
        ret = ioctl(fd, MOTOR_STOP, &dummy);
        if (ret < 0) perror("ioctl");
        close(fd);
        return ret ? 1 : 0;
    }

    int fd = open(argv[1], O_RDWR);
    if (fd < 0) { perror("open"); return 1; }

    if (!strcmp(argv[2], "move")) {
        int steps = (argc > 3) ? atoi(argv[3]) : 0;
        ret = ioctl(fd, MOTOR_MOVE_LIMIT, &steps);   /* sign IS the direction */
        if (ret == 0) {
            FILE *pf = fopen(pidpath, "w");
            if (pf) { fprintf(pf, "%d\n", (int)getpid()); fclose(pf); }
            sleep(MAX_HOLD_S);   /* keep fd open -> motor keeps moving until "stop" kills us */
        }
    } else if (!strcmp(argv[2], "home")) {
        int status[8];
        memset(status, 0, sizeof(status));
        ret = ioctl(fd, MOTOR_GET_STATUS, status);
        if (ret == 0)
            printf("status: %d %d %d %d %d %d %d %d\n",
                   status[0], status[1], status[2], status[3],
                   status[4], status[5], status[6], status[7]);
    } else {
        fprintf(stderr, "unknown cmd: %s\n", argv[2]);
        close(fd);
        return 2;
    }

    if (ret < 0) perror("ioctl");
    close(fd);
    return ret ? 1 : 0;
}
