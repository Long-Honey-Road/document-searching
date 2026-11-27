# Runbook: проверка и развёртывание на хосте

Для агента или человека с доступом к машине стенда (Docker, сеть). Состояние — после мержа 27.11 (`dev_docs/merge-log-2026-11-27.md`). Правила работы в репо — `AGENTS.md`: в `main` без команды человека не мержить, при вопросах остановиться и спросить.

## 0. Что сейчас готово

| Часть | Готово | Не готово (стенд на этом остановится) |
| --- | --- | --- |
| Инфраструктура (агент 3) | compose, Postgres с pgvector и AGE, RabbitMQ, Keycloak с realm и 4 учётками, Caddy, CI, мини-корпус, e2e-тесты | — |
| web (агент 3) | вход по OIDC (PKCE), чат со стримом и карточками источников | — |
| backend (агент 2) | проверка JWT по JWKS, пользователь и уровни из токена, закрытый `/store` | инструменты (P1-C2: есть только документы), память (P1-C3), граф агента (P1-C4) — сейчас граф-заглушка |
| knowledge (агент 1) | библиотечный уровень: пакет `common`, реестр LightRAG с изоляцией уровней, манифест, реестр документов, парсинг DOCX | нормализация (P1-A4), OKF (P1-A5), конвейер (P1-A6), поиск с проверкой прав и канарейки (P1-B2), **API (P1-B3)** |

**Чего ждать.** Контейнер `knowledge` не стартует: в образе нет `regagent_knowledge.main:app`, это P1-B3. Он будет перезапускаться с красным healthcheck. Сценарии чата и `pytest -m e2e` до P1-B3 и P1-C4 не пройдут. Это ожидаемо, такие падения — не ошибка развёртывания.

Сейчас на хосте проверяем всё, что от этих задач не зависит. В первую очередь — граф в AGE: его ни разу не запускали (§3).

## 1. Требования к хосту

- Docker с Compose v2, `uv`, Python 3.12 (uv поставит сам), Node 22 (только для проверки `web` вне Docker).
- Сеть при сборке: PyPI и `download.pytorch.org` (torch CPU для Linux), Docker Hub (`pgvector/pgvector`, `postgres`, `caddy`, `rabbitmq`), `quay.io` (Keycloak), `ghcr.io` (TEI, uv), `github.com` (исходники AGE при сборке образа Postgres), `openaipublic.blob.core.windows.net` (словарь tiktoken при сборке knowledge, D-31), Hugging Face (модель реранкера TEI при первом старте).
- Ollama на хосте с моделью эмбеддингов: `ollama pull bge-m3`. На Linux Ollama должна слушать не только loopback (`OLLAMA_HOST=0.0.0.0`), а порт 11434 надо закрыть снаружи файрволом. Контейнеры ходят на неё через `host.docker.internal` (в compose есть `host-gateway`).
- Адрес и ключ сервера модели (LLM), OpenAI-совместимый.
- Apple Silicon: в `.env` задать `TEI_IMAGE=ghcr.io/huggingface/text-embeddings-inference:cpu-arm64-1.10`.

## 2. Подготовка и проверки без стенда

```bash
git status                      # чисто, ветка main
uv lock --check                 # ВАЖНО: uv.lock при мерже правили вручную (pgvector)
#   если не сходится: uv lock && git add uv.lock && git commit -m "chore: relock"
uv sync --all-packages --frozen
cp .env.example .env            # заполнить, см. ниже
uv run ruff check . && uv run ruff format --check . && uv run mypy packages services
uv run pytest                   # ожидается: 160 passed, 17 deselected
(cd web && npm ci && npm test && npm run lint && npm run build)
docker compose --env-file .env -f deploy/compose.yaml config -q
```

Что заполнить в `.env`:
- `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`;
- все `*_PASSWORD` и `KNOWLEDGE_INTERNAL_TOKEN` (вместо `change-me`);
- пароль внутри `RABBITMQ_URL`, совпадающий с `RABBITMQ_PASSWORD`;
- четыре `KEYCLOAK_*_PASSWORD` демо-учёток (вместо `replace-me`), все разные;
- `PUBLIC_BASE_URL` — адрес, по которому стенд откроют в браузере, без слеша в конце.

Никогда не задавайте `POSTGRES_WORKSPACE` (D-30): эта переменная сводит все уровни доступа в один workspace.

