# Deploy and recover AgentOS with Docker Compose

The production overlay runs a single-host release with a TLS gateway, externally supplied credentials, PostgreSQL WAL archiving and private Prometheus and Alertmanager services. PostgreSQL stores the run history and checkpoints. Recovery can use empty Redis and JetStream volumes. The host and its storage remain single points of failure.

## Prepare the release

Use Docker Compose 2.24.4 or newer. Copy the environment example outside the repository. Assign a unique tag per build or use an image digest. Use the matching release checkout for the mounted database, broker, gateway and monitoring configuration. Provision the two external Docker volumes on encrypted, durable storage. Keep the base backups and archived WAL replicated off-host, together with a protected copy of the release configuration and workload credentials. A local Docker volume alone cannot survive host loss. [Environment template](../../infra/deployment/production.env.example)

Under `AGENTOS_SECRET_DIR`, provision the following files. On a Linux host restrict the parent directory to deployment administrators and give each consuming container UID read access to its mounted files. Keep the PostgreSQL password and broker password in each role directory independent. Generate each password with at least 32 random bytes encoded as hex. The API and worker use UID 65532, the gateway uses 101, and Alertmanager uses 65534.

| Files | Consumer and purpose |
| --- | --- |
| `admin/password`, `api/password`, `worker/password`, `publisher/password` | PostgreSQL, migration and the corresponding runtime roles |
| `worker/broker`, `publisher/broker` | The two authenticated JetStream identities |
| `identity/issuer.pem` | Trusted RS256 public key in PEM format. Configure the issuer and audience to match the identity provider |
| `tls/fullchain.pem`, `tls/privkey.pem` | Certificate chain for the gateway hostname and its matching private key |
| `alertmanager/alertmanager.yml`, `alertmanager/webhook-url` | On-call receiver configuration and its secret HTTPS URL |
| `worker/model-key` | Optional remote provider key. Set `AGENTOS_MODEL_API_KEY_FILE=/secrets/runtime/model-key` and configure the HTTPS model URL and model name |

Copy the receiver example and populate its URL file using the secret manager. Restrict host ingress to the TLS endpoint and required administrators. The database network is internal. Apply host egress rules to the worker and notification network for the approved destinations. The default bind address is loopback. Point a trusted ingress at it or set the bind address to the host's approved interface. [Receiver template](../../infra/deployment/alertmanager.example.yml)

The commands below run in a POSIX shell from the release checkout. Set the image tags in the environment file to the exact tags built here. Registry-based deployments can pull the same immutable images onto the host instead.

```sh
docker build -t agentos-backend:release-1 backend
docker build -t agentos-dashboard:release-1 dashboard
dc() { docker compose --env-file /etc/agentos/production.env -p agentos-prod -f compose.yaml -f compose.production.yaml "$@"; }
dc config --quiet
dc up -d --no-build --wait --scale worker=2
dc ps
dc run --rm --no-deps operator grant --tenant example --issuer https://identity.example.org/ --subject user-id --role developer
```

Verify `/health/ready` through the trusted HTTPS hostname and check both worker replicas and the publisher with `dc ps`. An accepted run must have an ordered event history and a terminal outcome. The operator grants tenant membership. The identity provider signs the access token. Issuer key rotation requires coordinating token lifetimes with replacing the public PEM. Rotate database credentials by setting the corresponding role passwords and updating the matched secret files during a maintenance window. Recreate consumers after rotation. Rotate the two broker credentials on NATS and the corresponding consumers together.

## Back up and establish a recovery point

Schedule `dc run --rm --no-deps backup BACKUP_ID` daily on the host. Use a unique ID, monitor its exit code and check that off-host replication completed. The job runs a streaming physical base backup and verifies its manifest. Keep at least two verified base backups and every WAL segment and timeline history file required by the oldest retained backup. Set retention to the business recovery window. Retire older files only after a successful independent restore.

```sh
dc run --rm --no-deps backup release_1
dc exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d agentos -c "SELECT pg_create_restore_point('before_release_2')"
dc exec -T postgres psql -At -U postgres -d agentos -c "SELECT pg_walfile_name(pg_current_wal_insert_lsn())"
dc exec -T postgres psql -U postgres -d agentos -c "SELECT pg_switch_wal()"
```

Record the returned segment name and confirm it exists in the replicated archive before releasing. WAL also switches on activity after the configured 60-second archive timeout. Recovery point loss depends on the latest available archived segment and replication lag. Monitor free space on PGDATA and the archive and the age of the latest verified off-host backup.

