# Register an agent and submit a run

From the repository root, start the local stack and build the operator image. The operator is a Compose profile for administrative tasks. It provisions a tenant membership and issues short-lived local development tokens. The API receives only the public verification key. The following examples use PowerShell.

```powershell
docker compose up --build -d --wait
docker compose build operator
$tenantId = (docker compose run --rm --no-deps -T operator grant --tenant workshop --subject developer-one --role developer).Trim()
$accessToken = (docker compose run --rm --no-deps -T operator token --subject developer-one).Trim()
$headers = @{ Authorization = "Bearer $accessToken" }
$rootUrl = "http://127.0.0.1:8080/v1/tenants/$tenantId"
```

Create an agent once. On subsequent visits, retrieve its ID with `GET /agents`. The same agent can have multiple published versions.

```powershell
$agent = Invoke-RestMethod -Method Post -Uri "$rootUrl/agents" -Headers $headers -ContentType application/json -Body '{"name":"Reporter"}'
$config = @{
  instructions = "Return a receipt for the submitted input."
  provider = "builtin"
  model = "receipt"
  limits = @{ max_steps = 5; max_tokens = 2048; timeout_seconds = 3600 }
  tool_version_ids = @()
} | ConvertTo-Json -Depth 4
$versionsUrl = "$rootUrl/agents/$($agent.id)/versions"
$version = Invoke-RestMethod -Method Post -Uri $versionsUrl -Headers $headers -ContentType application/json -Body $config
Invoke-RestMethod -Method Post -Uri "$versionsUrl/$($version.id)/publish" -Headers $headers
```

Submit to the published version. Keep the same key and body to retry an individual submission. Use a fresh key for a new task.

```powershell
$submissionHeaders = $headers.Clone()
$submissionHeaders["Idempotency-Key"] = [guid]::NewGuid().ToString()
$body = @{ agent_version_id = $version.id; input = @{ text = "A worker recovered its checkpoint." } } | ConvertTo-Json
$run = Invoke-RestMethod -Method Post -Uri "$rootUrl/runs" -Headers $submissionHeaders -ContentType application/json -Body $body
Invoke-RestMethod -Uri "$rootUrl/runs/$($run.id)" -Headers $headers
Invoke-RestMethod -Uri "$rootUrl/runs/$($run.id)/events" -Headers $headers
Invoke-RestMethod -Uri "$rootUrl/runs/$($run.id)/attempts" -Headers $headers
Invoke-RestMethod -Uri "$rootUrl/workers" -Headers $headers
```

A successful submission returns 201 and the queued run. An identical retry returns 200 and the same ID, including after version deactivation. Changing the input under the same key returns 409. The timeline begins with CREATED and QUEUED. To cancel queued work, post to `/runs/{id}/cancel`; both cancellation transitions appear in history.

Poll the run until it reaches SUCCEEDED. A receipt worker computes SHA-256 over the UTF-8 JSON input with sorted keys, compact separators and unescaped Unicode. The result identifies the worker and attempt. This built-in handler uses no model credentials and accepts no tool grants.

## Tenant identity and storage

The local issuer uses RS256 and a 30-minute token lifetime. The first token command generates a key pair in persistent Compose volumes. Only the operator mounts the private key. Membership is looked up on every request using the verified issuer, subject and requested tenant. The tenant API role is a non-owner subject to forced PostgreSQL row-level security. Tenant-qualified foreign keys also apply to privileged writers.

Use `operator grant` again to change a membership role. Viewers and approvers have read access to the Phase 2 API. Developers and admins can manage versions and submit or cancel runs. For an external RS256 issuer, provision its subject with `grant --issuer <issuer>`, configure `AGENTOS_TOKEN_ISSUER` and `AGENTOS_TOKEN_AUDIENCE`, and mount its trusted public PEM at the API's `AGENTOS_TOKEN_PUBLIC_KEY_FILE` (default `/keys/public/issuer.pem`). Obtain access tokens from that issuer. External key distribution and rotation are operator-managed.

The default tenant limits are 1,000 outstanding runs, 120 fresh submissions per minute and four concurrent claims. PostgreSQL serializes quota checks per tenant. Redis enforces the submission rate window. Each enqueue writes a run event and an outbox row in one transaction. The publisher sends a wakeup, and the next worker with capacity claims an eligible run. [Worker operations](worker-failure-runbook.md)
