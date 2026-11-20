# Home Assistant integration

Live view already works in HA via RTSP/ONVIF out of the box. This page covers the rest: a motion
binary_sensor (and the automation trigger that comes with it for free), a recordings list on a
Lovelace card, and single-tap PTZ buttons.

No new camera-side code was needed for motion/recordings - [`rec.cgi`](../src/web/cgi-bin/rec.cgi)
(built for the web UI) already returns exactly what both need: `recording` (bool) and `clips`
(day/file/url/size array). Note: by default `rec.cgi` only returns *today*'s clips, paginated,
for older days, a separate `?list=days` call (also paginated) lists the day-folders - that's what the
web UI's own day-folder view uses, to avoid ever sending the whole history at once. For the full
flat list (what this integration wants), call it with `?all=1` instead, as below.

## 1. Motion sensor + recordings list

`configuration.yaml`:

```yaml
binary_sensor:
  - platform: rest
    name: "Camera Motion"
    unique_id: camera_motion
    resource: http://CAMERA_IP/cgi-bin/rec.cgi?all=1
    value_template: "{{ value_json.recording }}"
    device_class: motion
    scan_interval: 5
    timeout: 5

sensor:
  - platform: rest
    name: "Camera Recordings"
    unique_id: camera_recordings
    resource: http://CAMERA_IP/cgi-bin/rec.cgi?all=1
    value_template: "{{ value_json.clips | length }}"
    json_attributes:
      - clips
    scan_interval: 30
    timeout: 5
```

Replace `CAMERA_IP` (a DHCP reservation on the router avoids ever having to touch this again).
`binary_sensor.camera_motion` turns `on` while a clip is recording - that includes the `MD_HOLD`
tail, so it behaves like a normal retriggering motion sensor, not an instant single-frame pulse.

> If the sensor looks stuck `on` with nothing moving, the camera is probably over-sensitive for
> its lighting, not Home Assistant - see the tuning note in
> [docs/03-motion-recording.md](03-motion-recording.md#tuning-a-static-scene-can-still-trigger-constantly).

## 2. Motion-detected automation

A state trigger on the binary_sensor *is* the "motion detected" event - add whatever actions you
want:

```yaml
automation:
  - alias: "Camera: motion detected"
    id: camera_motion_detected
    trigger:
      - platform: state
        entity_id: binary_sensor.camera_motion
        to: "on"
    action:
      - service: persistent_notification.create
        data:
          title: "Motion detected"
          message: "The camera detected motion."
      # - service: notify.mobile_app_yourphone
      #   data: { message: "Motion at the camera" }
```

## 3. Lovelace card

```yaml
type: vertical-stack
cards:
  - type: picture-entity
    entity: camera.your_camera_entity   # <- your existing camera entity
    camera_view: live
    show_state: true
  - type: entity
    entity: binary_sensor.camera_motion
    name: Motion
  - type: markdown
    title: Recent recordings
    content: >
      {% set clips = state_attr('sensor.camera_recordings', 'clips') %}
      {% if clips %}
      {% for c in clips[:10] %}
      - **{{ c.file }}** ({{ (c.size / 1048576) | round(1) }} MB) — [▶ View](http://CAMERA_IP/player.html?url={{ c.url | urlencode }})
      {% endfor %}
      {% else %}
      No recordings yet.
      {% endif %}
```

Clicking a clip's raw `.mp4` URL directly tends to make the browser download it instead of
playing it inline (depends on the webview/browser; HA's own frontend is one of the places
this happens) - [`player.html`](../src/web/player.html) is a tiny standalone page on the
camera itself that just wraps the clip in a `<video>` tag, so a plain link to it always plays
inline instead. That's what the markdown card above links to
(`player.html?url=/rec/<day>/<file>.mp4`) rather than the raw clip URL.

## 4. PTZ: single-tap buttons

The web UI's PTZ is "hold = move, release = stop" (`pointerdown`/`pointerup` in
[`src/web/index.html`](../src/web/index.html)), which a plain HA button can't do - it only has a
single tap, no "release" event. [`src/web/cgi-bin/ptz.cgi`](../src/web/cgi-bin/ptz.cgi) now also
accepts an optional `steps=N` query parameter for a one-shot "nudge": with a small step count the
motor naturally stops once it finishes those steps (see
[docs/11-ptz-fix.md](11-ptz-fix.md#the-fix-hold-the-fd-open-let-stop-kill-the-holder) for why that
works without an explicit follow-up `stop` call), which is exactly what a single tap needs.

```yaml
rest_command:
  camera_ptz_left:
    url: "http://CAMERA_IP/cgi-bin/ptz.cgi?dir=left&steps=300"
  camera_ptz_right:
    url: "http://CAMERA_IP/cgi-bin/ptz.cgi?dir=right&steps=300"
  camera_ptz_up:
    url: "http://CAMERA_IP/cgi-bin/ptz.cgi?dir=up&steps=300"
  camera_ptz_down:
    url: "http://CAMERA_IP/cgi-bin/ptz.cgi?dir=down&steps=300"
  camera_ptz_home:
    url: "http://CAMERA_IP/cgi-bin/ptz.cgi?dir=home"
```

Lovelace (a simple button grid; swap for a `grid` card or icons as you like):

```yaml
type: grid
columns: 3
cards:
  - type: button
    show_name: false
    icon: mdi:blank
  - type: button
    icon: mdi:arrow-up-bold
    tap_action: { action: call-service, service: rest_command.camera_ptz_up }
  - type: button
    show_name: false
    icon: mdi:blank
  - type: button
    icon: mdi:arrow-left-bold
    tap_action: { action: call-service, service: rest_command.camera_ptz_left }
  - type: button
    icon: mdi:home
    tap_action: { action: call-service, service: rest_command.camera_ptz_home }
  - type: button
    icon: mdi:arrow-right-bold
    tap_action: { action: call-service, service: rest_command.camera_ptz_right }
  - type: button
    show_name: false
    icon: mdi:blank
  - type: button
    icon: mdi:arrow-down-bold
    tap_action: { action: call-service, service: rest_command.camera_ptz_down }
  - type: button
    show_name: false
    icon: mdi:blank
```

`steps=300` is a starting point - raise it for a bigger nudge per tap, lower it for finer control.
The web UI's own hold-to-move buttons are unaffected: they call `ptz.cgi` without `steps`, which
keeps the original continuous-until-`stop` behaviour.
