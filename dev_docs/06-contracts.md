# 06. Контракты

Контракты — общая точка для параллельной работы. Pydantic-модели всех схем ниже живут в пакете `regagent-common` (`packages/common`), сервисы импортируют их оттуда. Любое изменение контракта — вместе с тестом.

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

`fragment_id` = `<doc_id>@<version>#<clause>` для регламентов, `practice:<practice_id>` для практики. У фрагментов практики `version` может быть `null` (D-17). `top_k` необязателен: `null` — `SEARCH_TOP_K` knowledge.

### `POST /clause`

`{"doc_id", "clause", "version": null, "levels": [...]}` → `{"fragment": {...}, "neighbors": [{...}]}`. `version: null` — действующая редакция. Нет прав или нет пункта → 404 (одинаково, чтобы не раскрывать существование документа).

### `POST /refs`

`{"doc_id", "levels"}` → `{"outgoing": [{"doc_id", "title", "clause", "via"}], "incoming": [...]}` — только доступные.

### `GET /areas`

Справочник областей из манифеста: `{"areas": [{"code": "ДИБ", "title": "Дирекция ИБ", "synonyms": [...]}]}`. С сервисным токеном. backend по нему проверяет значение `area` перед записью в память (D-20).

### Служебные (`/admin/...`)

`GET /admin/documents`, `GET /admin/jobs?status=failed`, `POST /admin/jobs/{id}/retry`, `GET /admin/review` (глоссарий и ссылки на сверку), `POST /admin/review/{id}` (`accept|reject|edit`). `GET /health` — без токена.

## RabbitMQ

Exchange `regagent` (topic). Все сообщения — JSON с полями `message_id`, `created_at`, `type`.

| Ключ / очередь | Кто пишет | Кто читает | Тело |
| --- | --- | --- | --- |
| `index.jobs` | knowledge.watcher | knowledge.consumer | `{job_id, op: "upsert"|"delete", doc_id, version, path, sha256}`; у `delete` `path` и `sha256` необязательны (D-18) |
| `practice.publish` | backend (ОКК) | knowledge.consumer | `{practice_id, text, binds: [{doc_id, clause}], mode, areas, verified: {by, at}, source_cluster_id}` |
| `practice.published` | knowledge | backend | `{practice_id, source_cluster_id, status: "indexed"|"failed", error}` |
| `qc.events` | knowledge | backend | `{kind: "recheck", reason: "new_version"|"deleted", doc_id, practice_ids: [...]}` |

Очередь ошибок `regagent.dlq`; до 3 повторов с паузой; обработчики идемпотентны.

## Ответ агента

Итоговое сообщение агента — markdown со ссылками `[f:<fragment_id>]`. В метаданных сообщения:

```json
{"verified": true, "area_used": "ДИБ", "area_source": "memory|question|asked|none",
 "sources": [{"n": 1, "fragment_id": "...", "title": "...", "clause": "2.7", "version": 3, "kind": "regulation"}],
 "outcome": "answer|refusal|not_found|fallback"}
```

web заменяет `[f:...]` на номера `[1]` по `sources`.

## Память

Store namespace `("users", sub, "memory")`, ключ `area:ДИБ`, значение `{kind, value, source, confirmed_at, times_confirmed}`.

## backend HTTP (свои маршруты)

| Метод | Путь | Роль | Что делает |
| --- | --- | --- | --- |
| GET | `/me/memory` | любой | своя память |
| DELETE | `/me/memory/{key}` | любой | удалить запись |
| GET | `/me/notifications` | любой | уведомления |
| POST | `/feedback` | любой | `{thread_id, message_id, kind: "contradiction"|"missing", comment}` → элемент ОКК |
| GET | `/qc/clusters` | qc | очередь |
| GET | `/qc/clusters/{id}` | qc | кластер с вопросами и черновиком |
| POST | `/qc/clusters/{id}/draft` | qc | сгенерировать черновик ответа |
| POST | `/qc/clusters/{id}/publish` | qc | `{text, binds, mode, areas}` |
| POST | `/qc/clusters/{id}/reject` | qc | закрыть без практики |
| * | `/admin/*` | admin | прокси служебного API knowledge |

Ошибки — RFC 9457 (`application/problem+json`).

## Переменные `.env`

