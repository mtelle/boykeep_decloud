#!/bin/sh
# de-cloud launcher sobre firmware STOCK (libs /opt/vicam, SD /mnt/sdcard/cam)
CAM=/mnt/sdcard/cam
export PATH=$CAM/bin:$PATH
export LD_LIBRARY_PATH=/lib:/opt/vicam:$CAM/bin   # $CAM/bin -> libmpi_aenc.so, libakaac.so, libakaudiocodec.so
export TZ=CET-1CEST,M3.5.0,M10.5.0/3            # España (CET/CEST); cambia si tu zona difiere
export SNAP_PATH=/tmp/www/snapshot.jpg SNAP_FPS=5
WLAN=$(cat /etc/wlanname 2>/dev/null || echo wlan0)
mkdir -p /tmp/www                                # docroot en tmpfs (la SD vfat no guarda +x)

# 1. parar la nube (el stock ya carga modulos, sensor GC2083 y WiFi)
touch /etc/closewdog
# NO llamar a /opt/vicam/dotstart.sh (ni start ni stop): lanza siempre en segundo plano
# sdcard_check(), que espera a ver "9008" en netstat -a (coincide por subcadena con
# CUALQUIER puerto efimero, ej. 49008) y entonces hace "umount /mnt/sdcard -l" solo.
# Nos desmonto la SD en pleno uso. Paramos viCam directamente, sin pasar por ahi.
killall -9 wdog watchdog.sh viCam 2>/dev/null
sleep 2

# 1b. Hora por el router (sin pila RTC, sin internet). Reajusta cada hora. Ver WEB.md
sh $CAM/timesync.sh &

# 2. SSH (dropbear, password de root -> ponla con 'passwd' si no lo has hecho ya).
#    scp funciona (esta incluido en dropbearmulti); sftp NO (le falta un binario externo).
#    cam/bin esta en la SD (vfat, sin symlinks) asi que el enlace "scp" va en /etc (jffs2).
mkdir -p /etc/decloud-bin
ln -sf $CAM/bin/dropbearmulti /etc/decloud-bin/scp
export PATH=/etc/decloud-bin:$PATH
# clave de host persistida en la SD ($CAM/etc), si no, cada reboot la regenera en /tmp
# (tmpfs) y cambia - eso dispara el aviso de "REMOTE HOST IDENTIFICATION HAS CHANGED"
# en el cliente ssh en cada reinicio. Se genera una sola vez, la primera vez que falte.
[ -f $CAM/etc/ssh_host_ed25519_key ] || $CAM/bin/dropbearmulti dropbearkey -t ed25519 -f $CAM/etc/ssh_host_ed25519_key >/dev/null 2>&1
$CAM/bin/dropbearmulti dropbear -r $CAM/etc/ssh_host_ed25519_key -p 22 &

# 2b. Deteccion de movimiento + grabacion MP4 por evento.
#     MD_CHN=1 (vs1, HEVC). H.264 no es posible en este chip: libmpi_venc y el driver del
#     kernel lo bloquean y, forzado, el codificador se cuelga tras el primer fotograma
#     (probado en hardware, ver docs/05-video-pipeline.md). Se queda en HEVC.
#     MD_AUDIO=1 mezcla el audio del microfono en el .mp4. MD_KEEPDAYS>0 borra clips mas viejos.
export MD=1 MD_FMT=mp4 MD_CHN=1 MD_CELL_TH=80 MD_MIN_CELLS=6 MD_HOLD=8 \
       MD_PERIOD=300 MD_MAXCLIP=120 MD_MINFREE=200 MD_KEEPDAYS=0 MD_AUDIO=0 MD_DIR=/mnt/sdcard/rec
# OSD: fecha/hora quemada en el video (ambos streams). OSD=0 lo desactiva. Ver WEB.md.
export OSD=1 OSD_X=16 OSD_Y=16 OSD_SCALE=2 OSD_FG=1 OSD_BG=0 OSD_ALPHA=70
# Config editable desde la web (sobreescribe los valores de arriba si el fichero existe)
[ -f $CAM/etc/config.env ] && . $CAM/etc/config.env

# Auth (web + RTSP), optional: $CAM/etc/auth.env (never in git - see deploy/auth.env.example)
# sets WEB_USER/WEB_PASS/RTSP_USER/RTSP_PASS. Missing file, or a blank value, leaves that
# part fully open - same as before this existed. RTSP_USER/RTSP_PASS only take effect in a
# rtspd built with patch_rtsp_auth.py (reads them via getenv at its own startup, below).
[ -f $CAM/etc/auth.env ] && . $CAM/etc/auth.env
mkdir -p /mnt/sdcard/rec

# ISP: el fichero real tiene el prefijo de placa (f6m05-) que nuestro rtspd no espera.
# Se regenera en cada arranque (no persiste, no hace falta persistirlo): copiamos el real
# a tmpfs con un alias symlink al nombre que rtspd busca, y tapamos el directorio de
# solo lectura con un bind-mount (no se puede escribir /system directamente).
ISPREAL=""
for f in /system/etc/config/isp_*-gc2083_mipi_2lane_h3b.conf; do
    [ -f "$f" ] && { ISPREAL="$f"; break; }
