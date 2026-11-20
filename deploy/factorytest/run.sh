#!/bin/sh
# de-cloud AUTOSTART (v4): conecta wlan0 a TU wifi (2.4GHz), abre shell root por telnet,
# y lanza el stack de-cloud completo (RTSP+motion+OSD+watchdog). Se ejecuta en CADA arranque
# via el gancho /mnt/sdcard/factorytest de rcS (firmware stock), sin tocar flash ni UART.
D=/mnt/sdcard/factorytest
OUT=$D/step2.log
WLAN=$(cat /etc/wlanname 2>/dev/null || echo wlan0)
exec > "$OUT" 2>&1
echo "=== AUTOSTART v4 $(date) ==="

CR=$(printf '\r')
i=0; SSID=""; PSK=""
while IFS= read -r line || [ -n "$line" ]; do
  i=$((i+1))
  case "$line" in *"$CR") line="${line%"$CR"}" ;; esac
  [ $i -eq 1 ] && SSID="$line"
  [ $i -eq 2 ] && PSK="$line"
done < "$D/wifi.txt"
echo "WLAN=$WLAN  SSID=[$SSID]  (clave: ${#PSK} chars)"

if [ -z "$SSID" ] || [ "$SSID" = "PON_AQUI_TU_SSID" ]; then
  echo "ERROR: falta wifi.txt con tu SSID/clave. Sigo igualmente con telnet+stack (sin red)."
fi

killall wpawatchdog.sh dhcpcwatchdog.sh wpa_supplicant udhcpc 2>/dev/null
sleep 1
rm -f /var/run/wpa_supplicant/$WLAN 2>/dev/null
mkdir -p /etc/conf
{
  echo "ctrl_interface=/var/run/wpa_supplicant"
  echo "update_config=1"
  echo "network={"
  echo "    ssid=\"$SSID\""
  echo "    psk=\"$PSK\""
  echo "    scan_ssid=1"
  echo "}"
} > /etc/conf/wpa.conf
echo "--- wpa.conf generado ($(wc -l < /etc/conf/wpa.conf) lineas) ---"; cat /etc/conf/wpa.conf
ifconfig $WLAN up

for DRV in nl80211 wext; do
  killall wpa_supplicant 2>/dev/null; sleep 1
  rm -f /var/run/wpa_supplicant/$WLAN 2>/dev/null
  echo "--- wpa_supplicant -D$DRV ---"
  wpa_supplicant -B -i$WLAN -D$DRV -C/var/run/wpa_supplicant -c/etc/conf/wpa.conf && sleep 1
  wpa_cli -i$WLAN reassociate 2>/dev/null
  j=0; OK=0
  while [ $j -lt 15 ]; do
    if wpa_cli -i$WLAN status 2>/dev/null | grep -q "wpa_state=COMPLETED"; then OK=1; break; fi
    sleep 1; j=$((j+1))
  done
  echo "wpa_state tras $DRV: $(wpa_cli -i$WLAN status 2>/dev/null | grep wpa_state)"
  [ $OK = 1 ] && break
done

udhcpc -i$WLAN -q -n -t 10
sleep 2
echo "--- ifconfig $WLAN ---"; ifconfig $WLAN
IP=$(ifconfig $WLAN 2>/dev/null | grep "inet addr" | sed "s/.*inet addr:\([0-9.]*\).*/\1/")
echo "IP=$IP"
echo "$IP" > "$D/CAMERA_IP.txt"

killall telnetd 2>/dev/null
telnetd -l /bin/sh -p 2323
echo "telnet $IP 2323 -> shell root"

# Lanzar el stack de-cloud completo (RTSP+motion+OSD+watchdog). En background: start.sh solo
# arranca demonios y vuelve, pero por si algo bloquea no queremos colgar el arranque.
if [ -x /mnt/sdcard/cam/start.sh ]; then
    echo "--- lanzando /mnt/sdcard/cam/start.sh ---"
    sh /mnt/sdcard/cam/start.sh &
else
    echo "AVISO: /mnt/sdcard/cam/start.sh no existe o no es ejecutable; solo wifi+telnet activos."
fi

sync
echo "=== AUTOSTART DONE ==="
