# Локальный стенд

Caddy публикует приложение на http://localhost:8080. Postgres, RabbitMQ, Keycloak,
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
