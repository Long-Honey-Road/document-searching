# Интеграция работы 27.11 (ветка `a/2026-11-27-sandbox`): что внутри, как влить, как проверить, что не проверено

Ветка лежит поверх `main` `e4450ce` и включает три слоя:

1. `5f80443` — работа Никиты (K-1…K-5, прогон качества Q, прогон демо M, проверка сравнения D-43);
2. `30da383…f1eafed` — `fix/demo-accounts`: гайд по Cloudflare Bypass, учётки `employee_*` в чек-листе демо, переменные knowledge в compose, закрытая админка Keycloak (D-47);
3. `b12c566…5da298b` — работа из песочницы, описанная ниже.

Номера решений заняты до **D-51**, техдолг — до **TD-03**. Следующие свободные: D-52, TD-04.

## 1. Что поменялось

| Коммит | Что | Где | Решение |
| --- | --- | --- | --- |
| `b12c566` | Инструмент агента `follow_refs` поверх `/refs`; модели `RefsRequest`/`RefsResponse`/`DocumentRef` — в `regagent_common.contracts` | backend `tools.py`, `knowledge_client.py`, `prompts/system.md`; knowledge `api.py`; common | D-48 |
| `b09a8cf`, `dc7fc85` | Проверка релиза входит под `employee_public` через форму Keycloak (через loopback), проверяет список диалогов и одного ассистента; аварийный выключатель `RELEASE_SIGN_IN_PROBE=off` | `deploy/prod/deploy.py` | D-49 |
| `62d7279` | `init-env` генерирует пароли и внутренние токены вместо заглушек | `deploy/prod/deploy.py` | — |
| `d694afc` | При старте knowledge: убрать недоделанные вставки LightRAG, снять `indexing` в реестре; `reindex --failed` | knowledge `rag.py`, `registry.py`, `pipeline.py`, `main.py`, `reindex.py` | D-50 |
| `9dce80c`, `8c26f94` | Служебный API knowledge `/admin/*` | knowledge `api.py`, `registry.py`, `main.py`; common | D-51, TD-03 |
| `4b4c482` | Прокси `/admin/*` в backend только для роли `admin` | backend `routes.py`, `knowledge_client.py` | D-51 |
| `f490cfa` | Флаг `force` — поле общего `IndexJob`, `IndexRequest` удалён | common, knowledge | D-45 |

### Изменения интерфейсов (важно для чужих веток)

- `regagent_knowledge.jobs.IndexRequest` **удалён**: вместо него `IndexJob(..., force=True)` из `regagent_common.contracts`. Ветки, где он импортируется, после мержа не соберутся.
- `RefsRequest`, `RefsResponse`, `DocumentRef` теперь импортируются из `regagent_common.contracts`, а не из `regagent_knowledge.api`.
- `build_search_tools(...)` возвращает **три** инструмента (`search_docs`, `read_clause`, `follow_refs`). Код с распаковкой `search, read = ...` сломается.
- Белый список инструментов модели в `test_graph.py` расширен на `follow_refs`.
- `ApiDeps` knowledge получил необязательное поле `admin: AdminDeps | None`. Пока его нет, `/admin/*` отвечает 503.
- Новые методы `Registry`: `list_jobs`, `get_job`, `interrupt_indexing`. `RagRegistry`: `discard_unfinished`.
- `queue_reindex(..., only_failed=False, doc_ids=None)` — новые необязательные параметры.

### Переменные окружения

| Переменная | Где | По умолчанию | Нужно ли менять на стенде |
| --- | --- | --- | --- |
| `INDEX_JOB_TIMEOUT_S`, `LIGHTRAG_LANGUAGE`, `LIGHTRAG_EXTRACTION_JSON` | knowledge (теперь передаются compose) | `1800`, `Russian`, `false` | нет |
| `RELEASE_SIGN_IN_PROBE` | только `.env` стенда, читает `deploy.py` | включено | только аварийно: `off` |
| `KEYCLOAK_EMPLOYEE_PUBLIC_PASSWORD` | `.env` стенда | — | **должен совпадать с паролем в Keycloak**, иначе релиз не пройдёт |

Схема БД: новых таблиц нет. Колонка `jobs.current_step` (коммит Никиты) добавляется `ALTER TABLE … IF NOT EXISTS` при старте knowledge, совместима с предыдущей версией.

