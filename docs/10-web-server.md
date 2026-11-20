# Web server: a standalone static BusyBox `httpd`

## Why

The stock busybox on this board was built **without** the `httpd` applet (`httpd: not found`),
and nothing else in the firmware serves HTTP. No other change to the project needed this more:
the web UI (live snapshot, PTZ, recordings, config page) is entirely CGI + static files served
over HTTP.

## What ships

[`scripts/build_httpd.sh`](../scripts/build_httpd.sh) cross-compiles BusyBox 1.36.1 statically
for this board (ARMv5TE/uClibc, same toolchain and chip flags as `rtspd`) with a plain
`defconfig`. No extra configuration was needed for the two features this project actually
depends on - both are **on by default** in that version (confirmed against the `//config:`
comments in `networking/httpd.c`):

- `FEATURE_HTTPD_CGI` - runs anything under `cgi-bin/` (PTZ control, config save/apply, health).
- `FEATURE_HTTPD_RANGES` - emits `Accept-Ranges`/`Content-Range`, so recorded `.mp4` clips can be
  seeked in the browser instead of only played from the start. Verified directly:
  ```
  curl -s -D- -o /dev/null -r 0-4 http://IP/some.mp4
  → HTTP/1.1 206 Partial Content
  → Content-Range: bytes 0-4/<size>
  → Accept-Ranges: bytes
  ```

The output is shipped to the camera as `cam/bin/busybox` and launched explicitly:

```sh
$CAM/bin/busybox httpd -p 80 -h /tmp/www -c /dev/null &
```

## The one gotcha: it must be named exactly `busybox`

BusyBox's multi-call dispatcher (`libbb/appletlib.c`) only falls back to "treat `argv[1]` as the
applet name" when the executable's own basename is **exactly** `"busybox"` - that check is
hardcoded, it is not a generic "unrecognised name → try argv[1]" fallback. Ship the same binary
under any other name (this was tried as `bb_web`, to avoid any appearance of colliding with the
system's own `/bin/busybox`) and *every* applet invocation fails, even trivial ones:

```
$ ./bb_web echo hi
bb_web: applet not found
$ ./busybox echo hi
hi
```

So the binary is named plain `busybox` and lives at `cam/bin/busybox`, always invoked by its full
path. This never collides with the real system `/bin/busybox` because a full-path invocation never
goes through `$PATH` resolution.

## `camwatch.sh`

The supervisor's `alive()` check (`pidof httpd` first, falling back to a `ps`-line grep for
`[ /]httpd`) still matches correctly even though the real process name is `busybox`: this
firmware's `ps` prints the **full command line**, so `busybox httpd -p 80 ...` still contains the
substring `" httpd"` that the fallback grep looks for. `start_httpd()` was updated to call
`$CAM/bin/busybox httpd ...` instead of the non-existent bare `httpd`.
