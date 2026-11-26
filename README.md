# regagent — агент по регламентам

Агент отвечает на вопросы сотрудников по корпоративным регламентам: со ссылками на пункты, с учётом области бизнеса и практики отдела контроля качества (ОКК), с разграничением доступа.

Вся проектная документация — в [dev_docs/](dev_docs/README.md). Начинать с `dev_docs/01-concept.md`, план работ — `dev_docs/10-work-plan.md`.

## Структура

```
packages/common/      общие контракты и утилиты (regagent-common)
services/knowledge/   индексация, LightRAG, поиск, OKF (regagent-knowledge)
services/backend/     Aegra: агент, память, ОКК, служебные маршруты (regagent-backend)
web/                  React-интерфейс
deploy/               docker compose, образ Postgres (pgvector + AGE), Keycloak, Caddy
config/               уровни доступа
data/                 документы и OKF стенда (не в git)
eval/                 набор вопросов и отчёты оценки
scripts/              вспомогательные скрипты (синтетический корпус и т.п.)
dev_docs/             документация и планы
```

## Быстрый старт

```bash
cp .env.example .env            # заполнить адреса модели, пароли
uv sync --all-packages          # первый запуск создаёт uv.lock с актуальными версиями
uv run pytest                   # unit-тесты
docker compose -f deploy/compose.yaml --env-file .env up -d
```

Все настраиваемые параметры — в `.env` (список: `dev_docs/06-contracts.md#переменные-env`). Новые зависимости — `uv add <пакет>`.

## Правила

- Ветка на задачу, PR в `main`, мерж делает человек.
- Сначала проектирование, затем тесты, затем код (`dev_docs/08-testing-quality.md`).
- Решения, меняющие архитектуру или контракты, — в `dev_docs/09-decisions.md`.
