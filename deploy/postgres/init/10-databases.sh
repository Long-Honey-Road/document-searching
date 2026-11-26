#!/bin/bash
# Отдельная БД и отдельный пользователь на сервис. LightRAG выполняет DDL при старте,
# поэтому knowledge — владелец только своей БД.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-SQL
  CREATE USER ${APP_DB_USER} WITH PASSWORD '${APP_DB_PASSWORD}';
  CREATE DATABASE ${APP_DB} OWNER ${APP_DB_USER};
  CREATE USER ${KNOWLEDGE_DB_USER} WITH PASSWORD '${KNOWLEDGE_DB_PASSWORD}';
  CREATE DATABASE ${KNOWLEDGE_DB} OWNER ${KNOWLEDGE_DB_USER};
  CREATE USER ${KEYCLOAK_DB_USER} WITH PASSWORD '${KEYCLOAK_DB_PASSWORD}';
  CREATE DATABASE ${KEYCLOAK_DB} OWNER ${KEYCLOAK_DB_USER};
SQL
for db in "${APP_DB}" "${KNOWLEDGE_DB}"; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -d "$db" -c "CREATE EXTENSION IF NOT EXISTS vector;"
done
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -d "${KNOWLEDGE_DB}" -c "CREATE EXTENSION IF NOT EXISTS age;"
# LightRAG использует типы и функции AGE от имени владельца knowledge.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -d "$KNOWLEDGE_DB" \
  -v "knowledge_role=$KNOWLEDGE_DB_USER" <<'SQL'
GRANT USAGE ON SCHEMA ag_catalog TO :"knowledge_role";
SQL
