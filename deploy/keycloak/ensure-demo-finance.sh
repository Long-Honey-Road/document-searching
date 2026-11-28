#!/usr/bin/env bash
# Однократное назначение группы при запуске существующего демо-стенда.
set -euo pipefail

if [[ ${REGAGENT_ENV:-} != stand || ${REGAGENT_DEMO_FINANCE_MIGRATION:-on} != on ]]; then
    exit 0
fi

port=${KC_HTTP_PORT:-8080}
server="http://127.0.0.1:$port/auth"
cli="$(dirname -- "${BASH_SOURCE[0]}")/bin/kcadm.sh"
profile_dir=$(mktemp -d)
profile="$profile_dir/session"
trap 'rm -f -- "$profile"; rmdir -- "$profile_dir"' EXIT

keycloak_ready() {
    (: > "/dev/tcp/127.0.0.1/$port") 2>/dev/null
}

for ((attempt = 0; attempt < 60; attempt++)); do
    if keycloak_ready; then
        break
    fi
    sleep 1
done
keycloak_ready
# Одна попытка входа: неверный пароль нельзя повторять до блокировки учётки.
KC_CLI_PASSWORD="${KC_BOOTSTRAP_ADMIN_PASSWORD:?}" "$cli" config credentials \
    --server "$server" --realm master --user "${KC_BOOTSTRAP_ADMIN_USERNAME:?}" \
    --config "$profile" >/dev/null 2>&1

realm() {
    "$cli" "$@" -r regagent --config "$profile"
}

user=$(realm get users -q username=admin -q email=admin@example.invalid -q exact=true \
    --fields id --format csv --noquotes)
group=$(realm get group-by-path/levels/closed_fin --fields id --format csv --noquotes)
id_pattern='^[[:xdigit:]]{8}(-[[:xdigit:]]{4}){3}-[[:xdigit:]]{12}$'
[[ $user =~ $id_pattern && $group =~ $id_pattern ]]
roles=$(realm get "users/$user/role-mappings/realm" --fields name --format csv --noquotes)
grep -Fxq admin <<< "$roles"
memberships=$(realm get "users/$user/groups" --fields id --format csv --noquotes)
if ! grep -Fxq "$group" <<< "$memberships"; then
    realm update "users/$user/groups/$group" -n >/dev/null
    memberships=$(realm get "users/$user/groups" --fields id --format csv --noquotes)
    grep -Fxq "$group" <<< "$memberships"
fi
echo 'Демо-администратор состоит в группе /levels/closed_fin'
