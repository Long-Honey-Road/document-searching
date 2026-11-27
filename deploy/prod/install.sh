#!/bin/sh
set -eu
umask 077
root=${1:-"$HOME/srv/regagent_prod"}
env_source=${2:?Укажите исходный .env}
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$root/bin" "$root/configs" "$root/releases" "$root/logs" "$root/data/docs" "$root/data/okf"
install -m 700 "$here/deploy.py" "$here/poll.sh" "$here/start-tunnel.sh" "$root/bin/"
if [ ! -e "$root/.env" ]; then
  /usr/bin/python3 - "$env_source" "$root/.env" <<'PY'
import sys
from pathlib import Path

source, destination = map(Path, sys.argv[1:])
base = "https://regagent.honey-road.tech"
issuer = base + "/auth/realms/regagent"
overrides = {
    "ENV": "stand",
    "PUBLIC_BASE_URL": base,
    "KEYCLOAK_ISSUER": issuer,
    "VITE_OIDC_AUTHORITY": issuer,
    "RERANK_MODE": "embeddings",
    "LIGHTRAG_EXTRACT_GLEANING": "0",
}
lines = [line for line in source.read_text().splitlines() if line.partition("=")[0] not in overrides]
destination.write_text("\n".join([*lines, *(f"{key}={value}" for key, value in overrides.items())]) + "\n")
PY
fi
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
