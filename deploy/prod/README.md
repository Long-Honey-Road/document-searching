# Выкладка RegAgent на Mac

GitHub Actions проверяет Python, тесты на утечки (Postgres с AGE и RabbitMQ) и веб
на PR и push в `main`. Пользовательский LaunchAgent на Mac каждые две минуты
подтягивает только `origin/main` и выполняет проверки локально. Для запуска нужны
доступ по SSH к репозиторию, работающий Docker Desktop, Ollama и вход в
пользовательскую сессию macOS.

Команды ниже проверены в zsh (оболочка macOS по умолчанию) и работают в bash.

## Установка и обновление выкатки

```sh
/bin/sh deploy/prod/install.sh "$HOME/srv/regagent_prod" /путь/к/исходному/.env
```

- Работает не `deploy.py` из релиза, а его копия в `~/srv/regagent_prod/bin/`.
  **Изменения в `deploy/prod/` вступают в силу только после повторного запуска
  `install.sh`.** Повторный запуск безопасен: существующие `.env` и `cloudflare.env`
  не меняются, обновляются `bin/` и LaunchAgent.
- `.env` стенда создаётся один раз из исходного файла (`deploy.py init-env`).
  Принудительно задаются `ENV=stand`, публичные адреса
  `https://regagent.honey-road.tech`, issuer Keycloak и `VITE_API_URL=/api`.
- Снимок конфигурации каждого релиза — `configs/<SHA>.env`, с ним же делается откат.
- `data/docs` и `data/okf` общие для всех релизов. Пустая папка документов при
  первом запуске заполняется мини-корпусом. Файл без записи в
  `data/docs/manifest.yaml` не индексируется.
- `logs/deploy.log` (и `.1`) — ход проверки, SHA, ошибки, откаты.
  `state.json` — принятый SHA (`good`), отклонённый (`rejected`), незавершённый
  (`pending`).
- `verify-release` — необязательный исполняемый файл для дополнительных проверок
  запущенного кандидата, получает SHA первым аргументом.

## Что проверяется в каждом релизе

1. **Конфигурация, до тестов и сборки.** Релиз отклоняется, если в снимке `.env`:
   - есть `POSTGRES_WORKSPACE`;
   - есть значения-заглушки `change-me` или `replace-me`.

   Для `ENV=stand` дополнительно отклоняется, если:
   - `PUBLIC_BASE_URL` не публичный https-адрес;
   - `KEYCLOAK_ISSUER` или `VITE_OIDC_AUTHORITY` не совпадают с ним;
   - `VITE_API_URL` не относительный путь;
   - в `VITE_*` есть `localhost` или `127.0.0.1`.

   В лог пишется имя переменной, без значения. После исправления `.env` следующая
   попытка через 2 минуты пройдёт сама.
2. **Тесты и сборка:** `uv lock --check`, ruff, mypy, pytest, тесты и сборка web.
3. **Сборка web:** в образе нет локального адреса стенда (`http://localhost:8080`),
   а `VITE_OIDC_AUTHORITY` из конфигурации есть. Это ловит фронт, собранный не с тем
   `.env`.
4. **После переключения:**
   - через loopback-порт 18082 мимо Cloudflare: web, `/api/health`, отказ без токена,
     issuer OIDC;
   - весь корпус из манифеста проиндексирован.

   Только после этого SHA становится рабочей версией, затем поднимается туннель.

Откат использует тот же Compose, снимок env и образы по SHA. Тома Postgres, RabbitMQ и
индекса общие для релизов, поэтому изменения схемы БД должны быть совместимы с
предыдущей версией.

## Учётки

Логины зашиты в `deploy/keycloak/regagent-realm.json`, пароли — в `.env`.

| Логин | Пароль в `.env` | Где входить | Что может |
| --- | --- | --- | --- |
| `employee_public` | `KEYCLOAK_EMPLOYEE_PUBLIC_PASSWORD` | страница приложения | открытые документы |
| `employee_fin` | `KEYCLOAK_EMPLOYEE_FIN_PASSWORD` | страница приложения | + закрытый финансовый уровень (группа `/levels/closed_fin`) |
| `qc_expert` | `KEYCLOAK_QC_EXPERT_PASSWORD` | страница приложения | роль `qc` |
| `admin` | `KEYCLOAK_ADMIN_DEMO_PASSWORD` | страница приложения | роль `admin` приложения |
| значение `KEYCLOAK_ADMIN` (обычно `admin`) | `KEYCLOAK_ADMIN_PASSWORD` | админка Keycloak `/auth/admin/` | администратор Keycloak (realm `master`) |

**Логин `admin` есть в двух местах с разными паролями.** На странице приложения нужен
`KEYCLOAK_ADMIN_DEMO_PASSWORD`, в админке Keycloak — `KEYCLOAK_ADMIN_PASSWORD`.

Пароли применяются **только при первом старте Keycloak**. Если поменять их в `.env`
потом, Keycloak этого не увидит. Менять пароль надо через админку или `kcadm.sh`:

```sh
docker exec -it regagent-prod-keycloak-1 sh -c '
  /opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080/auth \
    --realm master --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" &&
  /opt/keycloak/bin/kcadm.sh set-password -r regagent --username employee_public --new-password "<новый пароль>"'
```

