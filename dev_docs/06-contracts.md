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

Для фрагмента практики `Fragment.practice` содержит `mode` и `binds: [{doc_id, version, clause}]`. У фрагмента регламента поле отсутствует.

## RabbitMQ

Exchange `regagent` (topic). Все сообщения — JSON с полями `message_id`, `created_at`, `type`.

| Ключ / очередь | Кто пишет | Кто читает | Тело |
| --- | --- | --- | --- |
| `index.jobs` | knowledge.watcher, knowledge.reindex | knowledge.consumer | `{job_id, op: "upsert"\|"delete", doc_id, version, path, sha256}`. У `delete` `path` и `sha256` необязательны (D-26). `force` (по умолчанию `false`) — поле `IndexJob` в `regagent_common`: переиндексация ставит `true` у `upsert` (D-45), обработчик тогда не пропускает уже проиндексированный документ. |
| `practice.publish` | backend (ОКК) | knowledge.consumer | `PracticePublish`: `ticket_id, practice_id, revision, text, binds: [{doc_id, version, clause}], mode, approved_by, approved_levels` |
| ключ `practice.published` → очередь `qc.events` | knowledge | backend | `PracticePublicationResult`: `ticket_id, practice_id, revision, status, error_code`. Успех — `published`, отказ — `failed` с кодом. |
| `qc.events` | knowledge | backend | `PracticeRecheck`: `kind=recheck, ticket_id, practice_id, revision, doc_id, reason`. Причины: `new_version`, `deleted`, `access_changed`, `content_changed`. Повтор доставки сохраняет тот же `message_id`. |

Очереди, общие для knowledge и backend, объявляются из `regagent_common.broker` (D-61); модели backend (`qc_models`) повторяют этот контракт с типами UUID, совместимость проверяют `tests/contract`.

Очередь ошибок `index.jobs` и `practice.publish` — `regagent.dlq`. К очереди `qc.events` привязаны ключи `practice.published` и `qc.events`, у неё один активный потребитель и очередь ошибок `qc.events.dlq`. Сообщение несёт `message_id`, UTC `created_at` и `type`, равный ключу маршрутизации; у перепроверки `message_id` детерминирован (`recheck:<practice_id>:<revision>`). `ticket_id` и `practice_id` от backend — UUID. Лимиты команды: до 16 привязок, текст до 12000 символов. `revision` и редакция документа — целые числа от 1. Режим `clarifies` или `overrides`. Backend повторяет отправку того же сообщения до подтверждения брокера. Knowledge публикует результат до событий перепроверки той же новой практики и обеспечивает идемпотентность по `(practice_id, revision)`, подтверждает публикацию после индексации и проверяет все привязки и права. Повторы результатов и событий перепроверки безопасны на стороне backend. Схемой общего протокола и потребителем команды владеет поток A.

## Ответ агента

Итоговое сообщение агента — markdown со ссылками `[f:<fragment_id>]`. В метаданных сообщения:

```json
{"verified": true, "area_used": "ДИБ", "area_source": "memory|question|asked|none",
 "sources": [{"n": 1, "fragment_id": "...", "title": "...", "clause": "2.7", "version": 3, "kind": "regulation"}],
 "outcome": "answer|refusal|not_found|ask_area|ask_clarification|qc_help|fallback"}
```

web заменяет `[f:...]` на номера `[1]` по `sources`.

Где лежит (D-19): итоговое сообщение — AIMessage из узла `finalize`, `AnswerMeta` — в `additional_kwargs.answer_meta`. `assistant_id` — `regagent`. Публичный стрим доставляет проверенный итог. Вызовы инструментов, результаты поиска и черновик модели остаются во внутреннем запуске (D-35).

## Итог хода для аналитики ОКК (стык B + C)

`regagent_common.contracts.TurnReport` — один на ход с `run_id`. Граф передаёт его вместе с `UserIdentity` в `TurnSink.record(report, identity)` после того, как ответ готов (D-54). Рабочий приёмник — `QcTurnSink`: переводит `TurnReport` в `qc_analytics.TurnOutcome` потока B (вопрос обрезается до 4000 символов, фрагменты — до 500) и вызывает `record_turn`. Соответствие исходов: answered→answer, empty_search, unsupported→insufficient_evidence, ask_area и ask_clarification→clarification, refused→refusal, search_unavailable, model_timeout; для `error` в аналитике B нет значения — такой ход виден только в событиях.

