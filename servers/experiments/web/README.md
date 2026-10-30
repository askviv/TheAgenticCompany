# Viv experiment frontend

React + TypeScript, React Router, Vite and Tailwind. UI components in `src/components/ui` were generated with the official shadcn CLI (Base UI, neutral Nova preset); `components.json` records the configuration.

```sh
npm ci
npm run build
npm run lint
```

The build writes to `../static/app`, served by the Flask backend. Use the multi-stage Dockerfile in the parent directory for deployment. API calls use the same origin and Flask session CSRF; Run uses the UUID supplied by `/api/session` for safe retries on the private HTTP LAN. Server-sent events refresh the tracker when the chat changes. Deployment and verification are documented in `deploy/single-host/experiments.md`.