## 2. Как влить

1. Забрать бандл и влить быстрой перемоткой, если `main` с `e4450ce` не двигался:
   ```sh
   git fetch ~/Downloads/regagent-sandbox-2026-11-27.bundle a/2026-11-27-sandbox:a/2026-11-27-sandbox
   git switch main && git merge --ff-only a/2026-11-27-sandbox
   ```
   Если `main` ушёл вперёд — `git rebase main a/2026-11-27-sandbox`. Конфликты возможны в
   `dev_docs/09-decisions.md` (номера D-…): перенумеровать новые решения после последнего в
   `main`, `tests/docs/test_decisions.py` проверит последовательность.
2. Ветку Никиты `origin/feat/regagent-quality-and-knowledge` отдельно вливать не нужно: её
   коммит уже внутри.
3. Проверки перед пушем — раздел 3.1. После пуша в GitHub смотреть CI: job `integration`
   впервые прогонит эти изменения на образе Postgres с AGE и RabbitMQ (D-41).
4. Выкатка — бриф `dev_docs/deploy-brief-2026-11-27.md`.

## 3. Как проверить

### 3.1. Автоматические проверки (у любого разработчика)

```sh
uv run ruff check . && uv run ruff format --check . && uv run mypy packages services
uv run pytest                                   # ожидается: 381 passed
(cd web && npm test -- --run && npm run lint && npm run build)
```

Integration и утечки — на настоящем образе Postgres по `deploy/RUNBOOK.md`, раздел 3 (там
поднимается `regagent-postgres` с AGE). Для теста consumer нужен ещё RabbitMQ:

```sh
docker run -d --name regagent-mq-test -p 127.0.0.1:5672:5672 \
  -e RABBITMQ_DEFAULT_USER=test -e RABBITMQ_DEFAULT_PASS=test rabbitmq:management
TEST_POSTGRES_HOST=127.0.0.1 TEST_POSTGRES_PORT=55432 TEST_POSTGRES_USER=knowledge \
TEST_POSTGRES_PASSWORD=k TEST_POSTGRES_DB=knowledge \
TEST_RABBITMQ_URL=amqp://test:test@127.0.0.1:5672/ \
uv run pytest -m "(integration or leak) and not e2e" services packages -v
docker rm -f regagent-mq-test
```

В песочнице прошло 56 тестов на Postgres 16 + pgvector с графом NetworkX. С AGE и
RabbitMQ — не запускалось (раздел 4).

Что покрыто тестами по новым частям:

| Часть | Тесты |
| --- | --- |
| `follow_refs`: уровни из токена, код документа или id фрагмента, одинаковый ответ «нет документа» и «нет прав», лимит длины, ссылки не становятся источниками | `services/backend/tests/test_tools.py`, `test_prompts.py`, `test_graph.py` |
| Вход в probe: весь путь через loopback с публичным `Host`, cookie, PKCE, неверный пароль без повторов, 503 Keycloak с повторами, 401 API, число ассистентов, выключатель, совместимость `deploy.py` с Python 3.9 | `deploy/tests/test_release.py` |
| Генерация секретов в `init-env`: пароли и токены, пароль внутри `RABBITMQ_URL`, внешний ключ не трогается, значения не печатаются | `deploy/tests/test_release.py` |
| Восстановление после перезапуска: недоделанный документ LightRAG удаляется, обработанный остаётся; `indexing` → `failed`/`retrying`; порядок «сначала LightRAG, потом реестр»; `reindex --failed` (вставка или удаление, один документ) | `test_rag_pg.py`, `test_registry_pg.py`, `test_pipeline.py`, `test_job_progress.py` |
| Служебный API knowledge: токен, список документов всех уровней, задачи с фильтром и лимитом, повтор (202/404/409), сверка ссылок, 503 до старта | `services/knowledge/tests/test_api.py`, `test_registry_pg.py` |
| Прокси backend: 401 без токена, 403 без роли (до knowledge не доходит), сервисный токен вместо пользовательского, коды knowledge, 502 при недоступности | `services/backend/tests/test_admin_routes.py` |
| Админка Keycloak закрыта прокси, проверка в probe | `deploy/tests/test_compose.py`, `test_release.py` |

### 3.2. Проверка на стенде