## Drain and replace a release

Set the gateway to maintenance at ingress and allow submissions already in flight to finish. Take a base backup and confirm a named restore point as above. Run `dc stop worker` to send SIGTERM. Each session stops claiming and gives its active calls eight seconds to finish. Longer calls leave fenced leases for normal recovery. Inspect active attempts, uncertain model outcomes and approvals before proceeding. Stop the publisher and API with `dc stop publisher api`.

Update the release checkout and image references. Run `dc run --rm migrate`, then `dc up -d --no-build --wait --scale worker=2`. Confirm TLS readiness, fresh worker sessions, a successful receipt and cleared queue and outbox alerts before reopening ingress. A compatible schema permits returning to the previous image tags. For an incompatible migration use the recovery procedure and reconcile writes made since the selected restore point.

## Restore to an isolated project

1. Fence the original writers, workers and publisher. Preserve the original data volumes. Choose a completed base backup from before the named restore point. Mount a separate recovered copy of both off-host backup volumes. Use the original credentials, the matching PostgreSQL major version, and the application release matching the recovery point.
2. Define `dr` like `dc` above with a **new project name**, different HTTPS port and the recovered backup volume settings. With fresh project volumes run `dr run --rm --no-deps restore release_1 before_release_2`. The restore verifies the backup manifest and refuses a populated PGDATA directory. If copying fails, retry in another fresh project.
3. Start `dr up -d --no-build --wait --scale worker=0 --scale publisher=0` with ingress still fenced. PostgreSQL replays archived WAL and promotes at the named point. Inspect its logs and verify `SELECT pg_is_in_recovery()` returns false and the expected migration and tenant history exist. A missing target or WAL segment requires correcting the archive and repeating from an empty volume.
4. Compare the restored execution and approval history with provider receipts and protected external effects after the restore point. Reconcile affected runs before replay. Start the publisher and workers with `dr up -d --no-build --wait --scale worker=2`. Check that expired leases close and replacements resume from the recorded checkpoint. Verify the output, attempt generations and side effects before directing traffic to the recovered gateway. Create a new verified base backup on its promoted timeline. [Worker recovery](worker-failure-runbook.md)

For disaster recovery through the end of the available WAL, supply `latest` as the restore target. Verify the archive is contiguous and reconcile the last committed business records with external receipts. A named target makes PostgreSQL fail recovery if the archive ends before that point.

## Respond to alerts

| Signal | First checks and recovery |
| --- | --- |
| API scrape unavailable | Inspect API, migrations, PostgreSQL, network and Prometheus logs. Check HTTPS readiness and the internal scrape target separately. |
| No healthy workers | Check worker health, DB login, NATS credentials and JetStream. Restore capacity and watch an eligible run finish. |
| Old queued run | Inspect the oldest run's model, compatible workers, tenant concurrency and busy attempts. Add appropriate capacity or correct the model configuration. |
| Old unpublished event | Check the publisher health probe, broker storage and authentication. The publisher retries the durable outbox when service returns. |
| Expired owned lease | Check worker heartbeat, DB reachability and the maintenance loop. Restore workers, then inspect the replacement attempt and checkpoint. |
| WAL archive failure | Inspect PostgreSQL archiver logs, archive free space, permissions and off-host replication. Verify a new segment arrives and the alert clears before declaring backups current. |

The API readiness probe covers PostgreSQL and Redis. The publisher health probe requires a recent successful database and broker scan loop. The gateway serves only application traffic and health endpoints. Prometheus and Alertmanager are reachable inside the deployment network. Use authenticated host administration to inspect them. Run the private drill after changing credentials, images or backup storage.

```sh
cd backend
uv run --locked python ../scripts/check_deployment.py
```

## Verified deployment drill

On 28 November 2026 the Docker Desktop drill verified a base backup in **2.39 seconds** and drained two workers in **2.94 seconds** while a model call completed in its original attempt. All six alert rules passed their firing and clearing checks. The worker outage reached a private webhook through Alertmanager.

Restoring into empty data, Redis and JetStream volumes, starting the stack, and observing the interrupted run succeed through HTTPS took **32.77 seconds** from the start of the named-point restore. The first attempt expired, and the second reused the persisted final checkpoint. The latest-WAL clone included a committed post-marker write. The named-point clone excluded it. These are single-host drill timings with a small fixture database.
