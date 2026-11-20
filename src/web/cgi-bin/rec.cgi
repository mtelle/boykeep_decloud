#!/bin/sh
# List motion recordings as JSON. Files served statically from /rec (symlink).
#  GET rec.cgi                          -> today's clips only, paged
#  GET rec.cgi?list=days                -> day-folders only (day,count), paged, today excluded
#  GET rec.cgi?day=YYYYMMDD             -> that day's clips, paged
#  GET rec.cgi?all=1                    -> every clip, every day, flat (no paging) - for scripts/HA
#  ?offset=N&limit=N (default 0,30)     -> pagination within whichever list above is selected
echo "Content-type: application/json"
echo "Cache-Control: no-store"
echo
REC=${MD_DIR:-/mnt/sdcard/rec}

day=""; limit=30; offset=0; all=0; list=""
old_ifs=$IFS
IFS='&'
for kv in $QUERY_STRING; do
  case "$kv" in
    day=*) day=${kv#day=} ;;
    limit=*) limit=${kv#limit=} ;;
    offset=*) offset=${kv#offset=} ;;
    all=*) all=${kv#all=} ;;
    list=*) list=${kv#list=} ;;
  esac
done
IFS=$old_ifs
case "$limit" in ''|*[!0-9]*) limit=30 ;; esac
case "$offset" in ''|*[!0-9]*) offset=0 ;; esac
case "$day" in ''|*[!0-9]*) day="" ;; esac   # only digits allowed (no path traversal)
case "$all" in 1) all=1 ;; *) all=0 ;; esac
case "$list" in days) list=days ;; *) list="" ;; esac

today=$(date +%Y%m%d)

clip_files() {   # $1=day -> newline list of clip filenames, newest first
  ls -1r "$REC/$1" 2>/dev/null | { while read -r f; do
    case "$f" in *.mp4|*.h264|*.h265) echo "$f" ;; esac
  done; }
}

day_dirs() {   # -> "day:count" lines (excluding $1, if given), newest first, non-empty only
  excl=$1
  for d in $(ls -1r "$REC" 2>/dev/null); do
    [ -d "$REC/$d" ] || continue
    [ -n "$excl" ] && [ "$d" = "$excl" ] && continue
    cnt=0
    for f in $(clip_files "$d"); do cnt=$((cnt+1)); done
    [ "$cnt" -eq 0 ] && continue
    echo "$d:$cnt"
  done
}

print_clip() { printf '{"day":"%s","file":"%s","url":"/rec/%s/%s","size":%s}' "$1" "$2" "$1" "$2" "${3:-0}"; }

print_clips() {   # $1=day $2=offset $3=limit -> prints JSON array body (no brackets); sets g_total
  d=$1; off=$2; lim=$3
  g_total=0; i=0; first=1
  for f in $(clip_files "$d"); do
    if [ "$i" -ge "$off" ] && [ "$i" -lt $((off+lim)) ]; then
      sz=$(stat -c %s "$REC/$d/$f" 2>/dev/null)
      [ "$first" = 1 ] || printf ','
      first=0
      print_clip "$d" "$f" "$sz"
    fi
    i=$((i+1))
  done
  g_total=$i
}

print_days() {   # $1=exclude-day $2=offset $3=limit -> prints JSON array body (no brackets); sets g_total
  excl=$1; off=$2; lim=$3
  g_total=0; i=0; first=1
  for dc in $(day_dirs "$excl"); do
    d=${dc%%:*}; cnt=${dc#*:}
    if [ "$i" -ge "$off" ] && [ "$i" -lt $((off+lim)) ]; then
      [ "$first" = 1 ] || printf ','
      first=0
      printf '{"day":"%s","count":%s}' "$d" "$cnt"
    fi
    i=$((i+1))
  done
  g_total=$i
}

printf '{"recording":'
if [ -f /tmp/motion.rec ]; then printf 'true'; else printf 'false'; fi
printf ',"today":"%s"' "$today"

if [ "$all" = 1 ]; then
  # every clip, every day, unpaginated - meant for scripts/Home Assistant, not the web UI
  printf ',"clips":['
  first=1
  for d in $(ls -1r "$REC" 2>/dev/null); do
    [ -d "$REC/$d" ] || continue
    for f in $(clip_files "$d"); do
      sz=$(stat -c %s "$REC/$d/$f" 2>/dev/null)
      [ "$first" = 1 ] || printf ','
      first=0
      print_clip "$d" "$f" "$sz"
    done
  done
  printf ']}'
elif [ "$list" = days ]; then
  printf ',"days":['
  print_days "$today" "$offset" "$limit"
  printf '],"total":%s,"offset":%s,"limit":%s}' "$g_total" "$offset" "$limit"
elif [ -n "$day" ]; then
  printf ',"day":"%s","clips":[' "$day"
  print_clips "$day" "$offset" "$limit"
  printf '],"total":%s,"offset":%s,"limit":%s}' "$g_total" "$offset" "$limit"
else
  printf ',"clips":['
  print_clips "$today" "$offset" "$limit"
  printf '],"total":%s,"offset":%s,"limit":%s}' "$g_total" "$offset" "$limit"
fi
