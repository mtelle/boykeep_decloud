# PTZ fix: real `ak_motor.ko` ioctls, and why the motor needs the fd held open

An earlier version of [`src/tools/motorctl.c`](../src/tools/motorctl.c) had its ioctl numbers
right but their **meaning** wrong, which silently broke two of the four PTZ buttons (the two that
asked for negative step counts - "left" and "down" in the default wiring). Comparing against the
real vendor header
(`ANYKA_*_SDK/os/driver/external/include/ak_motor.h`, not shipped in this repo - supply it from
your own SDK copy) found the actual bug.

## What the header actually says

```c
#define MOTOR_MOVE_LIMIT    _IOW('m', 0x40, int)  /* clockwise: val>0, anticlockwise: val<0 */
#define MOTOR_MOVE_NOLIMIT  _IOW('m', 0x41, int)  /* same, but ignores the end-stops         */
#define MOTOR_STOP          _IOW('m', 0x42, int)
#define MOTOR_GET_STATUS    _IOW('m', 0x43, int)  /* reads status - does NOT move anything   */
```

There is only **one** normal-use "move" command, `MOTOR_MOVE_LIMIT`, and it takes a **signed**
step count - the sign *is* the direction. The previous code treated `nr=0x41` as "direction B",
calling it with an always-**positive** magnitude whenever the caller asked for a negative count.
That command is not a direction toggle at all; it is a *different* command (ignore the end-stops),
and it still wants a signed value. Positive-direction presses worked by accident (they used the
correct ioctl with the correct sign); negative-direction presses used the wrong ioctl with the
wrong sign and the motor never responded.

Also worth knowing: `nr=0x43` (called `MOTOR_HOME` in the old code) is really `MOTOR_GET_STATUS`
- it only *reads* an 8-int status array, it has never moved the motor anywhere. The real
"go to a known position" command would be `MOTOR_RESET` (`nr=0x60`), not wired up yet.

**Fix:** always call `MOTOR_MOVE_LIMIT`, pass the signed step count straight through.

## The motor only turns while the device stays open

Even with the right ioctl, a second bug remained: `move 1000` barely moved the camera, *and the
distance did not depend on the requested step count at all*. Reading the raw status
(`motor_message`: `status, pos, speed_step, speed_angle, steps_one_circle, total_steps, ...`)
before and after, with the old open→ioctl→close-immediately code, showed `pos` advancing by only
**4** steps for *any* requested count (1000, 4000, 50000 - always ~4). Making the process sleep 3
seconds between the ioctl and `close(fd)` changed that completely: the same `move 1000` then moved
the full 1000 steps. **The driver keeps the motor running only while the controlling fd is open**
- closing it (which happens in microseconds in open→ioctl→close) stops it almost immediately,
regardless of how many steps were requested.

### The fix: hold the fd open, let `stop` kill the holder

`move` now:
1. Issues the signed `MOTOR_MOVE_LIMIT` ioctl.
2. Writes its own PID to `/tmp/motorctl_<dev>.pid`.
3. Sleeps up to `MAX_HOLD_S` (30 s) with the fd **open** - the motor keeps turning for as long as
   this process is alive, up to that safety cap.

`stop` now:
1. Reads `/tmp/motorctl_<dev>.pid` and `kill`s that process - closing its fd, which halts the
   motor.
2. Also sends the explicit `MOTOR_STOP` ioctl via its own fresh `fd`, belt-and-suspenders.

This matches the web UI's existing "press and hold = move, release = stop" design
([`scripts/ptz`](../scripts/ptz) already backgrounds `motorctl ... move $BIG &` on press and calls
`motorctl ... stop` on release) - it just needed the holder/killer mechanics on the C side to
actually make that design work. Verified on hardware: `move 4000 &` for a timed 2 s, then `stop`,
moved ~690 steps (expected at `speed_step=350` Hz: `350 × 2 ≈ 700`) and left `status=0` (stopped)
with only a handful of residual steps from signal-delivery latency.
