#!/bin/sh
# PTZ control for the web UI -> calls the motorctl 'ptz' script. LAN only.
# ?steps=N (optional): a one-shot "nudge" of N motor steps instead of the continuous
# hold-to-move used by the web UI. For clients that can only do a single tap (Home
# Assistant buttons, curl, etc.) - no need to follow up with ?dir=stop for these.
echo "Content-type: text/plain"
echo "Access-Control-Allow-Origin: *"
echo
CAM=/mnt/sdcard/cam
dir=$(echo "$QUERY_STRING" | sed -n 's/.*dir=\([a-z]*\).*/\1/p')
steps=$(echo "$QUERY_STRING" | sed -n 's/.*steps=\([0-9]*\).*/\1/p')
case "$dir" in
  left|right|up|down)
    if [ -n "$steps" ]; then
      "$CAM/bin/ptz" "$dir" "$steps" >/dev/null 2>&1
    else
      "$CAM/bin/ptz" "$dir" >/dev/null 2>&1
    fi
    echo ok ;;
  stop|home) "$CAM/bin/ptz" "$dir" >/dev/null 2>&1; echo ok ;;
  *) echo "usage: ?dir=left|right|up|down|stop|home[&steps=N]" ;;
esac
