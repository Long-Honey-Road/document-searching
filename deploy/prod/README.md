# Выкладка RegAgent на Mac

GitHub Actions проверяет Python и веб на PR и push в `main`. Пользовательский
LaunchAgent на Mac каждые две минуты подтягивает только `origin/main` и выполняет
те же проверки локально. Для запуска нужны доступ по SSH к репозиторию, работающий
Docker Desktop, Ollama и вход в пользовательскую сессию macOS.

```sh
/bin/sh deploy/prod/install.sh "$HOME/srv/regagent_prod" /путь/к/проверенному/.env
```

- `.env` содержит параметры приложения. При первой установке публичные URL и
  issuer задаются для `https://regagent.honey-road.tech`. Пригодный для отката
  снимок конфигурации каждого релиза хранится с образами по SHA.
- `data/docs` и `data/okf` общие для всех релизов. При первом запуске пустая
  папка документов заполняется мини-корпусом.
- `logs/deploy.log` и `logs/deploy.log.1` содержат ход проверки, SHA, ошибки
  и итог отката. `state.json` содержит принятый SHA и состояние выкладки.
- `verify-release` — необязательный исполняемый файл для дополнительных
  проверок запущенного кандидата. Получает SHA первым аргументом.

Тесты и сборка идут до переключения контейнеров. После переключения проверяются
веб, API, запрет анонимного запроса, OIDC issuer и готовность Knowledge.
Только после этого SHA становится последней рабочей версией. Откат использует
тот же Compose, снимок env и сохранённые теги по SHA. При прерывании незавершённый
запуск восстанавливается перед следующей попыткой. Томами Postgres, RabbitMQ и
индекса управляет постоянный Compose project. Откат образов сохраняет данные,
поэтому изменения схемы БД должны быть совместимы с предыдущей версией.

## Cloudflare

1. В Cloudflare Zero Trust откройте **Networking → Tunnels** и выберите туннель
   для RegAgent. В **Published application** укажите hostname
   `regagent.honey-road.tech` и origin `http://proxy:8080`.
2. В **Add a replica → Docker** возьмите значение токена `eyJ…` и запишите
   строку `TUNNEL_TOKEN=…` в `~/srv/regagent_prod/cloudflare.env`.
   Этот файл не относится к GitHub Secrets.
3. Для публичного входа через Keycloak снимите Cloudflare Access с этого
   hostname. Запустите коннектор после первого принятого релиза.

```sh
/bin/sh ~/srv/regagent_prod/bin/start-tunnel.sh ~/srv/regagent_prod
docker logs --tail 30 regagent-prod-tunnel
```

Коннектор работает в Docker в сети `regagent-prod_default`. Веб и API проверяются
с Mac через loopback-порт 18082. При прямом открытии корневого пути Caddy
перенаправляет браузер на публичный URL.