## 3. Integration-тесты на настоящем образе Postgres (AGE) — первым делом

Спайк и тесты агента 1 шли без AGE. Здесь проверяются изоляция графа по workspace и работа AGE от пользователя `knowledge` без прав суперпользователя.

```bash
docker build -t regagent-postgres deploy/postgres
docker run -d --name regagent-pg-test -p 127.0.0.1:55432:5432 \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=pg \
  -e APP_DB=app -e APP_DB_USER=backend -e APP_DB_PASSWORD=app \
  -e KNOWLEDGE_DB=knowledge -e KNOWLEDGE_DB_USER=knowledge -e KNOWLEDGE_DB_PASSWORD=k \
  -e KEYCLOAK_DB=keycloak -e KEYCLOAK_DB_USER=keycloak -e KEYCLOAK_DB_PASSWORD=kc \
  regagent-postgres
# дождаться готовности
until docker exec regagent-pg-test pg_isready -U postgres; do sleep 1; done

# 3.1 версии расширений: vector должен быть не ниже 0.8 (D-33)
docker exec regagent-pg-test psql -U postgres -d knowledge -Atc \
  "select extname, extversion from pg_extension order by 1"

# 3.2 AGE от пользователя knowledge (не суперпользователь)
docker exec -e PGPASSWORD=k regagent-pg-test psql -h 127.0.0.1 -U knowledge -d knowledge -v ON_ERROR_STOP=1 -c \
  "LOAD 'age'; SET search_path = ag_catalog, \"\$user\", public; SELECT create_graph('rb_check'); SELECT drop_graph('rb_check', true);"

# 3.3 integration и leak с графом в AGE (по умолчанию PGGraphStorage)
TEST_POSTGRES_HOST=127.0.0.1 TEST_POSTGRES_PORT=55432 TEST_POSTGRES_USER=knowledge \
TEST_POSTGRES_PASSWORD=k TEST_POSTGRES_DB=knowledge \
uv run pytest -m "(integration or leak) and not e2e" services packages -v
# ожидается: 11 passed

docker rm -f regagent-pg-test
```

Что делать при падении:
- **3.1** показывает `vector` ниже 0.8. Записать версию и сообщить: нужен другой базовый образ.
- **3.2** падает на `LOAD 'age'`, ошибка про права. Варианты исправления: `ALTER DATABASE knowledge SET session_preload_libraries = 'age'` от суперпользователя или положить `age.so` в `$libdir/plugins`. Без согласования с человеком в `main` не коммитить.
- **3.3**, падает `test_two_workspaces_do_not_see_each_other`. Это утечка между уровнями доступа: **остановиться и сообщить человеку**, ничего не обходить.
- **3.3**, падают остальные тесты. Приложить вывод `pytest -v` и `docker logs regagent-pg-test`.

## 4. Развёртывание стенда

```bash
uv run python scripts/synth/generate.py          # мини-корпус в data/docs (7 документов + manifest.yaml)
docker compose --env-file .env -f deploy/compose.yaml --profile local-reranker up -d --build
docker compose --env-file .env -f deploy/compose.yaml ps
```

Профили:
- `local-reranker` — только TEI-реранкер, эмбеддинги идут в Ollama на хосте;
- `local-embeddings` — TEI и для эмбеддингов: тогда `EMBEDDINGS_BASE_URL=http://embeddings:80` и модель Hugging Face в `EMBEDDINGS_MODEL`;
- `dev` — Mailpit.

Ожидаемое состояние сейчас:

| Сервис | Ожидание |
| --- | --- |
| `postgres`, `rabbitmq` | `healthy` |
| `keycloak` | запущен; первый старт импортирует realm (до минуты) |
| `backend` | запущен |
| `web`, `proxy` | запущены |
| `reranker` | запущен, при первом старте качает модель |
| `knowledge` | **перезапускается / unhealthy — ожидаемо до P1-B3** (в логах нет модуля `regagent_knowledge.main`) |

## 5. План проверки на устройстве

Каждый пункт: команда → ожидаемый результат. Записывать фактический результат в отчёт (§6).

