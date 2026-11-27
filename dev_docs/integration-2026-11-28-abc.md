# Интеграция потоков A, B, C — 28.11

Ветка `int/abc-2026-11-28` от `main` (`a5588fc`). Проверочная сборка: в `main` не влита.

## Порядок сборки

1. Поток C: события (C3), расход токенов (C4), итог хода (C2), память области (C1).
2. Merge потока A (`a/P2-A3-practice-publication`) — публикация практики ОКК в knowledge.
3. Merge потока B (`b/practice-qc`) — тикеты ОКК, аналитика, кабинет в web.
4. Поверх: правила практики в ответе (`c2cc2ed`), события ОКК в общем формате (`02de2af`), итог хода → аналитика B (`0308c6c`).

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

## Открыто: стык A ↔ B (не сведён, ждёт решения, кто сводит)

| # | Расхождение | A (knowledge, общий контракт) | B (backend) | Предложение |
|---|---|---|---|---|
| 1 | Поля команды публикации | `approved_by`, `approved_levels`, `extra="forbid"` | `admin_id`, `admin_levels` | B переходит на `regagent_common.PracticePublish` — команда B сейчас отвергается валидацией |
| 2 | Объявление очередей | `practice.publish`, `qc.events` со своими `arguments` (DLX) | те же имена, другие `arguments` (`qc_worker.py:68–75`) | один набор аргументов в common; иначе второй сервис получит `PRECONDITION_FAILED` при старте. Очередь `practice.published` у A никто не читает — убрать или подписать B |
| 3 | Сообщение о перепроверке | по одному на практику: `ticket_id, practice_id, revision, content_changed` | одно на документ: `doc_id, version, practice_ids` | взять форму A (`PracticeRecheck`), B группирует сам |
| 4 | Лимиты | до 16 привязок, текст без ограничения | до 20 привязок, текст ≤ 12000 | 16 привязок и 12000 символов в общем контракте |

Пока 1–3 не сведены, публикация практики из кабинета ОКК до knowledge не доходит; поиск и ответы с уже опубликованной практикой работают.

## Открыто: исход `error` в аналитике B

В `qc_analytics.Outcome` нет общей ошибки. Сейчас такой ход в аналитику не пишется (`QcTurnSink` отказывает, граф пишет событие `turn.report` `error`, ответ не меняется). Варианты: добавить `error` в `Outcome` B (колонка — text без CHECK, миграция не нужна) или писать как `model_timeout`. Ждёт решения.

## Проверка

| Что | Результат |
|---|---|
| `ruff check`, `ruff format --check` | чисто |
| `mypy` (common, knowledge, backend) | 50 файлов, без ошибок |
| `pytest` (без integration) | 472 passed |
| integration на Postgres | 61 passed на предыдущем шаге сборки |
| web: тесты, сборка | 27 passed, build ок |

**Не запускалось в моём окружении (нет RabbitMQ):** `backend/tests/test_qc.py::test_rabbitmq_command_and_result_roundtrip`, `knowledge/tests/test_consumer_pg_mq.py`. Именно они поймали бы расхождения 1–3 — запускать на стенде после сведения стыка:

```sh
docker compose -f deploy/compose.yaml up -d postgres rabbitmq
uv run pytest -m integration services/backend/tests/test_qc.py services/knowledge/tests/test_consumer_pg_mq.py
```
