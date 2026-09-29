# faat frontend and gateway

React 19, Vite, TypeScript, TanStack Query, TronWeb and a Node 24 SQLite-backed gateway.

See the [project README](../README.md), [setup](../docs/SETUP.md), [architecture](../docs/ARCHITECTURE.md) and [verified scope](../docs/VERIFICATION.md).

```bash
pnpm install --frozen-lockfile
cp .env.server.example .env.server.local
# Configure Kiln and the finance service as documented in SETUP.md.
pnpm dev:all
```

`pnpm test` runs frontend regressions. `pnpm build` checks types and creates the production bundle. `pnpm contract` regenerates `docs/openapi.json` from the Zod contracts consumed by the app.

Public branding is faat. Internal machine-prefixed identifiers remain for API/session compatibility.