| Поле | Что |
| --- | --- |
| `run_id`, `thread_id`, `user_sub`, `at` | ход, диалог, пользователь, время UTC |
| `question` | текст последнего вопроса. Только в хранилище аналитики с учётом прав и срока хранения, в журнал событий не пишется |
| `area`, `area_source` | область ответа и откуда она (`AnswerMeta`) |
| `outcome` | `answered`, `empty_search`, `unsupported`, `ask_area`, `ask_clarification`, `refused`, `search_unavailable`, `model_timeout`, `error` |
| `reason` | код причины, как в `turn.result` |
| `fragments`, `cited` | id фрагментов, полученных в запуске, и процитированных в ответе |
| `practice_used` | в ответе процитирована запись практики |
| `searches` | число поисков |

`unsupported` — материалы нашлись, но ответ не прошёл проверку ссылок или сравнения, выдача слабая или найденные пункты не подтверждают ответ (`no_regulation_support`). `search_unavailable` — хотя бы один поиск не дошёл до knowledge и фрагментов нет. Исход «ограничение доступа» не выделяется: поиск не сообщает, что отфильтровал по правам, и пустая выдача не должна говорить о закрытых документах. Команды памяти («запомни / забудь»), инструкции и черновики ОКК (`qc_help`) исключаются из аналитики вопросов. Повтор одного `run_id` (повторная доставка) приходит с тем же `run_id`: хранилище обязано сводить его к одной записи.

## Память

Store namespace `("users", sub, "memory")`, ключ `area:ДИБ`, значение `{kind, value, source, confirmed_at, times_confirmed}`. Ключ `signals:area` — `{"history": [{run_id, area, at}]}`, последние 20 прямых указаний области (D-55).

## backend HTTP (свои маршруты)

| Метод | Путь | Роль | Что делает |
| --- | --- | --- | --- |
| GET | `/me/memory` | любой | `{"records": [...], "signals": [{area, at}], "progress": {current, current_confirmed_at, days_until_stale, pending_area, pending_count, pending_needed}}`: свои записи с полем `key`, указания области без `run_id` и счёт до записи или смены (D-55) |
| DELETE | `/me/memory/{key}` | любой | удалить свою запись (`area:<код>`, вместе с её сигналами) или все сигналы (`signals:area`), `204` также при отсутствии |
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

## События сервисов (журнал)

Каждое событие — одна JSON-строка в stdout контейнера, пишет `regagent_common.events.EventLog(<service>)` (D-52). Обычные логи сервиса (LightRAG, uvicorn, Aegra) идут отдельно, события в них не дублируются.

| Поле | Что | Пример |
| --- | --- | --- |
| `ts` | время UTC, миллисекунды | `2026-11-28T01:15:42.297Z` |
| `service` | `backend`, `knowledge`, `deploy` | `backend` |
| `event` | `<область>.<действие>`, имя из списка ниже | `guard.classify` |
| `status` | исход, низкокардинальный: `ok`, `error`, `empty`, `rejected`, `timeout` или свой для события | `ok` |
| `duration_ms` | длительность, целое | `412` |
| `error_code` | код причины: имя исключения или свой код | `APITimeoutError` |
| связь | `run_id`, `thread_id`, `request_id`, `job_id`, `doc_id`, `ticket_id`, `practice_id` | `run_id: "…"` |
| прочее | короткие коды и числа: `area`, `outcome`, `model`, `operation`, `fragments`, `input_tokens`… | |

Метки для агрегации (Loki, Grafana) — только `service`, `event`, `status`; идентификаторы остаются полями.

Ограничения, которые соблюдает код:
- поля с именами про тексты и секреты (`text`, `query`, `question`, `prompt`, `answer`, `message`, `content`, `comment`, `password`, `secret`, `token`, `api_key`, `authorization`, `cookie`) отбрасываются. Счётчики `input_tokens`, `output_tokens`, `total_tokens`, `cached_tokens` разрешены;
- отбрасываются строки длиннее 128 символов или с переводом строки, а также значения не из «строка, число, флаг, `null`, список строк»;
- список длиннее 50 элементов обрезается, полная длина — в `<поле>_total`;
- имена отброшенных полей пишутся в `dropped_fields`;
- запись события никогда не бросает исключение.

Каталог событий (новые — по тем же правилам, имя `<область>.<действие>`):

