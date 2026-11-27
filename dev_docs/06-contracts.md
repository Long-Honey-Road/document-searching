# 06. Контракты

Контракты — общая точка для параллельной работы. Межсервисные Pydantic-модели живут в пакете `regagent-common` (`packages/common`). HTTP-модели ОКК принадлежат backend. Любое изменение контракта — вместе с тестом.

## knowledge API

Внутренний, только из backend. Каждый запрос несёт заголовок `X-Service-Token: <KNOWLEDGE_INTERNAL_TOKEN>`. Уровни пользователя передаются явно телом запроса — backend вычисляет их из токена, knowledge им не доверяет вслепую: всё равно сверяет каждый фрагмент с реестром.

### `POST /search`

```json
{"query": "кто назначает РП", "levels": ["public"], "areas": ["ДИБ"], "top_k": 8}
```

```json
{
  "fragments": [
    {"fragment_id": "dib-project-activity@3#2.7", "doc_id": "dib-project-activity", "version": 3,
     "title": "Проектная деятельность Дирекции ИБ", "clause": "2.7", "area": "ДИБ",
     "kind": "regulation", "text": "...", "score": 0.83}
  ],
  "areas_found": ["ДИБ", "ДБРЦТ", "общий"],
  "areas_diverge": true,
  "low_confidence": false
}
```

`fragment_id` = `<doc_id>@<version>#<clause>` для регламентов, `practice:<practice_id>` для практики. У фрагментов практики `version` может быть `null` (D-25). `top_k` необязателен: `null` — `SEARCH_TOP_K` knowledge.

### `POST /clause`

`{"doc_id", "clause", "version": null, "levels": [...]}` → `{"fragment": {...}, "neighbors": [{...}]}`. `version: null` — действующая редакция. Нет прав или нет пункта → 404 (одинаково, чтобы не раскрывать существование документа).

### `POST /refs`

`{"doc_id", "levels"}` → `{"outgoing": [{"doc_id", "title", "clause", "via"}], "incoming": [...]}` — только доступные. Модели — `RefsRequest`, `RefsResponse`, `DocumentRef` в `regagent_common.contracts`. `clause` — пункт документа-источника, где упомянут целевой документ. Нет документа или нет прав → 404.

### Служебные (`/admin/...`)

По сервисному токену, как остальные маршруты knowledge. Модели — в `regagent_common.contracts`.

- `GET /admin/documents` → `{"documents": [{doc_id, version, level, areas, title, status, error, updated_at}]}` — все уровни.
- `GET /admin/jobs?status=failed&limit=100` → `{"jobs": [{job_id, op, doc_id, version, status, attempts, error, current_step, steps, created_at, updated_at}]}`, свежие первыми; `limit` 1…500.
- `POST /admin/jobs/{id}/retry` → 202 `{"queued": n}`: повтор документа задачи по текущему манифесту, как `reindex --failed`. 404 — задачи нет; 409 — задача или документ не в `failed`.
- `GET /admin/review` → `{"items": [{doc_id, version, title, clause, via, kind: "ambiguous"|"missing", candidates}]}` — ссылки по названию на сверку из OKF.
- `POST /admin/review/{id}` (`accept|reject|edit`) и глоссарий в `/admin/review` — не сделаны (TD-03, P2-B2).

Пока служебная часть не готова (старт knowledge), `/admin/*` отвечает 503. `GET /health` — без токена.

## RabbitMQ

Exchange `regagent` (topic). Все сообщения — JSON с полями `message_id`, `created_at`, `type`.

