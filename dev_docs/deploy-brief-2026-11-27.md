# Бриф выкатки 27.11: релиз `a/2026-11-27-sandbox` на стенд

**Для кого.** Человек с доступом к Mac-хосту стенда (`~/srv/regagent_prod`).
**Что выкатываем.** `main` после мержа ветки `a/2026-11-27-sandbox`: работа Никиты (K, Q, M, D-43), закрытая админка Keycloak, проверка входа при релизе, восстановление индексации после перезапуска, служебный API, `follow_refs`. Подробности — `dev_docs/integration-2026-11-27-sandbox.md`.
**Мерж в `main` и пуш делает человек**, этот бриф начинается после пуша.

Все команды — для zsh, вставлять блоками целиком. Секреты в чат и отчёты не копировать: в выводе команд ниже их нет.

## 0. Переменные и функция compose

В каждом новом окне терминала:

```sh
ROOT=$HOME/srv/regagent_prod
REPO=$HOME/путь/к/рабочей/копии/regs-agent   # своя копия репозитория, поправить путь
SHA=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["good"])' "$ROOT/state.json")
c() {
  (
    cd "$ROOT/releases/$SHA" || exit 1
    if [ -f tunnel.json ]; then
      docker compose -p regagent-prod --env-file "$ROOT/configs/$SHA.env" \
        -f deploy/compose.yaml -f deploy/prod/compose.yaml -f storage.json \
        -f tunnel.json -f images.json "$@"
    else
      docker compose -p regagent-prod --env-file "$ROOT/configs/$SHA.env" \
        -f deploy/compose.yaml -f deploy/prod/compose.yaml -f storage.json \
        -f images.json "$@"
    fi
  )
}
echo "$SHA"
```

После того как релиз принят (шаг 4), переменную `SHA` надо выставить заново той же строкой.

## 1. Обновить рабочую копию и скрипты выкатки

Работает копия `deploy.py` в `~/srv/regagent_prod/bin/`, а не файл из репозитория.
Новые проверки включаются только после `install.sh`. Сделать сразу после пуша `main`:

```sh
cd "$REPO" && git switch main && git pull --ff-only && uv sync --frozen
/bin/sh deploy/prod/install.sh "$ROOT" "$ROOT/.env"
```

Существующие `.env` и `cloudflare.env` не меняются. Если опрос успел выкатить новый `main`
старым `deploy.py` раньше этого шага — не страшно: новые проверки сработают со следующего релиза.

Если ещё не удалён старый ручной коннектор Cloudflare (с прошлого хотфикса):

```sh
docker ps -a --format '{{.Names}}' | grep -x regagent-prod-tunnel && docker rm -f regagent-prod-tunnel
```

## 2. Пароль `employee_public`

Новая проверка релиза входит под `employee_public` паролем из `.env`. Если пароль в
Keycloak другой, релиз не пройдёт.

1. В приватном окне браузера открыть https://regagent.honey-road.tech и войти под
   `employee_public` с паролем `KEYCLOAK_EMPLOYEE_PUBLIC_PASSWORD` из `~/srv/regagent_prod/.env`.
2. Вошёл — всё в порядке. Не вошёл — выставить в Keycloak пароль из `.env`:

```sh
P=$(/usr/bin/python3 -c 'import runpy,sys; e=runpy.run_path(sys.argv[1])["parse_env"](open(sys.argv[2]).read()); print(e["KEYCLOAK_EMPLOYEE_PUBLIC_PASSWORD"])' "$REPO/deploy/prod/deploy.py" "$ROOT/.env")
c exec -e P="$P" keycloak sh -c '
  /opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080/auth \
    --realm master --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null &&
  /opt/keycloak/bin/kcadm.sh set-password -r regagent --username employee_public --new-password "$P"'
unset P
```

## 3. Дождаться релиза

Опрос идёт раз в 2 минуты, проверка релиза — тесты, сборка, запуск, probe — занимает минуты.

```sh
tail -n 50 -f "$ROOT/logs/deploy.log"
```