Затем записать новый пароль в `.env`, чтобы он и дальше совпадал с Keycloak.

Логин и пароль администратора Keycloak, без вывода остального файла:

```sh
grep -E '^KEYCLOAK_ADMIN(_PASSWORD)?=' ~/srv/regagent_prod/.env
```

## Cloudflare

Hostname `regagent.honey-road.tech` закрыт Cloudflare Team Access, это решение команды.
Снаружи сначала проходят Access, потом вход Keycloak. Проверки релиза идут через
loopback и Access не задевают.

1. В Cloudflare Zero Trust → **Networking → Tunnels** → туннель RegAgent →
   **Published application**: hostname `regagent.honey-road.tech`, origin
   `http://proxy:8080`.
2. Токен коннектора (`eyJ…`) — строкой `TUNNEL_TOKEN=…` в
   `~/srv/regagent_prod/cloudflare.env`. В git и GitHub Secrets не кладётся.
3. Коннектор — сервис `tunnel` релиза (профиль `tunnel`, образ закреплён в
   `deploy.py`). Выкатка поднимает его после успешной проверки и откатывает вместе
   с релизом. Пока токена нет, в `deploy.log` пишется предупреждение. Сбой туннеля не
   отклоняет релиз — это предупреждение в логе.

```sh
docker logs --tail 30 regagent-prod-tunnel-1     # ждём "Registered tunnel connection"
```

**Переход со старого ручного коннектора.** Раньше его запускали через `start-tunnel.sh`.
Один раз удалите старый контейнер, иначе будут работать два коннектора:

```sh
docker rm -f regagent-prod-tunnel
```

Bypass, если понадобится пустить людей без Access: Zero Trust → Access → Applications
→ приложение `regagent.honey-road.tech` → Policies → политика с Action **Bypass**,
Include **Everyone**, выше политики Allow. Защитой останется только вход Keycloak.

## Диагностика

Общие переменные для команд ниже:

```sh
ROOT=$HOME/srv/regagent_prod
SHA=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["good"])' "$ROOT/state.json")
# Compose текущего релиза — функцией: zsh не разбивает строку из переменной на аргументы
c() {
  (
    cd "$ROOT/releases/$SHA" || exit 1
    if [ -f tunnel.json ]; then
      docker compose -p regagent-prod --env-file "$ROOT/configs/$SHA.env" \
        -f deploy/compose.yaml -f deploy/prod/compose.yaml -f storage.json \
        -f tunnel.json -f images.json "$@"
    else
      docker compose -p regagent-prod --env-file "$ROOT/configs/$SHA.env" \
        -f deploy/compose.yaml -f deploy/prod/compose.yaml -f storage.json \
        -f images.json "$@"
    fi
  )
}
```

**Состояние:**

```sh
cat "$ROOT/state.json"
docker ps --filter name=regagent-prod --format '{{.Names}}\t{{.Status}}'
tail -50 "$ROOT/logs/deploy.log"
```

**Индексация.** Все задачи `succeeded` и пустая очередь означают, что индексация
закончена, а не зависла.

```sh
docker logs --tail 200 regagent-prod-knowledge-1 2>&1 | grep -v "GET /health"
docker exec regagent-prod-rabbitmq-1 rabbitmqctl list_queues name messages_ready messages_unacknowledged consumers
docker exec -it regagent-prod-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$KNOWLEDGE_DB"'
```

```sql
select doc_id, op, status, attempts, left(error,200) as error, steps, now()-updated_at as idle
from jobs order by updated_at desc limit 10;
select status, count(*) from documents group by status;
select workspace, id, status, chunks_count, updated_at, left(error_msg,300)
from lightrag_doc_status where status <> 'processed' order by updated_at desc;
```

Сколько файлов в папке и сколько записей в манифесте:

```sh
ls "$ROOT/data/docs" | grep -v manifest | wc -l
grep -c "doc_id:" "$ROOT/data/docs/manifest.yaml"
```

**Web и API.** Через loopback (мимо Cloudflare) анонимный запрос должен вернуть
`401` с JSON. Снаружи без cookie Access ответит `302` на `cloudflareaccess.com` —
это Access, а не backend.

```sh
curl -si -X POST http://127.0.0.1:18082/api/assistants/search \
  -H 'Host: regagent.honey-road.tech' -H 'content-type: application/json' -d '{}' | head -3
docker exec regagent-prod-web-1 grep -rlF http://localhost:8080 /srv || echo "сборка web чистая"
docker exec regagent-prod-backend-1 printenv KEYCLOAK_ISSUER KEYCLOAK_AUDIENCE
```

В браузере web пишет причину сбоя: `401` — «войдите снова», нет связи — «сервер
недоступен», нет ассистента — «агент не настроен». В DevTools → Network запросы
должны идти на `https://regagent.honey-road.tech/api/…`. Если они идут на
`localhost`, web собран с неправильным `VITE_API_URL`.

**Пересобрать один сервис текущего релиза** (например, web после правки `.env`).
Сначала поправьте и `.env`, и `configs/$SHA.env`, затем:

```sh
c build web && c up -d --no-deps web
```
