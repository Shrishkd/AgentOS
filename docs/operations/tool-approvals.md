# Review a protected tool call

Open the [approval inbox](http://127.0.0.1:8080/approvals) on the local Compose stack. The inbox takes a tenant UUID and a short-lived bearer token. It keeps the token in page memory and refreshes pending requests every five seconds. Approvers and tenant administrators can decide. Developers can submit runs and inspect the resulting steps.

## Create a reviewable report

Run the following in PowerShell from the repository root. This example creates local development identities for a workshop tenant and installs the deployed report tool.

```powershell
docker compose build operator
$tenant = (docker compose run --rm --no-deps -T operator grant --tenant review-workshop --subject workshop-admin --role admin).Trim()
docker compose run --rm --no-deps -T operator grant --tenant review-workshop --subject workshop-reviewer --role approver
$admin = (docker compose run --rm --no-deps -T operator token --subject workshop-admin).Trim()
$reviewer = (docker compose run --rm --no-deps -T operator token --subject workshop-reviewer).Trim()
$root = "http://127.0.0.1:8080/v1/tenants/$tenant"
$headers = @{ Authorization = "Bearer $admin" }
function Post-Workshop($path, $body) {
  Invoke-RestMethod -Method Post -Uri "$root$path" -Headers $headers -ContentType application/json -Body ($body | ConvertTo-Json -Depth 8)
}
$tool = Post-Workshop '/tools' @{name='save_report'}
$agent = Post-Workshop '/agents' @{name="Report review $([guid]::NewGuid().ToString().Substring(0,8))"}
$versions = "/agents/$($agent.id)/versions"
$version = Post-Workshop $versions @{
  instructions='Create the requested report.'; provider='mock'; model='deterministic'
  tool_version_ids=@($tool.id); limits=@{max_steps=3; max_tokens=12000; timeout_seconds=1800}
}
Invoke-RestMethod -Method Post -Uri "$root$versions/$($version.id)/publish" -Headers $headers
$headers['Idempotency-Key'] = [guid]::NewGuid().ToString()
$run = Post-Workshop '/runs' @{
  agent_version_id=$version.id
  input=@{tool_call=@{name='save_report'; arguments=@{title='Weekly update'; sections=@('All scheduled jobs completed.')}}}
}
Invoke-RestMethod -Uri "$root/runs/$($run.id)" -Headers $headers
```

Open the inbox with `$tenant` and `$reviewer`. Select the pending report, inspect its arguments and digest, then choose **Approve** or **Reject**. While it waits, the run holds no worker slot and its tool step has zero dispatches. After approval the next worker verifies the current grant, tool version, arguments, digest and expiry. The saved report and completed tool step commit together. After rejection the run ends with `approval_rejected`.

```powershell
$url = "$root/runs/$($run.id)"
$steps = Invoke-RestMethod -Uri "$url/steps" -Headers $headers
$steps | Format-Table ordinal,kind,status,dispatch_count,error_code
Invoke-RestMethod -Uri "$url/checkpoints" -Headers $headers
$reportId = ($steps | Where-Object kind -eq tool).result.report_id
if ($reportId) { Invoke-RestMethod -Uri "$root/reports/$reportId" -Headers $headers }
```

## Registered tools

| Tool | Inputs and behavior | Review |
| --- | --- | --- |
| `text_length` | Counts Unicode code points. Existing version-1 grants remain usable. | Low risk |
| `calculator` | Evaluates bounded arithmetic using numbers, parentheses and the four basic operators. | Low risk |
| `inventory_lookup` | Lists hardware or software from a read-only sample SQLite database. Accepts a category and an optional row limit. | Low risk |
| `save_report` | Saves a title and sections to the run's tenant. The step UUID identifies the report. | Approval required |
| `web_search` | Returns up to five English Wikipedia page titles for the approved query. | Approval required |
| `http_get` | Reads a bounded UTF-8 response from the approved HTTPS URL on `www.rfc-editor.org`. | Approval required |

Use `GET /tools/catalog` to inspect the deployed schemas, timeouts, output limits and risks. A tenant administrator registers a version with `POST /tools` and can disable it with `PATCH /tools/{id}` and `{"enabled":false}`. Grant the registered ID when publishing an agent version. The worker advertises only enabled, recognized, granted versions to the model.

Network tools resolve the fixed hostname, validate every address, connect to a validated public address and verify TLS against the original name. They reject redirects and encoded responses. A transient network failure allows one scheduled retry with a fresh authorization check. The controlled database exposes a named parameterized lookup and ships independently of tenant data.

## Decision and recovery semantics

The approval window ends at the earlier of 15 minutes and the run deadline. Cancellation invalidates pending proposals. A duplicate decision with the same digest, decision and reason returns the original receipt. A different decision or digest returns HTTP 409. A grant revoked while a proposal waits prevents dispatch even if the reviewer approves.

`GET /runs/{id}/checkpoints` includes the approval wait and its resolution alongside completed model and tool steps. Page with `after_event` when multiple boundaries share a tool ordinal. The step history records dispatch counts, results and tool errors. The approval receipt identifies the deciding tenant member and both state-event sequence numbers.

The disposable integration suite exercises approval from the browser, conflicting and duplicate decisions, role and tenant isolation, altered arguments, revocation, expiry and cancellation. Run `python scripts/check_foundation.py --browser` with Docker and the dashboard test browser installed.