| Событие | Сервис | `status` | Поля |
| --- | --- | --- | --- |
| `turn.result` | backend | `ok`, `error` | `outcome`, `reason`, `area`, `area_source`, `searches`, `search_unavailable`, `fragments`, `sources`, `user`, `run_id`, `thread_id` |
| `guard.classify` | backend | `ok`, `error` | `run_id`, `thread_id` |
| `answer.generate` | backend | `ok`, `error` | `attempt`, `run_id` |
| `answer.evidence_check` | backend | `ok`, `rejected`, `error` | `run_id` |
| `answer.comparison_check` | backend | `ok`, `rejected`, `error` | `run_id` |
| `search.tool` | backend | `ok`, `empty`, `unavailable`, `error` | `areas`, `fragments`, `low_confidence`, `areas_diverge`, `run_id` |
| `refs.tool` | backend | `ok`, `empty`, `not_found`, `unavailable`, `error` | `doc_id`, `outgoing`, `incoming`, `run_id` |
| `search.request` | knowledge | `ok`, `empty`, `error` | `levels` (число), `areas`, `fragments`, `low_confidence`, `areas_diverge` |
| `clause.request`, `refs.request` | knowledge | `ok`, `empty` (refs), `not_found`, `rejected`, `error` | `doc_id`, `levels`, `http_status` |
| `index.step` | knowledge | `ok`, `error` | `step`, `job_id`, `doc_id`, `version`, `attempt` |
| `index.job` | knowledge | `succeeded`, `retrying`, `failed` | `job_id`, `doc_id`, `version`, `op`, `attempt`, `force` |
| `memory.area` | backend | `pending`, `remembered`, `confirmed`, `switched`, `duplicate` | `area`, `count`, `needed`, `area_source`, `run_id` |
| `memory.expired` | backend | `ok` | `areas` — удалённые устаревшие области, `run_id` |
| `turn.report` | backend | `ok`, `error` | `outcome`, `run_id` — запись итога хода в аналитику ОКК (D-54) |
| `llm.usage` | backend, knowledge | `ok`, `timeout`, `error` | `operation` (`guard`, `comparison_check`, `evidence_check`, `answer` в backend; `index`, `keywords`, `embedding` в knowledge), `model`, `input_tokens`, `output_tokens`, `total_tokens`, `cached_tokens`, `usage_source` (`provider` / `unknown`), `items` (эмбеддинги), `run_id` или `job_id` |
| `deploy.decision` | deploy | `accepted`, `rejected` | `candidate`, `previous` (SHA) |
| `deploy.rollback` | deploy | `ok`, `stopped`, `error` | `candidate`, `previous` |

`llm.usage` пишется на каждый физический вызов модели или эмбеддингов (D-53). Токены — только из ответа провайдера; если он их не прислал или вызов упал, `usage_source=unknown` и чисел нет. Ответ из кэша LightRAG модель не вызывает и события не даёт.

`reason` в `turn.result` — код исхода: `practice_not_cited`, `off_topic`, `injection`, `partial_topic`, `too_long`, `no_question`, `rate_limit`, `guard_error`, `memory_stale`, `memory_area_unclear`, `memory_remember`, `memory_forget`, `memory_unavailable`, `areas_diverge`, `missing_detail`, `empty_search`, `no_regulation_support`, `search_unavailable`, `qc_draft`, `qc_guide`, `generation_error`, `citations_rejected`, `comparison_rejected`, `comparison_check_error`, `evidence_check_error`; у проверенного ответа — нет. `not_found` у `/clause` и `/refs` означает «нет документа или нет прав» — как и в ответе, без различия. События backend и knowledge пока связаны только по времени: `run_id` в knowledge не передаётся. События выкатки пишутся в `~/srv/regagent_prod/logs/events.jsonl` хоста, остальные — в stdout контейнеров.

## Переменные `.env`

Адреса и модели в `CommonSettings` (`LLM_BASE_URL`, `LLM_MODEL`, `EMBEDDINGS_*`, `RERANK_BASE_URL`, `RERANK_MODEL`) обязательны и без значений по умолчанию: пустая переменная считается отсутствующей, сервис падает при старте (D-27). Для `ENV=stand` релиз дополнительно проверяет публичные адреса, `VITE_*` и заглушки (D-38).

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
| `MEMORY_AREA_SIGNALS_TO_REMEMBER`, `MEMORY_AREA_SIGNALS_TO_SWITCH`, `MEMORY_AREA_SIGNAL_WINDOW_DAYS` | прямых указаний области до записи в пустую память, подряд до смены области, окно учёта в днях (D-55) | `2`, `3`, `14` |
| `QUESTION_MAX_CHARS`, `RATE_LIMIT_PER_USER_PER_MIN` | лимиты | `2000`, `20` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | почта | |
| `AUDIT_RETENTION_DAYS` | срок хранения аналитики ОКК, в днях | `90` |
| `VITE_ASSISTANT_ID` | ID графа или ассистента в Aegra. Пустое значение подходит для стенда с одним ассистентом | |
| `VITE_API_URL`, `VITE_OIDC_AUTHORITY`, `VITE_OIDC_CLIENT_ID` | web (на этапе сборки) | |

Переменные самой Aegra задаются в compose и отображаются на наши: `PORT=8000` (по умолчанию в Aegra 0.12 — 2026), `AEGRA_CONFIG=<путь к services/backend/aegra.json в образе>`, `AUTH_TYPE=custom`, `DATABASE_URL=postgresql://${APP_DB_USER}:${APP_DB_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${APP_DB}` (D-17, D-19).
