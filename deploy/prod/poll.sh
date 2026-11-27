#!/bin/sh
set -eu
umask 077
root=${1:?Укажите каталог стенда}
log="$root/logs/deploy.log"
if [ -f "$log" ] && [ "$(wc -c < "$log")" -ge 10485760 ]; then
  mv -f "$log" "$log.1"
fi
exec /usr/bin/python3 "$root/bin/deploy.py" "$root" >>"$log" 2>&1