| № | Проверка | Как | Ожидание сейчас |
| --- | --- | --- | --- |
| 1 | Статический анализ и unit | §2 | всё зелёное, 160 passed |
| 2 | web вне Docker | §2 | тесты, lint, build проходят |
| 3 | AGE и integration/leak | §3 | 11 passed, `vector` ≥ 0.8, `LOAD 'age'` работает от `knowledge` |
| 4 | Сборка образов | `docker compose … build` | все образы собираются. В логе knowledge — шаг `lightrag-download-cache` без ошибок |
| 5 | Кэш tiktoken в образе | `docker compose … run --rm --no-deps --entrypoint sh knowledge -c 'ls $TIKTOKEN_CACHE_DIR'` | непустой каталог |
| 6 | Доступ к Ollama из контейнера | `docker compose … run --rm --no-deps --entrypoint python knowledge -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:11434/api/tags',timeout=5).status)"` | `200` |
| 7 | Сервисы подняты | §4, `ps` | как в таблице §4 |
| 8 | Ничего лишнего наружу | `docker compose … ps --format '{{.Name}} {{.Ports}}'` | порт опубликован только у `proxy`: `127.0.0.1:8080` |
| 9 | OIDC discovery | `curl -s $PUBLIC_BASE_URL/auth/realms/regagent/.well-known/openid-configuration \| python -m json.tool \| grep issuer` | `issuer` = `KEYCLOAK_ISSUER` |
| 10 | robots | `curl -s $PUBLIC_BASE_URL/robots.txt` | `Disallow: /` |
| 11 | backend жив | `curl -s -o /dev/null -w '%{http_code}' $PUBLIC_BASE_URL/api/health` | `200` |
| 12 | backend без токена | `curl -s -o /dev/null -w '%{http_code}' -X POST $PUBLIC_BASE_URL/api/threads` | `401` или `403` |
| 13 | Вход в web | браузер → `PUBLIC_BASE_URL` → вход `employee_public` | вход проходит, чат открывается. Ответов по документам пока нет (граф-заглушка, knowledge не готов) |
| 14 | Саморегистрация выключена | страница входа Keycloak | ссылки «Register» нет |
| 15 | Сквозные сценарии | `uv run pytest -m e2e` (см. `deploy/README.md`) | **сейчас падают — ожидаемо**. Цель после P1-B3 и P1-C4: сценарии 1, 2, 4, 7, 8 под `employee_public` и `employee_fin`, все ответы со ссылками на пункты, тесты на утечки зелёные |

## 6. Что вернуть людям

Файл `dev_docs/host-check-<дата>.md` (в ветке, не в `main`) с:
- коммитом `main`, на котором проверяли, ОС и архитектурой хоста, версиями Docker, Compose и Ollama;
- результатом по каждому пункту §5: «как ожидалось» или фактический вывод;
- выводом §3.1 (версии расширений) и §3.3 (`pytest -v`);
- при падениях — `docker compose … logs --no-color <сервис> | tail -200`;
- если делали `uv lock` — отдельным коммитом в ветке, с пометкой в отчёте.

## 7. Частые проблемы

- **`uv lock --check` не сходится** — ожидаемо после ручной правки, см. §2.
- **Сборка knowledge падает на `lightrag-download-cache`**: нет доступа к `openaipublic.blob.core.windows.net`. Открыть доступ. Выключать шаг нельзя: без словаря knowledge будет ходить в интернет при старте (D-31).
- **Вход отбрасывает обратно или Keycloak ругается на redirect URI**: адрес в браузере не совпадает с `PUBLIC_BASE_URL`. Используйте ровно его, `127.0.0.1` перенаправляется на него.
- **Пароли учёток не меняются после правки `.env`**: realm импортируется только при первом старте. Меняйте в админке Keycloak или пересоздайте том `pgdata` (сотрёт все данные).
- **`host.docker.internal` не резолвится**: проверьте `extra_hosts` в compose и что Ollama слушает не только `127.0.0.1`.
- **Стенд не открывается с другой машины**: `proxy` в compose слушает только `127.0.0.1:8080`, так задумано для локального стенда. Чтобы открыть стенд наружу, нужно поменять привязку порта и `PUBLIC_BASE_URL`, включить TLS и поставить ограничение по IP в Caddy. Делать только по согласованию с человеком: вход по учёткам обязателен, открытый стенд с LLM жжёт токены.
- **TEI на Mac очень медленный**: не тот `TEI_IMAGE`, см. §1.