Порядок и команды — в брифе выкатки (`dev_docs/deploy-brief-2026-11-27.md`). Что должно
получиться:

| Проверка | Ожидается |
| --- | --- |
| Релиз | `deploy.log`: «Принят <SHA>», в `state.json` `good` = новый SHA |
| Админка Keycloak снаружи | `/auth/admin/` и `/auth/realms/master/...` → 404 (или 302 на Cloudflare Access) |
| Переиндексация после смены языка | все документы `indexed`, сущности в графе на русском |
| Восстановление | перезапуск knowledge посреди переиндексации → в логе «удалены недоделанные вставки…», документ доходит до `indexed` после повторной доставки или `reindex --failed` |
| Служебный API | `GET /api/admin/jobs` под `admin` → 200, под `employee_public` → 403 |
| `follow_refs` | вопрос по пункту со ссылкой на другой регламент → в источниках ответа пункт связанного документа |
| Сравнение (D-43) | вопрос про субподрядчика по двум областям без утверждения «запрос не требуется»; либо fallback |

## 4. Что не сделано или не проверено из-за условий песочницы

В песочнице нет Docker и реестров образов, сеть закрыта (ни реестров пакетов, ни внешних
сайтов), нет доступа к стенду, Cloudflare и серверу модели.

| Что | Почему не сделано | Кто и как закрывает |
| --- | --- | --- |
| Integration-тесты на AGE (`PGGraphStorage`) | нет образа `deploy/postgres`, графы шли на NetworkX | CI job `integration` после пуша, или RUNBOOK раздел 3 |
| Тест consumer с RabbitMQ (`test_consumer_pg_mq.py`) | нет RabbitMQ | CI или команда из 3.1 |
| Caddyfile с блокировкой админки вживую | нет Caddy; проверен только текст конфига | первый релиз: probe требует 404 на админке, иначе откат |
| Вход probe против настоящего Keycloak | проверен на подставном Keycloak | первый релиз; при несовместимости — `RELEASE_SIGN_IN_PROBE=off` и `deploy.log` разработчику |
| `deploy.py` на системном Python 3.9 macOS | в песочнице 3.11+, проверен только синтаксис 3.9 | первый запуск LaunchAgent после `install.sh` |
| Сборка образов (knowledge, backend, web) | нет Docker | сборка в релизе |
| Восстановление после настоящего убийства процесса | в тесте недоделанный документ создаётся постановкой в очередь LightRAG без обработки | ручная проверка на стенде (бриф, шаг 7) |
| Переиндексация корпуса после смены языка | нет стенда | бриф, шаг 6 |
| K-4: почему модель не ставит `<|COMPLETE|>` | нужен сырой ответ модели из кэша стенда | бриф, шаг 8: выгрузка кэша, разбор — knowledge |
| D-43 и `follow_refs` на живой модели | нет стенда и модели | бриф, шаг 9 |
| Прогон качества Q (`scripts/eval`) | нет стенда; до Bypass скрипт не проходил Cloudflare Access | после Bypass — с любой машины по `eval/README.md` |
| Остаток ручного прогона M (сценарии 1, 4, 7, 8) | нет входа на стенд | человек по `dev_docs/demo-checklist.md` |
| Приложение Access на пути админки Keycloak | Cloudflare только через дашборд | необязательно после D-47; шаг 3 `deploy/prod/cloudflare-bypass.md` |
| Ограничение запросов по IP (P0-3) | плагин Caddy собирается с сетью | правило Rate limiting в Cloudflare (WAF) на `/api/*` и `/auth/*` |
| TD-02: PDF через Docling | модели Docling скачиваются из сети | этап 2, образ knowledge с моделями |
| TD-03: решения по сверке ссылок | нужно хранилище решений — отдельная задача | P2-B2 или P2-D2 |
| Автообновление `deploy.py` и автопереиндексация при смене настроек | отложено по решению 27.11 | при необходимости: обновлять `bin/` из `origin/main` после проверки синтаксиса; отпечаток настроек извлечения в реестре |

Мелочи, найденные при самопроверке и оставленные:

- на 404/409 от knowledge backend отвечает общей фразой «rejected by knowledge», без причины от knowledge;
- `follow_refs` не ограничен по числу вызовов (лимит есть только у `search_docs`), ответ ограничен `TOOL_RESULT_MAX_CHARS`.