| Что в логе | Что значит | Что делать |
| --- | --- | --- |
| `Принят <SHA>` | релиз прошёл | шаг 4 |
| `<ПЕРЕМЕННАЯ>: …` при подготовке | плохой `.env` | поправить `.env`, следующая попытка сама через 2 минуты |
| `Повторять не буду` и `вход под employee_public не прошёл: пароль в .env не совпадает с Keycloak?` | пароль | шаг 2, затем снять отказ (ниже) |
| `Проверка … (N/61): вход под employee_public: Keycloak ответил …` до 61-й попытки, затем `Откат` | вход через loopback не сработал с настоящим Keycloak | в `.env` добавить `RELEASE_SIGN_IN_PROBE=off`, снять отказ, прислать разработчику 40 строк лога вокруг ошибки |
| `Проверка … (N/61): /auth/admin/ → 200` (или `→ 302`), затем `Откат` | Caddy не закрыл админку | снять отказ нельзя — прислать лог разработчику |
| `после входа … → 401` | фронт или backend снова не принимают токен | прислать лог и `c logs --tail 50 backend` |
| `после входа ассистентов N, нужен ровно один` | ассистент не зарегистрирован или их несколько | прислать лог и `c logs --tail 50 backend` |
| `Проверка … (N/61): корпус ещё не проиндексирован` | индексация идёт или упала | ждать; если дошло до `Откат` — сводка шага 5, прислать её |

При отказе стенд остаётся на прошлом рабочем SHA, это нормально. Отклонённый SHA
повторно не проверяется. Чтобы проверить его ещё раз после правки `.env`:

```sh
/usr/bin/python3 - "$ROOT/state.json" <<'PY'
import json, sys
path = sys.argv[1]
state = json.load(open(path))
state.pop("rejected", None)
json.dump(state, open(path, "w"))
PY
```

## 4. После «Принят»: снаружи

```sh
SHA=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["good"])' "$ROOT/state.json")
B=https://regagent.honey-road.tech
curl -s -o /dev/null -w '%{http_code} /\n' "$B/"
curl -s -o /dev/null -w '%{http_code} /api/health\n' "$B/api/health"
curl -s -o /dev/null -w '%{http_code} /auth/realms/regagent\n' "$B/auth/realms/regagent/.well-known/openid-configuration"
curl -s -o /dev/null -w '%{http_code} /auth/admin/\n' "$B/auth/admin/"
curl -s -o /dev/null -w '%{http_code} /auth/realms/master\n' "$B/auth/realms/master/.well-known/openid-configuration"
```

Ожидается: `200`, `200`, `200`, затем `404` и `404`. Вместо 404 может быть 302 на `*.cloudflareaccess.com`,
если делали шаг 3 гайда `deploy/prod/cloudflare-bypass.md`. Любое другое — прислать вывод.

В браузере: войти под `employee_public`, открыть чат, задать вопрос, получить ответ с источниками.

## 5. Сводка по базе (пригодится в шагах 6–8)

```sh
c exec -T postgres sh -c 'psql -X -U "$POSTGRES_USER" -d "$KNOWLEDGE_DB"' <<'SQL'
SELECT status, count(*) FROM documents GROUP BY 1 ORDER BY 1;
SELECT doc_id, status, attempts, current_step, steps, left(error, 120) AS error
FROM jobs WHERE status <> 'succeeded' ORDER BY updated_at DESC LIMIT 20;
SQL
```

## 6. Переиндексация корпуса (язык извлечения теперь русский)

Смена языка сама задач не ставит, поэтому запустить вручную один раз:

```sh
c exec knowledge python -m regagent_knowledge.reindex --all
```

Ждать, пока в сводке шага 5 все документы станут `indexed`, а задач не в `succeeded`
не останется. Текущий шаг висящей задачи — в колонке `current_step`. Ход виден и в логе:

```sh
c logs --since 10m knowledge | grep -E 'step=|прервана|недоделанные'
```

Проверка, что сущности теперь на русском:

```sh
c exec -T postgres sh -c 'psql -X -U "$POSTGRES_USER" -d "$KNOWLEDGE_DB"' <<'SQL'
SELECT workspace, entity_name FROM lightrag_vdb_entity ORDER BY random() LIMIT 15;
SQL
```

Ожидается: имена сущностей по-русски («Руководитель проекта», а не «Project Manager»).

## 7. Восстановление после перезапуска (проверка D-50, ~10 минут)

1. Снова запустить `c exec knowledge python -m regagent_knowledge.reindex --all`.
2. Дождаться в сводке шага 5 задачи с `current_step = index` и перезапустить knowledge:
   `c restart knowledge`.
3. Через минуту:
   ```sh
   c logs --since 5m knowledge | grep -E 'недоделанные вставки|прервана индексация'
   ```
   Ожидается: строки про удалённые вставки и прерванный документ.
