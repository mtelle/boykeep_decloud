#!/bin/sh
# health.cgi - service health for the web UI (JSON from /tmp/watchdog.status, written by camwatch.sh)
echo "Content-type: application/json"
echo "Cache-Control: no-store"
echo
printf '{'
first=1
while IFS='=' read -r k v; do
  [ -z "$k" ] && continue
  [ "$first" = 1 ] || printf ','
  first=0
  case "$v" in
    ''|*[!0-9]*) printf '"%s":"%s"' "$k" "$v" ;;   # string
    *)           printf '"%s":%s' "$k" "$v" ;;      # number
  esac
done < /tmp/watchdog.status 2>/dev/null
printf '}'
