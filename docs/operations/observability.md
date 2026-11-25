# Inspect an execution

From the repository root, start the API, workers and local monitoring services. Keep the same model environment variables and worker scale that you use for ordinary Compose runs.

```sh
docker compose -f compose.yaml -f compose.observability.yaml up -d --build
```

Open **Execution health** for run states, healthy workers and retry outcomes. **Model usage** shows provider-reported tokens, estimated cost and invocation settlement states. The charts show totals across tenants at each scrape. Their time range selects the period of snapshots displayed. Grafana and Jaeger are loopback-only operator tools with access to system-wide data. Override their host ports with `AGENTOS_GRAFANA_PORT` and `AGENTOS_JAEGER_PORT`. [Execution health](http://127.0.0.1:3000/d/agentos-execution) · [Model usage](http://127.0.0.1:3000/d/agentos-usage)

## Follow a run

Use a viewer or developer bearer token and the tenant and run IDs returned by submission. These PowerShell examples use the local gateway.

```powershell
$tenantUrl = "http://127.0.0.1:8080/v1/tenants/$tenantId"
$runUrl = "$tenantUrl/runs/$runId"
$headers = @{Authorization = "Bearer $token"}
Invoke-RestMethod "$runUrl/diagnostics" -Headers $headers
curl.exe -sN -H "Authorization: Bearer $token" -H "Accept: text/event-stream" "$runUrl/events"
```

Each SSE event carries the same sequence, timestamp and payload as its JSON history entry. Save the ID after processing each event. To reconnect after event 3, add `-H "Last-Event-ID: 3"` to the curl command. The response starts at the next stored sequence, tails new commits, then closes when it delivers the terminal state. Idle streams emit heartbeat comments. A reconnect after the final sequence returns an empty stream. Invalid or future cursors return HTTP 422. The API rechecks credentials and tenant membership while tailing.

## Diagnose a retry or failure

Read `/diagnostics` for the current state, elapsed seconds, last worker and attempt, last step, retry count, failure code and trace ID. The retry count includes attempts that entered a provider or tool retry and attempts closed for lease expiry. Inspect `/attempts` for individual outcomes, `/steps` for tool and model results, `/checkpoints` for the last committed continuation, and `/usage` under the run for invocation settlement and provider latency.

When the run has a trace ID, open `http://127.0.0.1:16686/trace/<trace_id>`. A submission span is the parent of worker attempts, including attempts claimed on another worker. Expand a model or tool span to inspect its ordinal, outcome and normalized error. Filter API and worker JSON logs by the same trace or run ID. Runs submitted before tracing was enabled still have their durable database history. The local Jaeger configuration stores spans in memory, so restarting it clears the trace view. PostgreSQL retains run events and usage, and the Prometheus volume retains metric snapshots. [Jaeger all-in-one storage](https://www.jaegertracing.io/docs/2.15/getting-started/)

## Reconcile usage

```powershell
Invoke-RestMethod "$tenantUrl/usage?run_id=$runId" -Headers $headers
Invoke-RestMethod "$runUrl/usage" -Headers $headers
```

The first request sums settled rows and counts reported, rejected, unknown, outstanding and unpriced invocations. Omit `run_id` for tenant totals. Optional `since` and `until` select invocations by creation time using an inclusive start and exclusive end. Both timestamps require a time zone. The second request lists individual invocations with cursor pagination.

Only reported rows contribute token totals. Estimated USD sums the reported rows with known prices. Consult the unpriced and unknown counts before interpreting a zero. Prometheus scrapes the internal API every five seconds. Its bounded labels exclude tenant, run and model identifiers.

## Verify locally

```sh
python scripts/check_foundation.py --browser --observability
```

The isolated suite disconnects and resumes a queued run, tails its retry through completion, checks both worker attempts in Jaeger and compares the provisioned Grafana queries with PostgreSQL usage rows. It also exercises migration replay, tenant isolation, approval handling and crash recovery.
