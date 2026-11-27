# Интеграция потоков A, B, C — 28.11

Ветка `int/abc-2026-11-28` от `main` (`a5588fc`), влита в `main` по команде пользователя.

## Порядок сборки

1. Поток C: события (C3), расход токенов (C4), итог хода (C2), память области (C1).
2. Merge потока A (`a/P2-A3-practice-publication`) — публикация практики ОКК в knowledge.
3. Merge потока B (`b/practice-qc`) — тикеты ОКК, аналитика, кабинет в web.
4. Поверх: правила практики в ответе (`c2cc2ed`), события ОКК в общем формате (`02de2af`), итог хода → аналитика B (`0308c6c`), исход `error` (`f47fa38`), стык A ↔ B (`6d9cced`).

## Конфликты и как сведены

| Файл | Решение |
|---|---|
| `knowledge/consumer.py` | цикл попыток C (`_run_attempts`, контекст `CURRENT_JOB`) + хук A `after_index` после `_attempt` |
| `knowledge/api.py` | событие `search.request` (C) + `practice_lookup` (A) |
| `backend/routes.py`, `settings.py`, тесты | оставлены обе части: память (C) и ОКК (B) |
| `web/Chat.tsx` | кнопка «Память» (C) и `EscalateQuestion` (B) |
| docs 01–08 | описание графа — от C, раздел ОКК и страницы — от B, контракт RabbitMQ — от A с пометкой, что B шлёт другой формат |
| `09-decisions.md` | перенумерация: D-52 потока A → D-56; D-52…D-54 потока B → D-57…D-59, ссылки поправлены только в строках B |

## Что сделано на стыках потоком C

- **B → события.** `qc_event` писал через `logging` с `extra`, в stdout поля терялись. Теперь `EventLog` (D-52): одна JSON-строка с `run_id`, `error_code`, `duration_ms`. Тест `test_qc_events.py`.
- **C → аналитика B.** `TurnSink.record(report, identity)` — identity добавлен, потому что `record_turn` потока B требует уровни пользователя. `QcTurnSink` переводит исходы: answered→answer, unsupported→insufficient_evidence, ask_area→clarification, refused→refusal, остальные совпадают. Подключён в `_runtime_deps`. Тест `test_qc_turn_sink.py`.
- **A → ответ агента.** Практика с `overrides` обязана цитироваться вместе с пунктом, который она заменяет: одна переписка с подсказкой, иначе fallback `practice_not_cited` (D-60).

## Стык A ↔ B — сведён (D-61)

По поручению пользователя свёл поток C: B переведён на общий контракт A.

| # | Было | Стало |
|---|---|---|
| 1 | B слал `admin_id`, `admin_levels`; knowledge отвергал команду (`extra="forbid"`) | B шлёт `approved_by`, `approved_levels` |
| 2 | `practice.publish` и `qc.events` объявлялись с разными аргументами; очередь `practice.published` без потребителя | общие очереди — `regagent_common.broker.declare_shared`, оба сервиса вызывают её; `practice.published` — только ключ в `qc.events` |
| 3 | перепроверка: A — на практику, B ждал на документ (`practice_ids`, `version`) | B принимает форму A; заявка открывается по `ticket_id + practice_id + revision`, повтор отсекается по `message_id`; причина `content_changed` добавлена в B и web |
| 4 | 16 / 20 привязок, текст без лимита / 12000 | 16 привязок и 12000 символов — константы в common; привязки в B проверяются правилами knowledge (slug документа, номер пункта) |

Тесты: `tests/contract/test_broker_topology.py` (оба сервиса объявляют общие очереди одинаково), `tests/contract/test_practice_messages.py` (сообщения читаются в обе стороны), `packages/common/tests/test_broker.py`.

## Исход `error` в аналитике — решён

Добавлен в `qc_analytics.Outcome` и в список вопросов без ответа (как `search_unavailable`, `model_timeout`); в web — «Сбой». Миграция не нужна: колонка `outcome` — text.

## Проверка

| Что | Результат |
|---|---|
| `ruff check`, `ruff format --check` | чисто |
| `mypy` (common, knowledge, backend) | 51 файл, без ошибок |
| `pytest` (без integration) | 487 passed |
| integration на Postgres (граф NetworkX) | 61 passed; 4 упали только из-за отсутствия RabbitMQ |
| web: тесты, сборка | 27 passed, build ок |

**Не запускалось в моём окружении (нет RabbitMQ и AGE):** `backend/tests/test_qc.py::test_rabbitmq_command_and_result_roundtrip`, `knowledge/tests/test_practice.py::test_broker_delivers_publication_and_persistent_result`, `knowledge/tests/test_consumer_pg_mq.py` (2), `knowledge/tests/test_rag_pg.py` на AGE. Тесты брокера проверяют стык на настоящем RabbitMQ — запустить на стенде до выкатки:

```sh
docker compose -f deploy/compose.yaml up -d postgres rabbitmq
uv run pytest -m integration services/backend/tests/test_qc.py services/knowledge/tests/test_practice.py services/knowledge/tests/test_consumer_pg_mq.py
```

## Проверка на хосте после интеграции

В ветке a/fix-abc-checks устранены три проблемы, обнаруженные при проверке объединённого main.

- Исправлены типы тестовых UserIdentity и приёмника итогов хода. Полная команда mypy packages services проходит.
- Тест Knowledge подписывается на результаты публикации через свою временную очередь. Общая очередь backend может содержать результат предыдущего теста. Чужой ticket_id воспроизводился в общем прогоне, отдельный тест проходил. После исправления общий прогон на PostgreSQL с AGE и RabbitMQ проходит.
- Проверка релиза разбирает discovery как JSON только при HTTP 200. Закрытый master realm возвращает пустой 404. Поведение закреплено тестом test_probe_accepts_empty_404_for_master_discovery.

Результаты проверки ветки: 488 обычных тестов и 65 проверок integration/leak прошли. Ruff и полная проверка типов чистые.

По решению пользователя 28.11 вывод области по одной тематике вопроса отложен. В ближайшей версии остаются прямые указания области и накопление сигналов по D-55.
