#!/bin/sh
set -eu
umask 077
root=${1:-"$HOME/srv/regagent_prod"}
env_source=${2:?Укажите исходный .env}
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$root/bin" "$root/configs" "$root/releases" "$root/logs" "$root/data/docs" "$root/data/okf"
install -m 700 "$here/deploy.py" "$here/poll.sh" "$root/bin/"
rm -f "$root/bin/start-tunnel.sh"  # туннель теперь поднимает выкатка (сервис tunnel)
# Первичный .env стенда: публичные адреса и VITE_API_URL=/api; существующий .env не меняется.
/usr/bin/python3 "$root/bin/deploy.py" init-env "$env_source" "$root/.env" "https://regagent.honey-road.tech"
if [ ! -e "$root/cloudflare.env" ]; then
  printf 'TUNNEL_TOKEN=\n' >"$root/cloudflare.env"
fi
if [ ! -d "$root/source/.git" ]; then
  git clone --no-checkout git@github.com:Long-Honey-Road/document-searching.git "$root/source"
fi
agents="$HOME/Library/LaunchAgents"
mkdir -p "$agents"
plist="$agents/tech.honey-road.regagent-prod.plist"
/usr/bin/python3 - "$root" "$plist" <<'PY'
import os
import plistlib
import sys

root, plist = sys.argv[1:]
with open(plist, "wb") as output:
    plistlib.dump({
        "Label": "tech.honey-road.regagent-prod",
        "ProgramArguments": ["/bin/sh", f"{root}/bin/poll.sh", root],
        "EnvironmentVariables": {
            "PATH": f"{os.environ['HOME']}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "GIT_TERMINAL_PROMPT": "0",
        },
        "StartInterval": 120,
        "RunAtLoad": True,
        "StandardErrorPath": f"{root}/logs/launchd.log",
    }, output)
PY
domain="gui/$(id -u)"
launchctl bootout "$domain" "$plist" 2>/dev/null || true
launchctl bootstrap "$domain" "$plist"
