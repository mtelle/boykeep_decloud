#!/bin/sh
# de-cloud factorytest wrapper (firmado UNA sola vez).
# Toda la logica real vive en run.sh: se puede editar libremente sin re-firmar, porque
# safe_exec.sh/check_auth solo verifican el hash de ESTE fichero, no de lo que el llama.
sh /mnt/sdcard/factorytest/run.sh
