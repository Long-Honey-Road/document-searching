# Web RegAgent

Веб-приложение использует Vite, React, TypeScript, Tailwind и shadcn/ui.

Поднимите Keycloak и Caddy через Compose. Vite читает переменные `VITE_*`
из корневого `.env`. Локальный origin Vite по умолчанию — http://localhost:5173.

```bash
npm ci
npm run dev
npm test
npm run lint
npm run build
```