4. Сводка шага 5: документ ушёл в `failed`, затем после повторной доставки — в `indexed`.
   Если остался `failed`: `c exec knowledge python -m regagent_knowledge.reindex --failed`,
   и через пару минут он `indexed`.

## 8. Выгрузка для K-4 (маркер `<|COMPLETE|>`)

После шага 6 сохранить последние ответы модели при извлечении:

```sh
c exec -T postgres sh -c 'psql -X -U "$POSTGRES_USER" -d "$KNOWLEDGE_DB" --csv' <<'SQL' > "$HOME/regagent-extract-cache.csv"
SELECT workspace, id, create_time,
       return_value LIKE '%<|COMPLETE|>%' AS has_complete,
       length(return_value) AS answer_len,
       left(return_value, 6000) AS answer
FROM lightrag_llm_cache WHERE cache_type = 'extract'
ORDER BY create_time DESC LIMIT 30;
SQL
wc -l "$HOME/regagent-extract-cache.csv"
```

Файл передать разработчику knowledge **лично**, не в общий канал: в ответах модели текст демо-корпуса.

## 9. Живые проверки новых функций

**Служебный API (D-51).** Токен демо-учётки `admin` и запросы:

```sh
cd "$REPO"
TOKEN=$(uv run --no-sync python - "$ROOT/.env" <<'PY'
import sys
from dotenv import dotenv_values
from tests.e2e.test_mvp_scenarios import sign_in
env = dotenv_values(sys.argv[1])
base = env["PUBLIC_BASE_URL"]
audience = env.get("KEYCLOAK_AUDIENCE") or "regagent-web"
print(sign_in(base + "/auth/realms/regagent", audience, base, "admin", env["KEYCLOAK_ADMIN_DEMO_PASSWORD"]))
PY
)
B=https://regagent.honey-road.tech
curl -s -H "Authorization: Bearer $TOKEN" "$B/api/admin/documents" | python3 -m json.tool | head -30
curl -s -H "Authorization: Bearer $TOKEN" "$B/api/admin/jobs?status=failed" | python3 -m json.tool
curl -s -H "Authorization: Bearer $TOKEN" "$B/api/admin/review" | python3 -m json.tool | head -40
unset TOKEN
```

Ожидается: список документов всех уровней, задачи (может быть пусто), ссылки на сверку.
Под `employee_public` те же адреса должны отвечать 403.

**`follow_refs` (D-48).** Найти пункт со ссылкой на другой документ:

```sh
grep -h -A4 '^links:' "$ROOT"/data/okf/docs/*/v*.md | grep -E 'doc_id|clause|via' | head -12
```

Взять пару «документ + пункт», спросить в чате о содержании этого пункта так, чтобы ответ
требовал связанного регламента (например: «Что делать по пункту X, если нужен субподрядчик?»).
Ожидается: в источниках ответа есть пункт связанного документа. Если `links` везде пустые —
записать это, значит ссылок по названию в корпусе нет.

**Сравнение (D-43).** Под `employee_public` в новом диалоге:
«Сравните общую область и ДИБ. Кто по регламенту подбора субподрядчика формирует запрос, если
внутренних ресурсов не хватает?» Ожидается: РП для общего порядка, пресейл-архитектор для ДИБ,
**без** фразы, что запрос от РП не требуется. Допустим и отказ в стиле fallback. Снять скриншот ответа.

## 10. Прогон качества (если есть время)

После Bypass скрипт проходит к стенду снаружи. Он очищает память `employee_public` и
`employee_fin` и создаёт диалоги, поэтому не запускать одновременно с ручным прогоном демо.

```sh
cd "$REPO"
uv run --no-sync python -m scripts.eval.run --validate-only
uv run --no-sync python -m scripts.eval.run --env-file "$ROOT/.env" \
  --stand https://regagent.honey-road.tech --reports-dir "$HOME/regagent-eval"
```

Код выхода `3` — утечка источника: прогон остановится сам, сообщить сразу.

## 11. Что прислать обратно

- SHA принятого релиза и 20 последних строк `deploy.log`, либо строки с ошибкой;
- вывод шага 4;
- сводку шага 5 после переиндексации и вывод проверки языка (шаг 6);
- строки из лога шага 7 и итоговый статус документа;
- файл шага 8 — лично разработчику knowledge;
- результаты шага 9: коды ответов служебного API, вопрос и источники для `follow_refs`, скриншот сравнения;
- если запускали шаг 10 — `$HOME/regagent-eval/*.md`.

Не присылать: `.env`, токены, пароли, вывод `env`.
