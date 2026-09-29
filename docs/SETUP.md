# Run faat locally

## 1. Install dependencies

Requirements: Python 3.11+, Node 24.19+, pnpm 11.19.0. Node 24 is required for the gateway's built-in SQLite API.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[service,contracts,validation]'
cd frontend
pnpm install --frozen-lockfile
cp .env.server.example .env.server.local
cd ..
```

## 2. Configure the local finance service

The SQLite reference profile supports interactive development. Hosted scheduled Watch uses PostgreSQL.

Create private local secrets:

```bash
mkdir -p .local
chmod 700 .local
python - <<'PY'
from pathlib import Path
import base64, secrets
p = Path('.local')
for name, value in [('gateway-secret', secrets.token_hex(32)),
                    ('session-hmac', base64.b64encode(secrets.token_bytes(32)).decode())]:
    f = p / name
    if not f.exists():
        f.write_text(value + '\n')
        f.chmod(0o600)
PY
```

In the backend shell:

```bash
export FINANCE_SERVICE_SESSION_KEY_ID=local-v1
export FINANCE_SERVICE_SESSION_HMAC_FILE="$PWD/.local/session-hmac"
export FINANCE_SERVICE_REFERENCE_SQLITE_PATH="$PWD/.local/finance.sqlite"
export MACHINE_GATEWAY_SECRET_FILE="$PWD/.local/gateway-secret"
export FINANCE_SERVICE_PUBLIC_DOMAIN=localhost
export GWDC_QWEN_BASE_URL=https://api.bricksum.com/v1
export GWDC_QWEN_MODEL_ID=qwen3-32b
# Set GWDC_QWEN_API_KEY securely, or GWDC_QWEN_API_KEY_FILE to an owner-only key file.
python -m uvicorn finance_service.entrypoint:app --host 127.0.0.1 --port 8190
```

The backend needs its Kiln key for condition extraction. The gateway also needs the key for conversational responses. Missing providers are reported as unavailable; no fake model response replaces them.

## 3. Configure the gateway

Edit `frontend/.env.server.local`:

```dotenv
KILN_API_KEY=<your Kiln key>
APP_ORIGIN=http://127.0.0.1:5173
PORT=8000
DATABASE_PATH=.data/machine.sqlite
DAILY_MESSAGE_LIMIT=100
FINANCE_API_URL=http://127.0.0.1:8190
FINANCE_SERVICE_TOKEN=<exact contents of .local/gateway-secret>
```

In another terminal:

```bash
cd frontend
pnpm dev:all
```

Open `http://127.0.0.1:5173/?network=nile`. Create an agent and connect a Nile wallet with TronLink. Ownership proof is a message signature; financial steps require separate transaction signatures. Use your own test funds and review each transaction. No key is prefixed with `VITE_`.

Vite forwards `/v1` to the Node gateway on port 8000. The gateway validates its session and wallet, authenticates to the finance service, and maps finance routes to `/v1/machine/*`. The finance service does not accept unauthenticated browser identity headers.

## 4. Hosted PostgreSQL and scheduled Watch

Apply `009_finance_service.sql`, `011_finance_postgres_runtime.sql`, `012_finance_resilience.sql` and `013_finance_journal_audit.sql` using `scripts/apply_postgres_migrations.py` with a migration-role DSN. Follow [PostgreSQL operations](operations/postgres-resilience.md) for role separation, TLS and recovery.

- **API:** set `FINANCE_SERVICE_POSTGRES_API_DSN_FILE` to its owner-only DSN file with verified TLS. Remove the SQLite profile. Do not give the API the worker DSN.
- **Watch:** run `python -m finance_service.machine_worker` as a separate service with its required API/worker DSN files. Create `/var/lib/machine-finance/` writable by that worker for its heartbeat. Observation does not grant signing authority.
- **Gateway:** build using `pnpm build`, then run `pnpm server` from `frontend/`. Set `NODE_ENV=production` and an HTTPS `APP_ORIGIN`.
- **Network:** put a TLS reverse proxy before the gateway. Keep the finance API and PostgreSQL private. Store runtime databases and secrets outside release directories.

`deploy/` contains earlier deployment templates: adapt users, paths and environment files. `web/` is the earlier reference client; `frontend/` is the faat UI. Do not seed the hosted workspace with `FINANCE_SERVICE_DEMO_STORY_PATH`.

## 5. Verify

Frontend: `pnpm test` and `pnpm build` from `frontend/`. `pnpm contract` regenerates the OpenAPI document from Zod contracts.

Backend: run the product regression command in the [README](../README.md). Broader `unittest discover -s tests` includes research and external-runtime checks that require their dependencies and environments. Solidity verification scripts require their documented compiler/runtime inputs. Tests do not deploy contracts or authorize user transactions.
