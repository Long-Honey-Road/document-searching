# Keycloak

Compose импортирует `regagent-realm.json` при первом запуске. Сервер доступен через Caddy
на `/auth`, а адрес issuer задаётся публичным URL стенда. Клиент `regagent-web`
использует Authorization Code с PKCE S256. Realm импортирует четыре именные учётки
с паролями из `.env`. У `employee_fin` есть группа `/levels/closed_fin`,
у `qc_expert` и `admin` свои роли. Саморегистрация выключена.

До первого запуска задайте разные пароли в `.env`. Последующие старты сохраняют realm
в Postgres, поэтому пароли существующих учёток меняются в Keycloak.

Для локального стенда URL приложения должен совпадать с `PUBLIC_BASE_URL`, без завершающего слеша.
Vite использует дополнительный origin `WEB_DEV_BASE_URL`.
