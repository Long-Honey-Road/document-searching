#!/bin/sh
set -eu
root=${1:?Укажите каталог стенда}
test -r "$root/cloudflare.env"
if ! /usr/bin/grep -q '^TUNNEL_TOKEN=eyJ.' "$root/cloudflare.env"; then
    printf 'Заполните TUNNEL_TOKEN в %s/cloudflare.env\n' "$root" >&2
    exit 1
fi
/usr/bin/python3 -c 'import json,sys; sys.exit(not json.load(open(sys.argv[1])).get("good"))' \
  "$root/state.json"
if docker container inspect regagent-prod-tunnel >/dev/null 2>&1; then
    docker start regagent-prod-tunnel >/dev/null
    exit
fi
docker run -d --name regagent-prod-tunnel --restart unless-stopped \
  --network regagent-prod_default --env-file "$root/cloudflare.env" \
  --log-opt max-size=10m --log-opt max-file=3 \
  cloudflare/cloudflared:2026.10.7 tunnel --no-autoupdate run