| Переменная | Назначение | Пример |
| --- | --- | --- |
| `ENV` | dev / stand | `dev` |
| `LOG_LEVEL` | уровень логов | `INFO` |
| `PUBLIC_BASE_URL` | внешний адрес стенда (ссылки в письмах) | `https://regs.example.org` |
| `LLM_BASE_URL`, `LLM_API_KEY` | внешний OpenAI-совместимый сервер | |
| `LLM_MODEL` | модель по умолчанию | |
| `LLM_AGENT_MODEL`, `LLM_GUARD_MODEL`, `LLM_EXTRACT_MODEL`, `LLM_KEYWORD_MODEL`, `LLM_JUDGE_MODEL` | переопределения по ролям (пусто = `LLM_MODEL`) | |
| `LLM_TIMEOUT_S`, `LLM_MAX_CONCURRENCY` | таймаут и параллелизм | `120`, `8` |
| `LLM_EXTRA_BODY_JSON` | доп. параметры запроса, зависящие от сервера (например отключение рассуждений) | `{"chat_template_kwargs":{"enable_thinking":false}}` |
| `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_MODEL`, `EMBEDDINGS_DIM` | эмбеддинги | `http://embeddings:80`, `BAAI/bge-m3`, `1024` |
| `TEI_IMAGE` | Образ TEI для архитектуры хоста (`cpu-arm64-1.10` на Apple Silicon) | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.10` |
| `RERANK_BASE_URL`, `RERANK_MODEL`, `RERANK_MIN_SCORE` | реранкер и порог | |
| `POSTGRES_HOST`, `POSTGRES_PORT` | Postgres | |
| `APP_DB`, `APP_DB_USER`, `APP_DB_PASSWORD` | БД backend | |
| `KNOWLEDGE_DB`, `KNOWLEDGE_DB_USER`, `KNOWLEDGE_DB_PASSWORD` | БД knowledge | |
| `KEYCLOAK_DB`, `KEYCLOAK_DB_USER`, `KEYCLOAK_DB_PASSWORD` | БД Keycloak | |
| `RABBITMQ_URL` | брокер | |
| `KEYCLOAK_ISSUER`, `KEYCLOAK_AUDIENCE`, `KEYCLOAK_ADMIN`, `KEYCLOAK_ADMIN_PASSWORD` | OIDC и админ Keycloak | |
| `KEYCLOAK_JWKS_URL` | Внутренний адрес публичных ключей Keycloak | `http://keycloak:8080/auth/realms/regagent/protocol/openid-connect/certs` |
| `WEB_DEV_BASE_URL` | Дополнительный origin для Vite в realm | `http://localhost:5173` |
| `KEYCLOAK_EMPLOYEE_PUBLIC_PASSWORD`, `KEYCLOAK_EMPLOYEE_FIN_PASSWORD`, `KEYCLOAK_QC_EXPERT_PASSWORD`, `KEYCLOAK_ADMIN_DEMO_PASSWORD` | Пароли именных учёток, импортируемые при создании demo realm | |
| `ACCESS_LEVELS_FILE` | YAML уровней доступа | `config/access_levels.yaml` |
| `KNOWLEDGE_URL`, `KNOWLEDGE_INTERNAL_TOKEN` | адрес и токен knowledge | |
| `DOCS_DIR`, `OKF_DIR` | папки документов и OKF | `/data/docs`, `/data/okf` |
| `WATCH_DEBOUNCE_S`, `INDEX_MAX_RETRIES` | индексация | `3`, `3` |
| `CHUNK_MAX_TOKENS` | максимум токенов во фрагменте | `1200` |
| `SEARCH_TOP_K`, `SEARCH_MODE` | поиск LightRAG | `8`, `mix` |
| `AGENT_ESCALATE_AFTER` | после скольких неудач предлагать ОКК | `2` |
| `AGENT_MAX_STEPS` | предел шагов Deep Agent | `12` |
| `MEMORY_AREA_TTL_DAYS`, `MEMORY_MAX_ACTIVE_AREAS` | память | `14`, `3` |
| `QUESTION_MAX_CHARS`, `RATE_LIMIT_PER_USER_PER_MIN` | лимиты | `2000`, `20` |
| `QC_CLUSTER_SIMILARITY`, `QC_NOTIFY_TO` | ОКК | `0.85`, `qc@example.org` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | почта | |
| `AUDIT_RETENTION_DAYS` | срок хранения журнала | `90` |
| `VITE_ASSISTANT_ID` | ID графа или ассистента в Aegra. Пустое значение подходит для стенда с одним ассистентом | |
| `VITE_API_URL`, `VITE_OIDC_AUTHORITY`, `VITE_OIDC_CLIENT_ID` | web (на этапе сборки) | |

Переменные самой Aegra (`AEGRA_CONFIG`, `AUTH_TYPE`, её `POSTGRES_*`) — по её документации, отображаются на наши.
