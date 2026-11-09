# CLAUDE.md

Guidance for AI assistants working in this repo.

## What this repo is
A capacity-test lab for **OpenFGA** (fine-grained authorization) as used by a RAG app (DocAI):
before returning vector-DB chunks to a user, the app asks OpenFGA "can `user:X` view `document:Y`?".
The lab measures how many such checks/sec one OpenFGA + one Postgres can serve.

## Files
| File | Purpose |
|---|---|
| `docker-compose.yml` | Postgres 16, `migrate` (OpenFGA schema migration), OpenFGA, postgres-exporter, cAdvisor, Prometheus, Grafana. CPU/mem limits via `FGA_CPUS`, `FGA_MEM`, `PG_CPUS`, `PG_MEM`. |
| `common.py` | `FGA_URL` (env, default `http://localhost:8080`), the authorization `MODEL` (JSON form), `save_state`/`load_state` for `state.json`. |
| `seed.py` | Creates a store + model, writes synthetic tuples (default 200k docs, 500 folders, 50 groups, 1000 users) in batches of 100, 16 concurrent. Writes `state.json`. |
| `loadtest.py` | Python load test: per query picks a user + N random candidate docs, batch-checks in chunks of 50 (`--mode check` uses single `/check`). Optional `--list-objects`. |
| `loadtest.k6.js` | k6 ramping-arrival-rate test (batch-check of 50 docs/iteration); aborts when p95 > `P95_MS` or errors > 1%. Reads `state.json` or `-e STORE_ID/MODEL_ID`. |
| `prometheus/prometheus.yml` | Scrapes `openfga:2112`, `postgres-exporter:9187`, `cadvisor:8080`, itself. k6 pushes via remote-write. |
| `grafana/` | Provisioned Prometheus datasource + "OpenFGA capacity test" dashboard. |
| `config.py` | DB setup/check script (`python config.py`): connects to the DocAI RDS host (direct or via jump tunnel when `JUMP_HOST` is set), creates the `openfga` DB if missing, checks migrations and OpenFGA `/healthz`. Credentials come from `.env`. |
| `.env` | Secrets (jump server, `DB_USER`/`DB_PASSWORD`, `OPENFGA_DB_NAME`, optional `OPENFGA_DATASTORE_URI`). Read by `config.py` and by docker compose. |

## Authorization model
```
type user
type group     relations: member: [user]
type folder    relations: viewer: [user, group#member]
type document  relations: parent: [folder]
                          viewer: [user, group#member] or viewer from parent
```
Seed data: user -> 1-3 groups; folder viewable by 1-2 groups; each doc has one parent folder (`d % folders`); ~5% of docs shared directly with a user.

## Data flow
`docker compose up -d` -> `python seed.py` (creates `state.json`) -> `k6 run ...` or `python loadtest.py` -> watch Grafana at http://localhost:3001.

## Database plan (important)
- `config.py` points the DocAI app at the RDS host
  `hex-aws-docaidb-dev-1f37ed-use1-rds-01.cvkiu8yy2qpg.us-east-1.rds.amazonaws.com:5432`, database **`postgres`**.
- **Do NOT use the `postgres` database (the DocAI app's DB) for OpenFGA.**
- Use the **same RDS host** but a **separate database for OpenFGA** (e.g. `openfga`), so OpenFGA's tables
  never mix with the app's tables and can be dropped/reset independently.
- Create it once (as an admin user on that host):
  ```sql
  CREATE DATABASE openfga;
  -- optional, recommended: dedicated least-privilege user
  CREATE USER openfga_user WITH PASSWORD '<secret>';
  GRANT ALL PRIVILEGES ON DATABASE openfga TO openfga_user;
  \c openfga
  GRANT ALL ON SCHEMA public TO openfga_user;
  ```
- `python config.py` does the `CREATE DATABASE` automatically if it is missing (needs CREATEDB privilege).
- docker compose uses `${OPENFGA_DATASTORE_URI}` for `migrate`, `openfga` and `postgres-exporter`; unset = local `postgres` container.
- OpenFGA datastore URI format (used by both `migrate` and `openfga` services):
  `postgres://<user>:<password>@<rds-host>:5432/openfga?sslmode=require`
  Run `openfga migrate` against it before `openfga run`.
- Postgres exporter `DATA_SOURCE_NAME` should point at the same `openfga` database if RDS is used.
- If the RDS is only reachable through the jump server: open an SSH tunnel on the Docker host
  (e.g. local port 15432) and use `host.docker.internal:15432` from containers. The Python tunnel in
  `config.py` (`_start_jump_tunnel`) only works inside a Python process, not for the OpenFGA container.
- With RDS, the local `postgres` container, `PG_CPUS`/`PG_MEM`, and cAdvisor Postgres CPU panels no longer
  reflect the real database; use RDS CloudWatch metrics for DB CPU/connections.

## Conventions / cautions
- Credentials live only in `.env`. Never hardcode them, commit `.env`, or print them in logs.
- Grafana runs with anonymous admin: local only, never expose.
- `state.json` is generated; `docker compose down -v` wipes local data (not RDS).
- Batch-check limit is 50 checks/request and Write limit is 100 tuples/request (OpenFGA defaults); keep scripts aligned.
- Python deps: `httpx` (seed/loadtest/config); `python-dotenv`, `psycopg2-binary`, `sshtunnel` for `config.py`.