| Ключ / очередь | Кто пишет | Кто читает | Тело |
| --- | --- | --- | --- |
| `index.jobs` | knowledge.watcher, knowledge.reindex | knowledge.consumer | `{job_id, op: "upsert"\|"delete", doc_id, version, path, sha256}`. У `delete` `path` и `sha256` необязательны (D-26). `force` (по умолчанию `false`) — поле `IndexJob` в `regagent_common`: переиндексация ставит `true` у `upsert` (D-45), обработчик тогда не пропускает уже проиндексированный документ. |
| `practice.publish` | backend (ОКК) | Knowledge (поток A) | `{ticket_id, practice_id, revision, text, mode, binds: [{doc_id, version, clause}], admin_id, admin_levels}` |
| `practice.published` | Knowledge (поток A) | backend | `{ticket_id, practice_id, revision, status, error_code}`. Статус `published` или `failed`. При ошибке требуется машинный код причины. |
| `qc.events` | Knowledge (поток A) | backend | `{kind: "recheck", reason, doc_id, version, practice_ids}`. Причина `new_version`, `access_changed` или `deleted`. При удалении `version` может быть `null`. Если одновременно изменилась редакция и доступ, Knowledge передаёт `access_changed`. В кабинете для изменения доступа и удаления показывается код ранее привязанного документа. |

Для очереди индексации используется `regagent.dlq`. Для ОКК backend объявляет устойчивые очереди `practice.publish` и `qc.events`, ко второй привязаны ключи `practice.published` и `qc.events`. У очереди событий один активный потребитель и очередь ошибок `qc.events.dlq`. Сообщение несёт UUID `message_id`, UTC `created_at` и `type`, равный ключу маршрутизации. `ticket_id`, `practice_id` и элементы `practice_ids` — UUID. `revision` и редакция документа — целые числа от 1. Режим `clarifies` или `overrides`. Backend повторяет отправку того же сообщения до подтверждения брокера. Knowledge публикует результат до событий перепроверки той же новой практики и обеспечивает идемпотентность по `(practice_id, revision)`, подтверждает публикацию после индексации и проверяет все привязки и права. Повторы результатов и событий перепроверки безопасны на стороне backend. Схемой общего протокола и потребителем команды владеет поток A. До его подключения поток B проверяется на имитаторе.

## Ответ агента

Итоговое сообщение агента — markdown со ссылками `[f:<fragment_id>]`. В метаданных сообщения:

```json
{"verified": true, "area_used": "ДИБ", "area_source": "memory|question|asked|none",
 "sources": [{"n": 1, "fragment_id": "...", "title": "...", "clause": "2.7", "version": 3, "kind": "regulation"}],
 "outcome": "answer|refusal|not_found|ask_area|fallback"}
```

web заменяет `[f:...]` на номера `[1]` по `sources`.

Где лежит (D-19): итоговое сообщение — AIMessage из узла `finalize`, `AnswerMeta` — в `additional_kwargs.answer_meta`. `assistant_id` — `regagent`. Публичный стрим доставляет проверенный итог. Вызовы инструментов, результаты поиска и черновик модели остаются во внутреннем запуске (D-35).

## Память

Store namespace `("users", sub, "memory")`, ключ `area:ДИБ`, значение `{kind, value, source, confirmed_at, times_confirmed}`.

## backend HTTP (свои маршруты)

| Метод | Путь | Роль | Что делает |
| --- | --- | --- | --- |
| GET | `/me/memory` | любой | `{"records": [...]}`: свои записи с полем `key` |
| DELETE | `/me/memory/{key}` | любой | удалить свою запись, `204` также при её отсутствии |
| GET | `/qc/me` | вошедший | `{can_review, areas}` |
| POST | `/qc/tickets` | вошедший | `{submission_id, question, comment?}` → своя заявка; повтор ключа возвращает ту же заявку |
| GET | `/qc/tickets` | вошедший | свои заявки, статус и время |
| GET | `/qc/admin/tickets` | admin | очередь с фильтром `status`, `limit`, `offset` |
| GET | `/qc/admin/tickets/{id}` | admin | детали заявки и история |
| POST | `/qc/admin/tickets/{id}/publish` | admin | `{text, mode, binds}` → статус `publishing` |
| POST | `/qc/admin/tickets/{id}/reject` | admin | переход в `rejected` |
| GET | `/qc/admin/analytics` | admin | `days`, `area`, `outcome`, `ticket_status`, `limit`, `offset` → сводка и группы |
| POST | `/qc/admin/analytics/{run_id}/ticket` | admin | создать заявку из неотвеченной группы |
| * | `/admin/*` | admin | прокси служебного API knowledge |

