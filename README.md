# OpenFGA RAG capacity lab (OpenFGA + Postgres + live dashboard)

One stack gives you: OpenFGA on Postgres (as designed for the RAG app), a Grafana dashboard,
a synthetic 2 lakh document data set, and k6 / Python load tests.

## 1. Start everything
```bash
docker compose up -d          # first start pulls images, wait ~30s
```
| What | URL |
|---|---|
| Grafana dashboard ("OpenFGA capacity test", opens by default, no login) | http://localhost:3001 |
| OpenFGA API | http://localhost:8080 |
| OpenFGA Playground | http://localhost:3000 |
| Prometheus | http://localhost:9090 |

Check: `curl localhost:8080/healthz` and Prometheus -> Status -> Targets (all should be UP).

Fixed "one instance" size (change as you like):
```bash
FGA_CPUS=2 FGA_MEM=2g PG_CPUS=2 PG_MEM=4g docker compose up -d
```

## 2. Load 2 lakh documents of permissions
```bash
pip install httpx
python seed.py                # writes state.json with store + model ids
```

## 3. Run the capacity test with k6, pushing metrics to the dashboard
```bash
K6_PROMETHEUS_RW_SERVER_URL=http://localhost:9090/api/v1/write \
K6_PROMETHEUS_RW_TREND_STATS="avg,p(95),p(99),max" \
k6 run -o experimental-prometheus-rw loadtest.k6.js
```
- If your k6 version says the output is unknown, use `-o prometheus-rw` instead.
- The test ramps up and aborts when p95 > 200 ms or errors > 1%. Tweak with
  `-e MAX_RATE=500 -e P95_MS=100 -e MODE=check`.
- Run k6 on another machine if you can: set `-e BASE_URL=http://<server>:8080`
  and `K6_PROMETHEUS_RW_SERVER_URL=http://<server>:9090/api/v1/write`.
- Without the k6 flags the OpenFGA, CPU and Postgres panels still work. Only the k6 panels stay empty.

Quick Python alternative (no k6): `python loadtest.py --requests 1000 --concurrency 10`

## 4. How to read the dashboard
- **Top row:** req/s, p95 latency, error %, CPU of OpenFGA and Postgres, and k6's offered load.
- **Bottleneck:** the "Container CPU %" panel. 200 means a 2-core limit is full. Postgres at the limit
  first = database bound; OpenFGA first = add OpenFGA replicas.
- **Breaking point:** the first moment p95 passes your target or errors appear. The offered rate
  just before that is your capacity (batch-checks/s; divide by 1-2 calls per user query).
- **Pool pressure:** "Postgres connections": pool total at max with idle 0 means raise max connections.
- **Memory fit:** buffer cache hit % dropping means the data no longer fits in Postgres memory.
- **k6 req/s vs OpenFGA req/s:** if k6 is higher, requests are queueing or failing.

## 5. Experiments (one change per run)
1. Baseline. 2. Bigger Postgres (`PG_CPUS=4`). 3. OpenFGA Check cache on (see "Running OpenFGA in Production" docs for flags).
4. Two OpenFGA replicas behind a load balancer.

## Notes
- Grafana runs with anonymous admin access: for local testing only. Don't expose it.
- If a panel is empty, open Prometheus -> Graph and search for the metric name to see what your
  OpenFGA version exposes, then edit the panel query. Metric names can differ between versions.
- cAdvisor needs access to the Docker host. On Docker Desktop (Mac/Windows) container CPU panels
  can be missing or approximate. On a Linux server they work normally.
- Reset everything: `docker compose down -v`.
