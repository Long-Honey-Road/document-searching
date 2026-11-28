# Grafana для стенда RegAgent

Отдельный Compose-проект `regagent-grafana` на том же Mac, что и стенд. Он не
зависит от релизов: выкатка стенда его не трогает, а его падение не трогает стенд.

| Сервис | Что делает |
| --- | --- |
| `alloy` | читает логи контейнеров проекта `regagent-prod` через Docker и `logs/events.jsonl` выкатки, у строк-событий (D-52) делает метки `event` и `status` |
| `loki` | хранит логи 14 дней на томе `loki-data` |
| `grafana` | источник Loki и дашборд «RegAgent — события стенда» уже подключены |
| `tunnel` | коннектор Cloudflare для `grafana.honey-road.tech` |

Наружу открыт только `tunnel`. Grafana доступна ещё на loopback Mac (`127.0.0.1:13000`)
для проверки, Loki и Alloy портов не публикуют.

## Запуск (10 минут)

### 1. Туннель в Cloudflare

Нужен **отдельный туннель**, не туннель стенда. Если повесить второй коннектор на
туннель стенда, Cloudflare будет раскидывать запросы между коннекторами: коннектор
Grafana не знает `proxy:8080`, коннектор стенда не знает `grafana:3000`, и половина
запросов к обоим хостам упадёт с 502.

1. Zero Trust → **Networks → Tunnels → Create a tunnel** → Cloudflared, имя
   `regagent-grafana`.
2. Скопировать токен (`eyJ…`) из команды установки, саму команду не запускать.
3. **Published application**: hostname `grafana.honey-road.tech`, service
   `HTTP` → `grafana:3000`.
4. Рекомендуется: **Access → Applications** — та же политика Team Access, что у
   `regagent.honey-road.tech`. Вход в Grafana при этом остаётся.

### 2. Файлы на Mac

```sh
mkdir -p "$HOME/srv"
cp -R deploy/grafana "$HOME/srv/regagent_grafana"
cd "$HOME/srv/regagent_grafana"
cp .env.example .env
chmod 600 .env
```

В `.env` вписать:

- `TUNNEL_TOKEN` — токен из шага 1;
- `GRAFANA_ADMIN_PASSWORD` — любой длинный пароль (`openssl rand -base64 24`);
- `REGAGENT_LOGS_DIR` — полный путь к `logs` выкатки, без `~` и без `<user>`. Проще вписать командой:
  `sed -i '' "s|^REGAGENT_LOGS_DIR=.*|REGAGENT_LOGS_DIR=$HOME/srv/regagent_prod/logs|" .env`

Пустой `TUNNEL_TOKEN`, `GRAFANA_ADMIN_PASSWORD` или `REGAGENT_LOGS_DIR` остановит
`docker compose` с ошибкой — так и задумано.

### 3. Старт и проверка

```sh
cd "$HOME/srv/regagent_grafana"
docker compose up -d
docker compose ps
docker compose logs --tail 20 tunnel        # ждём "Registered tunnel connection"
docker compose logs --tail 20 alloy         # без ошибок про docker.sock и events.jsonl
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:13000/api/health   # 200
```

Открыть `https://grafana.honey-road.tech`, войти как `admin` с паролем из `.env`,
**Dashboards → RegAgent → RegAgent — события стенда**. Данные появятся после первых
вопросов в чате: Alloy читает логи с момента своего старта.

Быстрая проверка, что события доходят: **Explore** → Loki →
`{event="turn.result"}`.

## Что на дашборде

- Сверху сводка за выбранный период: ходы, отказы и «не найдено», токены LLM, ошибки,
  p95 времени хода, упавшие задачи индексации.
- Графики: ходы по исходам, время хода (p50/p95), токены по операциям
  (`guard`, `answer`, `comparison_check`, `index`, `keywords`, `embedding`), вызовы
  моделей по статусу, ошибки по событиям, индексация, пустой поиск и ограничение доступа.
  `search.request access_limited` означает, что запрос явно назвал документ вне прав
  сотрудника. По графику можно отличить этот случай от `search.request empty`.
  Для показа отдельной строки в Explore используйте `{event="search.request",status="access_limited"}`.
- Логи: решения выкатки и откаты, события с ошибкой, логи любого сервиса (переменная
  «Сервис» вверху, `GET /health` скрыт).

Поля событий — в [docs/LLD.md, «События»](../../docs/LLD.md#10-события). В запросах
поля читаются через `| json`; метками стали только `service`, `container`, `event`,
`status`. Тексты вопросов и ответов в события не пишутся (D-52), поэтому в Grafana их нет.

Дашборд можно менять в интерфейсе, но после перезапуска Grafana его перезапишет файл
`dashboards/regagent.json`. Чтобы сохранить правки: **Share → Export → Save to file**,
заменить файл в репозитории (`deploy/grafana/dashboards/regagent.json`) и скопировать в
`$HOME/srv/regagent_grafana/dashboards/`.

## Обслуживание

```sh
cd "$HOME/srv/regagent_grafana"
docker compose pull && docker compose up -d     # после смены версий в compose.yaml
docker compose restart grafana                  # после правки дашборда или источника
docker compose down                             # остановить; тома с данными остаются
```

- **Пароль admin** из `.env` применяется только при первом старте. Сменить потом:
  `docker compose exec grafana grafana cli admin reset-admin-password '<новый>'`
  и записать новый пароль в `.env`.
- **Обновление из репозитория:** заново скопировать `deploy/grafana` поверх
  `$HOME/srv/regagent_grafana` (`.env` в репозитории нет, он не перезапишется), затем
  `docker compose up -d`.
- **Место на диске:** Loki хранит 14 дней (`loki.yaml`, `retention_period`).
- **Docker socket** смонтирован в `alloy` только на чтение, но Docker API через него
  всё равно доступен целиком. В `alloy` не добавлять ничего, кроме сбора логов, и не
  публиковать его порт.

## Если что-то не так

| Симптом | Что проверить |
| --- | --- |
| 1033 на `grafana.honey-road.tech` при живом туннеле | DNS-запись `grafana` указывает не на этот туннель: нужна CNAME на `<ID туннеля>.cfargotunnel.com`, Proxied. ID — в `docker compose logs tunnel` (`tunnelID=`) |
| 502 на `grafana.honey-road.tech` | `docker compose logs tunnel`; service в Published application — `grafana:3000`; туннель отдельный от стенда |
| Дашборд пустой | `docker compose logs alloy`; стенд называется `regagent-prod` (`docker ps --filter name=regagent-prod`), иначе поменять `REGAGENT_PROJECT`; время на дашборде включает момент вопросов |
| Нет «Выкатка: решения» | в `$REGAGENT_LOGS_DIR` есть `events.jsonl` (появляется после первого решения выкатки) |
| `mkdir /host_mnt/Users/<user>: permission denied` при старте `alloy` | в `REGAGENT_LOGS_DIR` осталась заглушка — вписать настоящий путь |
| `permission denied` на `docker.sock` | Docker Desktop → Settings → Advanced → включено **Allow the default Docker socket to be used** |