За пределами доступных уровней заявки и ходы отвечают 404. Недопустимый переход отвечает 409. Валидация выполняется на сервере. `submission_id` в чате совпадает с ID пользовательского сообщения и связующим полем `message_id` аналитики.

Для потока C backend предоставляет внутреннюю функцию `regagent_backend.qc_analytics.record_turn(turn, identity)`. `TurnOutcome` принимает UUID `run_id`, UTC `occurred_at`, `question`, `message_id` (ID сообщения, если есть), `area` (код или null), `fragment_ids`, `used_practice`, `outcome` и `reason` (необязательный машинный код). Исходы: `answer`, `empty_search`, `insufficient_evidence`, `clarification`, `access_limited`, `search_unavailable`, `model_timeout`, `refusal`. `access_limited` передаётся только по подтверждённому сигналу от поиска. На пустой выдаче используется `empty_search`. Функция берёт доступы из серверной `UserIdentity`, при сбое возвращает `false` и пишет техническое событие без текста.

## Переменные `.env`

| Переменная | Назначение | Пример |
| --- | --- | --- |
| `ENV` | dev / stand | `dev` |
| `LOG_LEVEL` | уровень логов | `INFO` |
| `PUBLIC_BASE_URL` | внешний адрес стенда (ссылки в письмах) | `https://regs.example.org` |
| `LLM_BASE_URL`, `LLM_API_KEY` | внешний OpenAI-совместимый сервер | |
| `LLM_MODEL` | модель по умолчанию | |
| `LLM_AGENT_MODEL`, `LLM_GUARD_MODEL`, `LLM_EXTRACT_MODEL`, `LLM_KEYWORD_MODEL`, `LLM_JUDGE_MODEL` | переопределения по ролям (пусто = `LLM_MODEL`) | |
| `GUARD_MODE` | формат структурированного ответа guard, `json_schema` или `tool` | `json_schema` |
| `LLM_TIMEOUT_S`, `LLM_MAX_CONCURRENCY` | таймаут и параллелизм | `120`, `8` |
| `LLM_EXTRA_BODY_JSON` | доп. параметры запроса, зависящие от сервера (например отключение рассуждений) | `{"chat_template_kwargs":{"enable_thinking":false}}` |
| `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_MODEL`, `EMBEDDINGS_DIM` | эмбеддинги | `http://embeddings:80`, `BAAI/bge-m3`, `1024` |
| `TEI_IMAGE` | Образ TEI для архитектуры хоста (`cpu-arm64-1.10` на Apple Silicon) | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.10` |
| `RERANK_MODE` | `embeddings` переиспользует модель эмбеддингов для демо, `tei` включает отдельный реранкер | `embeddings` |
| `RERANK_BASE_URL`, `RERANK_MODEL`, `RERANK_MIN_SCORE` | реранкер и порог | |
| `RERANK_CANDIDATE_LIMIT`, `RERANK_TEXT_MAX_CHARS` | предел числа кандидатов и длины релевантного отрывка для одного кандидата в реранке | `24`, `384` |
| `POSTGRES_HOST`, `POSTGRES_PORT` | Postgres | |
| `APP_DB`, `APP_DB_USER`, `APP_DB_PASSWORD` | БД backend | |
| `KNOWLEDGE_DB`, `KNOWLEDGE_DB_USER`, `KNOWLEDGE_DB_PASSWORD` | БД knowledge | |
| `KEYCLOAK_DB`, `KEYCLOAK_DB_USER`, `KEYCLOAK_DB_PASSWORD` | БД Keycloak | |
| `RABBITMQ_URL` | брокер | |
| `KEYCLOAK_ISSUER`, `KEYCLOAK_AUDIENCE`, `KEYCLOAK_ADMIN`, `KEYCLOAK_ADMIN_PASSWORD` | OIDC и админ Keycloak | |
| `KEYCLOAK_JWKS_URL` | ключи Keycloak по внутреннему адресу (`iss` проверяется по `KEYCLOAK_ISSUER`) | `http://keycloak:8080/auth/realms/regagent/protocol/openid-connect/certs` |
| `JWKS_CACHE_TTL_S`, `JWKS_MIN_REFRESH_S`, `JWT_LEEWAY_S` | срок кэша ключей, минимальный интервал внеочередного обновления (новый `kid`, Keycloak недоступен), допуск по времени токена | `300`, `30`, `30` |
| `AREA_CODES` | коды областей через запятую — справочник backend на этап 1 (D-20) | `ДИБ,ДБРЦТ,общий` |
| `WEB_DEV_BASE_URL` | Дополнительный origin для Vite в realm | `http://localhost:5173` |
| `KEYCLOAK_EMPLOYEE_PUBLIC_PASSWORD`, `KEYCLOAK_EMPLOYEE_FIN_PASSWORD`, `KEYCLOAK_QC_EXPERT_PASSWORD`, `KEYCLOAK_ADMIN_DEMO_PASSWORD` | Пароли именных учёток, импортируемые при создании demo realm | |
| `ACCESS_LEVELS_FILE` | YAML уровней доступа | `config/access_levels.yaml` |
| `KNOWLEDGE_URL`, `KNOWLEDGE_INTERNAL_TOKEN` | адрес и токен knowledge | |
| `KNOWLEDGE_TIMEOUT_S` | ожидание ответа knowledge, с (D-24) | `30` |
| `DOCS_DIR`, `OKF_DIR` | папки документов и OKF | `/data/docs`, `/data/okf` |
| `WATCH_DEBOUNCE_S`, `INDEX_MAX_RETRIES` | индексация | `3`, `3` |
| `INDEX_JOB_TIMEOUT_S` | тайм-аут одной попытки индексации, с | `1800` |
| `CHUNK_MAX_TOKENS` | максимум токенов во фрагменте | `1200` |
| `SEARCH_TOP_K`, `SEARCH_MODE` | поиск LightRAG | `8`, `mix` |
| `LIGHTRAG_WORKING_DIR` | рабочий каталог LightRAG (якорь конфигурации хранилищ); на стенде — постоянный том | `/data/lightrag` |
| `LIGHTRAG_EXTRACT_GLEANING` | повторные проходы извлечения сущностей (каждый удваивает вызовы LLM) | `1` |
| `LIGHTRAG_LANGUAGE` | язык извлечения сущностей и связей | `Russian` |
| `LIGHTRAG_EXTRACTION_JSON` | извлечение в JSON, требует поддержки `json_object` на сервере модели | `false` |
| `AGENT_MAX_STEPS` | предел шагов Deep Agent | `12` |
| `AGENT_MAX_SEARCHES`, `TOOL_RESULT_MAX_CHARS` | поисков на один ответ; текста фрагментов модели за один поиск, символов (D-24) | `3`, `12000` |
| `MEMORY_AREA_TTL_DAYS`, `MEMORY_MAX_ACTIVE_AREAS` | память | `14`, `3` |
| `QUESTION_MAX_CHARS`, `RATE_LIMIT_PER_USER_PER_MIN` | лимиты | `2000`, `20` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | почта | |
| `AUDIT_RETENTION_DAYS` | срок хранения аналитики ОКК, в днях | `90` |
| `VITE_ASSISTANT_ID` | ID графа или ассистента в Aegra. Пустое значение подходит для стенда с одним ассистентом | |
| `VITE_API_URL`, `VITE_OIDC_AUTHORITY`, `VITE_OIDC_CLIENT_ID` | web (на этапе сборки) | |

Переменные самой Aegra задаются в compose и отображаются на наши: `PORT=8000` (по умолчанию в Aegra 0.12 — 2026), `AEGRA_CONFIG=<путь к services/backend/aegra.json в образе>`, `AUTH_TYPE=custom`, `DATABASE_URL=postgresql://${APP_DB_USER}:${APP_DB_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${APP_DB}` (D-17, D-19).