done
if [ -n "$ISPREAL" ] && [ ! -f /system/etc/config/isp_gc2083_mipi_2lane_h3b.conf ]; then
    ISPNAME=${ISPREAL##*/}   # basename no existe en este busybox: expansion pura
    mkdir -p /tmp/ispcfg
    cp "$ISPREAL" /tmp/ispcfg/
    ln -sf "$ISPNAME" /tmp/ispcfg/isp_gc2083_mipi_2lane_h3b.conf
    mount -o bind /tmp/ispcfg /system/etc/config
fi

# 3. RTSP (respawn). vs0=H.265 1080p (vivo), vs1=H.265 720p (grabacion + web)
$CAM/bin/rtspd -i $CAM/etc/rtsp.ini >/tmp/rtsp.log 2>&1 &

# 4. Web (directo + PTZ + grabaciones) y ONVIF
rm -rf /tmp/www/cgi-bin /tmp/www/index.html 2>/dev/null
cp -r $CAM/www/* /tmp/www/ 2>/dev/null
chmod +x /tmp/www/cgi-bin/* 2>/dev/null
# ONVIF endpoints (/onvif/<service>): one tiny CGI per service, rebuilt each boot in tmpfs.
# onvif_simple_server takes the service name as its last argument, reads -c for the conf
# (it ignores ONVIF_CONF) and opens its XML templates relative to the current directory.
mkdir -p /tmp/www/onvif
# it exits WITHOUT answering if it cannot open its log. Our build (build_onvif.sh) moved that
# log, and the default conf, from read-only /var/log and /etc to /tmp/onvif.
mkdir -p /tmp/onvif
for s in device_service media_service media2_service ptz_service events_service deviceio_service; do
    printf '#!/bin/sh\ncd %s/bin/templates && exec %s/bin/onvif_simple_server -c /tmp/onvif.conf %s\n' "$CAM" "$CAM" "$s" > /tmp/www/onvif/$s
    chmod +x /tmp/www/onvif/$s
done
rm -rf /tmp/www/rec 2>/dev/null                 # evita que ln -sf siga un symlink previo hacia la SD (vfat sin symlinks)
ln -sf /mnt/sdcard/rec /tmp/www/rec              # grabaciones servidas en /rec (con Range -> seek)
MAC=$(cat /sys/class/net/$WLAN/address 2>/dev/null | sed "s/://g")   # tr no existe en este busybox
# ONVIF auth: ONVIF clients (Home Assistant, ODM, NVRs) send credentials inside the SOAP body
# (WS-Security), never as HTTP Basic, so our busybox httpd lets /onvif/* through
# (src/tools/patch_busybox_httpd.py) and onvif_simple_server checks them itself. Same
# user/password as the web unless ONVIF_USER/ONVIF_PASS are set in auth.env.
OU=${ONVIF_USER:-$WEB_USER}; OP=${ONVIF_PASS:-$WEB_PASS}
{ [ -n "$OU" ] && [ -n "$OP" ] && printf 'user=%s\npassword=%s\n' "$OU" "$OP"
  sed -e "s/^serial_num=SERIAL/serial_num=${MAC:-000000000000}/" -e "s/HOSTNAME/$(hostname)/" -e "s/^ifs=.*/ifs=$WLAN/" $CAM/etc/onvif_simple_server.conf
} > /tmp/onvif.conf
chmod 600 /tmp/onvif.conf
export ONVIF_CONF=/tmp/onvif.conf
# httpd.conf: a single "/:user:pass" line if WEB_USER/WEB_PASS are set, empty otherwise
# (busybox httpd's basic-auth format - see src/rtsp-patches/patch_rtsp_auth.py's comment for
# the RTSP side of this same on/off-by-file-content idea). camwatch.sh reuses this same file
# as-is if it ever has to restart httpd, it does not regenerate it.
if [ -n "$WEB_USER" ] && [ -n "$WEB_PASS" ]; then
    printf '/:%s:%s\n' "$WEB_USER" "$WEB_PASS" > /tmp/httpd.conf
else
    : > /tmp/httpd.conf
fi
$CAM/bin/busybox httpd -p 80 -h /tmp/www -c /tmp/httpd.conf &   # busybox propio: httpd no esta en el del sistema
SCOPES=$(grep "^scope=" /tmp/onvif.conf | cut -d= -f2-)   # tr no existe: unir lineas con set -- / $*
set -- $SCOPES
SCOPES="$*"
$CAM/bin/wsd_simple_server -i $WLAN -x "http://%s/onvif/device_service" -m AK3918EV300L-PTZ -n Anyka -s "$SCOPES" -p /tmp/wsd.pid -t $CAM/bin/templates/wsd_files &

# 5. PTZ homing (motorctl / driver stock)
$CAM/bin/ptz home >/dev/null 2>&1 &

# 5b. MQTT -> Home Assistant (camqtt, optional): only if $CAM/etc/mqtt.env exists and sets
#     MQTT_HOST (never in git - see deploy/mqtt.env.example). camwatch restarts it if it dies.
[ -f $CAM/etc/mqtt.env ] && . $CAM/etc/mqtt.env
if [ -n "$MQTT_HOST" ] && [ -x $CAM/bin/camqtt ]; then
    export MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASS MQTT_ID MQTT_NAME MQTT_BASE MQTT_DISCOVERY MQTT_LANG MQTT_SNAP_INT MQTT_PTZ_STEPS
    $CAM/bin/camqtt 2>>/tmp/camqtt.log &
fi

# 6. Watchdog (supervisa y reinicia servicios; HW watchdog opcional: WATCHDOG_HW=1)
sh $CAM/camwatch.sh &
