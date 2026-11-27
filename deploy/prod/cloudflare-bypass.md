# Cloudflare: открыть стенд для демо (Bypass) и оставить закрытой админку Keycloak

**Зачем.** Сейчас весь `regagent.honey-road.tech` закрыт Cloudflare Access: до формы
Keycloak пускают только людей из команды. Заказчику на демо и скрипту прогона качества
(`scripts/eval`) Access мешает: скрипт не умеет проходить форму Access, а у заказчика
нет учётки в нашей организации. После этой настройки:

| Путь | Кто пускает |
| --- | --- |
| `/`, `/api/*`, `/auth/realms/regagent/*` и всё прочее | никого не спрашивает Cloudflare, вход — Keycloak (realm `regagent`) |
| `/auth/admin/*`, `/auth/realms/master/*` | Cloudflare Access, только команда; прокси стенда всё равно отвечает 404 (D-47) |

Названия пунктов меню — по документации Cloudflare на ноябрь 2026. Интерфейс у них
меняется часто: если пункт называется иначе, ищите по смыслу, порядок шагов тот же.

## 0. Что знать заранее

- **Bypass выключает Access целиком:** нет проверки и нет журнала Access по этим запросам.
  Защищают стенд только Keycloak (саморегистрация выключена, защита от подбора пароля
  включена в `deploy/keycloak/regagent-realm.json`), лимит вопросов на учётку в backend и
  заголовки `noindex` в Caddy. Пароли демо-учёток должны быть не `replace-me` — релиз это
  уже проверяет.
- **Порядок политик.** Access сначала проверяет Bypass и Service Auth, потом Block и Allow.
  Поэтому Bypass на приложении сработает раньше старой Allow-политики, удалять её не обязательно.
- **Более точный путь главнее.** Приложение на `regagent.honey-road.tech/auth/admin`
  перекрывает приложение на весь hostname для этого пути. На этом построен шаг 3.

## 1. Проверить, что туннель не требует токен Access

Если у публичного hostname туннеля включено **Protect with Access**, `cloudflared` сам
проверяет JWT Access на каждом запросе. После Bypass JWT не выдаётся, и стенд начнёт
отвечать 403.

1. Cloudflare dashboard → **Networking** → **Tunnels** (в Zero Trust тот же список —
   **Networks** → **Connectors**) → туннель RegAgent → **Edit** → вкладка
   **Published application routes**.
2. Открыть маршрут `regagent.honey-road.tech` → раскрыть **Additional application
   settings** → раздел **Access**.
3. **Protect with Access** — выключить. Сохранить.

Туннель у нас запускается по токену (`TUNNEL_TOKEN` в `cloudflare.env`), конфиг берётся из
дашборда, перезапуск контейнера не нужен.

## 2. Bypass на весь стенд

1. **Zero Trust** → **Access controls** → **Policies** → **Add a policy**.
   - Policy name: `regagent-demo-bypass`
   - Action: **Bypass**
   - Include: selector **Everyone**
   - Сохранить.
2. **Zero Trust** → **Access controls** → **Applications** → приложение на
   `regagent.honey-road.tech` → **Configure** (или **Edit**) → **Policies**.
3. Добавить `regagent-demo-bypass`. Старую Allow-политику команды можно оставить:
   Bypass всё равно проверяется первым.
4. Сохранить.

**Альтернатива — удалить приложение Access на этот hostname целиком.** Результат для
стенда тот же, но шаг 3 обязателен *до* удаления, иначе админка Keycloak на минуту
окажется открытой. Bypass удобнее: вернуть закрытый стенд — значит снять одну политику.

## 3. Закрыть админку Keycloak отдельным приложением

С релиза, где появилось D-47, прокси стенда сам отвечает 404 на эти пути, а релиз это
проверяет. Шаг 3 — вторая линия: на старых релизах и при ошибке в Caddyfile консоль
администратора Keycloak (логин master realm) была бы открыта всему интернету.

1. **Access controls** → **Applications** → **Create new application** →
   **Self-hosted and private** → **Add public hostname**.
2. Name: `regagent-keycloak-admin`.
3. Hostname: subdomain `regagent`, domain `honey-road.tech`, path `auth/admin`.
4. Ещё раз **Add public hostname** с тем же доменом и path `auth/realms/master`.
5. **Access policies** → та же Allow-политика команды, что стояла на стенде.
6. **Create**.

Пути `/auth/realms/regagent/*` (вход пользователей, токены, JWKS для backend) сюда не
попадают — они остаются под Bypass.

## 4. Проверка (из чистой сессии, не из сети команды)

```sh
B=https://regagent.honey-road.tech
# 200 и без редиректа на *.cloudflareaccess.com
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$B/"
curl -s -o /dev/null -w '%{http_code}\n' "$B/api/health"
curl -s -o /dev/null -w '%{http_code}\n' "$B/auth/realms/regagent/.well-known/openid-configuration"
# админка закрыта: 302 на *.cloudflareaccess.com (шаг 3) или 404 от прокси (D-47).
# 302 на /auth/admin/master/console/ или 200 — открыта
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$B/auth/admin/"
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "$B/auth/realms/master/.well-known/openid-configuration"
```

Затем в приватном окне браузера: открыть стенд → форма Keycloak (не Cloudflare) → вход
под `employee_public` → чат открылся, список диалогов загрузился.

Если стенд отвечает 403 без формы — вернуться к шагу 1. Если всё ещё форма Cloudflare —
политика Bypass не привязана к приложению, которое покрывает hostname (проверить, нет ли
второго приложения с wildcard `*.honey-road.tech`).

## 5. Вернуть как было

Убрать `regagent-demo-bypass` из приложения стенда (или удалить политику). Приложение
админки Keycloak можно оставить — оно ничему не мешает. Если выключали **Protect with
Access** у туннеля, включить обратно.

## Источники

- [Access policies: действия, порядок проверки, Bypass](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/)
- [Управление политиками](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/policy-management/)
- [Пути приложений: более точный путь главнее](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/)
- [Публичное self-hosted приложение](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)
- [Параметры туннеля: Protect with Access](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/)
