# Local development

Install Docker with Linux containers and the Compose v2 command or newer. The first build downloads container images and locked packages. Start from the repository root.

```sh
docker compose up --build
```

Open [AgentOS](http://127.0.0.1:8080). For background mode, append `-d`. Use `docker compose ps -a` to inspect startup. The credentials and migration jobs should exit with code 0. The dashboard, API, PostgreSQL, Redis and NATS should report healthy. The gateway serves dashboard assets, the [API reference](http://127.0.0.1:8080/docs), and probe requests on the same origin.

## Startup and storage

On first boot the credentials job generates random passwords in two Compose volumes. PostgreSQL initializes a restricted API login. The migration job uses the administrative credential to upgrade the schema to its current head. The API runs as an unprivileged OS user and has read access to the schema revision. New domain tables and migrations ship with the features that own them.

Phase 2 grants that API login scoped registry and run privileges, forces row-level security on the tenant tables and commits dispatch intent with each enqueue. The optional operator profile manages membership and local identity keys. [Identity settings and first run](first-run.md)

The API opens long-lived async client pools and closes them at shutdown. A readiness request checks PostgreSQL's revision and Redis PING concurrently with per-probe timeouts. Broker health is independent so a broker outage can be identified while the control plane remains ready. The dashboard polls every ten seconds while visible and supports pausing or manually refreshing checks.

`docker compose down` stops the stack and retains the database, passwords and JetStream storage. Keep the credential volumes with the PostgreSQL volume when reusing a local installation. Redis is ephemeral. The local topology assumes a trusted developer machine and Compose network. Deployment hardening is tracked in Phase 10.

## Environment reference

To override a Compose setting, set it in your shell or in a local `.env` beside `compose.yaml`. The default startup command works from a fresh checkout.

| Setting | Consumer | Default / purpose |
| --- | --- | --- |
| AGENTOS_PORT | Compose gateway binding | 8080 on 127.0.0.1. Choose a free port if already occupied. |
| AGENTOS_LOG_LEVEL | API | INFO. Also accepts DEBUG, WARNING or ERROR. Logs are JSON records with timestamp, status, request ID and duration. |
| AGENTOS_HEALTH_TIMEOUT_SECONDS | API | 2 seconds per probe. Must be positive and at most 10. |
| AGENTOS_DB_HOST / AGENTOS_DB_PORT | API and migrate | Compose supplies postgres and the client defaults to 5432. |
| AGENTOS_DB_NAME / AGENTOS_DB_USER | API and migrate | agentos database. The API connects as agentos_api, the migration job as postgres. |
| AGENTOS_DB_PASSWORD_FILE | API and migrate | Required readable file containing the corresponding password. Compose mounts only the role's own secret volume. |
| AGENTOS_REDIS_URL | API | Compose supplies redis://redis:6379/0. |
| POSTGRES_DB / POSTGRES_PASSWORD_FILE | PostgreSQL initialization | Compose supplies agentos and the admin password file. |

The last five rows describe service settings wired in `compose.yaml`. For standalone backend development, supply the applicable `AGENTOS_*` variables directly. Keep file secrets out of source control.

## Verification

- Backend tests use Python 3.12. From `backend` run `uv run --locked pytest -q`.
- From `dashboard` run `npm ci` and `npm run build` using Node 22.12+ or Node 24. To work on the UI, start Compose on its default port and run `npm run dev` here. Vite forwards probe requests to the gateway.
- From the root, `python scripts/check_foundation.py` creates a disposable project on a random loopback port. It verifies clean startup, migration replay and downgrade/upgrade, tenant permissions, version grants, concurrent run admission and transaction rollback. It then exercises dependency outages, recreates the containers and retrieves a persisted run. It removes the temporary project's resources.
- For the same browser check as CI, install dashboard dependencies, run `npx playwright install chromium` from `dashboard`, then run `python scripts/check_foundation.py --browser` from the root. To use an installed Chrome instead, set `AGENTOS_BROWSER_CHANNEL=chrome` for the test. The browser opens the real dashboard, pauses polling, simulates a failed health request, refreshes after recovery and checks mobile overflow.

## Troubleshooting

| Symptom | Check and recovery |
| --- | --- |
| Gateway port in use | Set AGENTOS_PORT in the root .env, rerun Compose and open the new port. |
| API waits for migrations | Inspect `docker compose logs migrate postgres`. Fix the reported connection or migration error and rerun `docker compose up --build`. |
| Database readiness down | Compare `docker compose run --rm migrate alembic current` with `docker compose run --rm migrate alembic heads`. Apply missing revisions with `docker compose run --rm migrate`. Inspect API logs for the probe error type. |
| API alive, cache unavailable | Inspect `docker compose logs redis`, restore Redis with `docker compose up -d redis`, then refresh. |
| Broker unavailable | Inspect `docker compose logs nats` and its volume. Restore with `docker compose up -d nats`. |
| Gateway reports unknown database/cache health | Inspect `docker compose ps -a` and `docker compose logs api dashboard`. Unknown means the gateway could not obtain those probe results. |
