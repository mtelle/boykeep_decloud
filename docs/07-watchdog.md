# Watchdog / service supervision

De-clouded, nothing from the stock system keeps the custom stack alive, so the project ships its
own supervisor: [`scripts/camwatch.sh`](../scripts/camwatch.sh), launched by `start.sh`.

## What it does

Every `WD_INT` seconds (default 10) it checks the critical services and **restarts any that
died**:

| Service | Check | Restart |
|---|---|---|
| `rtspd` | `pidof rtspd` | relaunch with the stock libs and `rtsp.ini` |
| `httpd` | `pidof httpd` | relaunch on port 80 from `/tmp/www` |
| `dropbear` (SSH) | `pidof dropbear` | regenerate host key if needed, relaunch |
| `wsd_simple_server` (ONVIF discovery) | `pidof` | relaunch (if present) |

It also writes a **health snapshot** to `/tmp/watchdog.status` and logs actions to
`/tmp/watchdog.log`. The web UI reads the snapshot via `/cgi-bin/health.cgi` and shows a
🟢/🔴 line with free SD space.

> Note: `rtspd` already has internal recovery for the encoder; `camwatch` covers the case where
> the whole process dies, plus the other daemons that previously had **no** supervision.

## Levels of protection

1. **Process crash** (a daemon dies) → `camwatch` restarts it within `WD_INT` s. **On by default.**
2. **Total system hang** (kernel lockup, WiFi driver deadlock) → a process-level supervisor cannot
   help. For this there is the **SoC hardware watchdog** (`/dev/watchdog`): if it is not fed, the
   chip reboots the board. `camwatch` feeds it **only while its own loop is running**, so a real
   hang stops the feed and forces a reboot.

### Enabling the hardware watchdog (opt-in)

Off by default to avoid reboot loops. To enable, export before `start.sh` (or edit `start.sh`):

```sh
export WATCHDOG_HW=1
```

`camwatch` opens `/dev/watchdog`, feeds it every loop, and logs `HW watchdog armed`.

> ⚠️ Test it on the bench first. The kernel's default watchdog timeout varies by build; if it is
> shorter than `WD_INT`, lower `WD_INT` (e.g. `export WD_INT=3`). If the board enters a reboot
> loop, disable it (`WATCHDOG_HW=0`) and rely on software supervision only.

## Check it

```sh
cat /tmp/watchdog.status      # live snapshot (services up/DOWN, recording, SD free, uptime)
cat /tmp/watchdog.log         # restart history
# simulate a crash:
killall httpd ; sleep 12 ; pidof httpd && echo "httpd was restarted"
```

In the browser, the line under the live view shows 🟢/🔴 per service and free SD space, refreshed
every 10 s.

## Tunables

| Var | Default | Purpose |
|---|---|---|
| `WD_INT` | 10 | Check interval (seconds). |
| `WATCHDOG_HW` | 0 | `1` arms the SoC hardware watchdog (hang → reboot). |

---
*Produced with AI assistance.*
