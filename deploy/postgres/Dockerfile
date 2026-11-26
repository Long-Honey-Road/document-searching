# Postgres с pgvector и Apache AGE (графовое хранилище LightRAG).
# Перед сборкой сверить, что ветка AGE поддерживает выбранную major-версию Postgres.
ARG PG_MAJOR=17
FROM pgvector/pgvector:pg${PG_MAJOR}
ARG PG_MAJOR
ARG AGE_REF=PG${PG_MAJOR}
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git flex bison postgresql-server-dev-${PG_MAJOR} ca-certificates \
 && git clone --depth 1 --branch ${AGE_REF} https://github.com/apache/age.git /tmp/age \
 && make -C /tmp/age && make -C /tmp/age install \
 && rm -rf /tmp/age \
 && apt-get purge -y build-essential git flex bison postgresql-server-dev-${PG_MAJOR} \
 && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY init/ /docker-entrypoint-initdb.d/
