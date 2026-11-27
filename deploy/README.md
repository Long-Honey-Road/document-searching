# Локальный стенд

Caddy публикует приложение на http://localhost:8080. При открытии через
http://127.0.0.1:8080 браузер перейдёт на адрес из `PUBLIC_BASE_URL` до входа в Keycloak. Postgres, RabbitMQ, Keycloak,
Knowledge и Backend работают во внутренней сети Compose. Внешний issuer Keycloak
указывает на Caddy, а Backend получает ключи напрямую от Keycloak внутри сети.

Скопируйте `.env.example` в `.env`, задайте пароли и конфигурацию LLM.
На Apple Silicon установите `TEI_IMAGE` с тегом `cpu-arm64-1.10`.
Профиль `local-reranker` запускает только TEI-реранкер, а эмбеддинги идут в запущенную
на хосте Ollama через `host.docker.internal:11434`.

```bash
uv sync --all-packages --frozen
uv run python scripts/synth/generate.py
docker compose --env-file .env -f deploy/compose.yaml --profile local-reranker up -d --build
```

Если используете TEI и для эмбеддингов, выберите профиль `local-embeddings`,
укажите `EMBEDDINGS_BASE_URL=http://embeddings:80` и имя своей модели Hugging Face
в `EMBEDDINGS_MODEL`.

## Сквозные проверки

На тестовом стенде с чистой памятью демо-учёток и первой редакцией корпуса выполните
`uv run pytest -m e2e`. Проверки используют браузерный поток OIDC с PKCE и LangGraph SDK.
Тест смены редакции обновляет синтетический регламент до второй редакции в активном корпусе.
На другом checkout укажите расположение исходных образцов через `REGAGENT_E2E_SAMPLES_DIR`.
Путь к файлу с локальными настройками задаётся `REGAGENT_E2E_ENV_FILE`,
путь к монтируемому корпусу — `REGAGENT_E2E_DOCS_DIR`.
