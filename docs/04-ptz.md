# PTZ — motor control

The board has two stepper motors (pan/tilt) handled by the stock **`ak_motor.ko`** driver, which
exposes two character devices:

- `/dev/motor0` → **pan** (horizontal)
- `/dev/motor1` → **tilt** (vertical)

## ioctls (verified against the vendor header)

Magic `'m'` (0x6d). An earlier pass reverse-engineered the right *numbers* but the wrong
*meaning* for two of these - see [docs/11-ptz-fix.md](11-ptz-fix.md) for the full story, this
table is the corrected version, matched against the real SDK header
(`os/driver/external/include/ak_motor.h`):

| Command | Value | Action |
|---|---|---|
| `MOTOR_MOVE_LIMIT`   | `0x40046d40` | move (signed steps: val>0 cw, val<0 ccw), respects end-stops |
| `MOTOR_MOVE_NOLIMIT` | `0x40046d41` | same, but **ignores** end-stops - not a direction toggle |
| `MOTOR_STOP`         | `0x40046d42` | stop |
| `MOTOR_GET_STATUS`   | `0x40046d43` | **reads status only** - does not move/home |

`MOTOR_MOVE_LIMIT` takes the signed step count directly; the sign *is* the direction. The
driver also only keeps the motor turning **while the controlling fd stays open** -
[`motorctl`](../src/tools/motorctl.c) holds it open (up to 30 s) on `move`, and `stop` kills
that holder (plus sends an explicit `MOTOR_STOP`). Details and the on-hardware verification:
[docs/11-ptz-fix.md](11-ptz-fix.md).

Implementation: [src/tools/motorctl.c](../src/tools/motorctl.c) — static ARM binary.

```
motorctl /dev/motor0 move 40     # pan + 40 steps
motorctl /dev/motor0 move -40    # pan - 40
motorctl /dev/motor1 move 40     # tilt
motorctl /dev/motor0 stop
motorctl /dev/motor0 home
```

## High-level script

[scripts/ptz](../scripts/ptz) wraps `motorctl` with per-axis signs and a "continuous" mode:

```
ptz left|right|up|down     # hold = move (big steps) until 'stop'
ptz stop
ptz home                   # center (home both axes)
```

If an axis moves the wrong way on your unit, flip `PAN_SIGN` / `TILT_SIGN` to `-1` in the script.
The web UI calls these via `cgi-bin/ptz.cgi`.

> The stock driver (`ak_motor.ko`) was chosen over GPIO bit-banging to avoid depending on the
> board's exact pinout.

---
*Produced with AI assistance; ioctls verified against the device driver.*
