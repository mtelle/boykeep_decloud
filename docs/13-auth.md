# Authentication: web UI (works) and RTSP (doesn't)

File-gated login for the web UI and an attempt at the same for the RTSP stream, both driven by
one optional file: `etc/auth.env` (not in git - copy `deploy/auth.env.example` and fill it in
yourself). Leaving it absent, or any value in it blank, keeps that part exactly as open as it was
before this feature existed - nothing breaks if you never touch it.

## 1. Web UI - works

`start.sh` sources `etc/auth.env` right after `config.env`. If both `WEB_USER` and `WEB_PASS` are
set, it writes a one-line `busybox httpd` auth config to `/tmp/httpd.conf`:

```
/:WEB_USER:WEB_PASS
```

and launches `httpd -c /tmp/httpd.conf` instead of `-c /dev/null`. That single `/:user:pass` line
protects the whole site - `/`, `/cgi-bin/*`, recordings, `player.html`, `config.html`, everything -
with HTTP Basic auth. `camwatch.sh` reuses the same generated file if it ever has to restart
`httpd` (it does not regenerate it - that only happens once, at boot, from `start.sh`).

Confirmed working on hardware: visiting the camera's IP triggers the browser's native
username/password prompt, and once entered, the browser's own HTTP-auth cache carries those
credentials to every subsequent request on the page (buttons, the recordings list, video
playback) automatically.

**Known footgun:** embedding credentials in the URL itself
(`http://user:pass@CAMERA_IP/`) only authenticates that one page load - modern browsers do **not**
reuse URL-embedded credentials for the page's own background `fetch()` calls, so buttons and the
recordings list will fail with 401 even though the page itself loaded. Always let the native
browser login popup handle it (just visit `http://CAMERA_IP/` with no credentials in the URL).

## 2. RTSP - confirmed NOT to work

`src/rtsp-patches/patch_rtsp_auth.py` wires `RTSP_USER`/`RTSP_PASS` into
`rtsp_server_init_param(rtsp_init_param*)`, the one function the SDK exposes for this
(`platform/sample/rtsp/ak_rtsp_server.h`). `librtsp_server.so` genuinely has Digest-auth code
inside it (`MD5Auth_DigestCalcHA1/HA2/Response`, a literal `WWW-Authenticate: Digest realm="IP
Camera(AC813)"` string) - the vendor's own sample just always called this function with `NULL`,
leaving it permanently off.

Wiring it up does **not** actually restrict access to `vs0`/`vs1`, confirmed on hardware:

- The build log and `/tmp/rtsp.log` both confirm the credentials were read from the environment
  and the struct was built and passed to `rtsp_server_init_param()`.
- VLC/`ffplay` still connect and play `vs0`/`vs1` with **zero** credentials, every time.

One real lead was chased and ruled out: `ak_rtsp_server.h`'s own (translated) field comments say
its two URL-prefix fields are named backwards from what they do -

```c
char cRecordUrlPrefix[16];      // comment: "live stream url prefix"
char cLiveStearmUrlPrefix[16];  // comment: "recording url prefix"
```

The first attempt set `cLiveStearmUrlPrefix = "vs"` (matching its *name*, for the live channels).
Swapping to `cRecordUrlPrefix = "vs"` (matching its *actual documented behaviour*) made no
difference. Setting **both** fields to `"vs"` at once also made no difference. In all three cases
`vs0`/`vs1` stayed completely open.

**Conclusion:** the Digest-auth code is present in this `librtsp_server.so` build but does not
appear to be wired to the live-stream request handler at all - there is no other auth-related
function in the SDK header to try. Without ARM disassembly of the closed `.so` (not attempted -
same situation as the H.264 investigation in
[docs/05-video-pipeline.md](05-video-pipeline.md)), this is as far as it goes.

The code is left in place (harmless - it's a no-op unless `RTSP_USER`/`RTSP_PASS` are set, and even
then it doesn't restrict anything, it just doesn't help either) in case a different
`librtsp_server.so` build ever surfaces that actually honours it. **Do not rely on it**: if you
want the RTSP stream itself protected, do it at the network level - keep port 554 off the public
internet and off any untrusted network/VLAN, same as you should for the web UI's port 80 even with
`WEB_USER`/`WEB_PASS` set.

## 3. ONVIF - works, with its own credentials

ONVIF clients (Home Assistant's ONVIF integration, ONVIF Device Manager, NVRs) do not send HTTP
Basic auth. They put the user and password inside the SOAP request (WS-Security
UsernameToken). With `WEB_USER`/`WEB_PASS` set, busybox httpd's `/:user:pass` rule therefore
answered every ONVIF request with **401**, even with the right credentials typed in HA.

Fix:

- [`src/tools/patch_busybox_httpd.py`](../src/tools/patch_busybox_httpd.py) makes httpd skip HTTP
  auth for `/onvif/*` only (the rest of the web stays protected).
- `start.sh` writes `user=`/`password=` into `/tmp/onvif.conf`, so `onvif_simple_server` checks
  the WS-Security credentials itself. They are the web ones, unless `ONVIF_USER`/`ONVIF_PASS` are
  set in `auth.env`.

Both parts are needed. The new busybox without credentials in `onvif.conf` would leave ONVIF
open. Credentials without the new busybox still give 401. With no `WEB_USER`/`WEB_PASS`, nothing
changes: ONVIF stays open, like the rest.
