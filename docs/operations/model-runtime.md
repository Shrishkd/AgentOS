# Run a model-backed agent

Start the Compose stack and obtain a tenant ID and bearer token using the [first-run walkthrough](first-run.md). These PowerShell commands reuse its `$headers` and `$rootUrl` variables.

## Try a three-step run

Install the text-length tool for the workshop tenant and grant its exact version to a new agent version.

```powershell
$toolId = (docker compose run --rm --no-deps -T operator install-text-length --tenant workshop).Trim()
$agent = Invoke-RestMethod -Method Post -Uri "$rootUrl/agents" -Headers $headers -ContentType application/json -Body '{"name":"Text counter"}'
$versions = "$rootUrl/agents/$($agent.id)/versions"
$config = @{
  instructions = "Use text_length to count the requested string and return the count."
  provider = "mock"
  model = "deterministic"
  limits = @{ max_steps = 3; max_tokens = 8000; timeout_seconds = 60 }
  tool_version_ids = @($toolId)
} | ConvertTo-Json -Depth 5
$v = Invoke-RestMethod -Method Post -Uri $versions -Headers $headers -ContentType application/json -Body $config
Invoke-RestMethod -Method Post -Uri "$versions/$($v.id)/publish" -Headers $headers
$submit = $headers.Clone()
$submit["Idempotency-Key"] = [guid]::NewGuid().ToString()
$body = @{ agent_version_id = $v.id; input = @{ count_text = "hello" } } | ConvertTo-Json
$run = Invoke-RestMethod -Method Post -Uri "$rootUrl/runs" -Headers $submit -ContentType application/json -Body $body
$url = "$rootUrl/runs/$($run.id)"
Invoke-RestMethod -Uri $url -Headers $headers
Invoke-RestMethod -Uri "$url/steps" -Headers $headers
Invoke-RestMethod -Uri "$url/usage" -Headers $headers
```

Refresh the run until it reaches SUCCEEDED. Its output is `{"answer":"5"}`. The steps show a model proposal, a tool result and a final answer. Usage contains two reported invocations. With ordinary JSON input and no `count_text` field, the mock returns the compact JSON as its answer in one step.

## Connect a Chat Completions endpoint

Set the base URL and exact model name in the root `.env`. Workers append `/chat/completions` to the URL. For an Ollama server reachable on the Docker Desktop host, use `http://host.docker.internal:11434/v1` and the name of an installed tool-capable model.

| Worker setting | Effect |
| --- | --- |
| AGENTOS_MODEL_BASE_URL / AGENTOS_REMOTE_MODEL | Set both to enable remote inference. A worker claims remote runs for this exact model. Other models remain queued until a matching worker starts. Both settings default to empty. |
| AGENTOS_MODEL_API_KEY_FILE | Optional worker-local file containing a bearer credential. A credentialed endpoint requires HTTPS. |
| AGENTOS_MODEL_TIMEOUT_SECONDS | Whole-request HTTP timeout, also bounded by the run deadline. Default 20 seconds. |
| AGENTOS_MODEL_OUTPUT_CAP | Maximum completion tokens requested per invocation. Default 512. The remaining run budget can reduce it. |
| AGENTOS_MODEL_INPUT_USD_PER_MILLION / AGENTOS_MODEL_OUTPUT_USD_PER_MILLION | Operator-supplied input and output prices per million tokens. A remote run with a monetary limit requires both. Unpriced calls report unknown cost. Mock pricing is zero. |

For a credentialed endpoint, add the following to a local Compose override and set `AGENTOS_MODEL_KEY_PATH` to the host key file. Keep that file outside the repository.

```yaml
services:
  worker:
    secrets: [model_api_key]
    environment:
      AGENTOS_MODEL_API_KEY_FILE: /run/secrets/model_api_key
secrets:
  model_api_key:
    file: ${AGENTOS_MODEL_KEY_PATH}
```

Recreate the workers with `docker compose up -d --build --scale worker=2 worker`. Publish a version with provider `openai-compatible` and the configured model. The endpoint receives the pinned instructions and JSON input. Tool-enabled runs use native function calls, call IDs and tool-result messages. The adapter requests `max_completion_tokens` and requires reported prompt and completion usage. Each turn accepts one final text answer or one function proposal. The Phase 4 tool counts up to 8,192 Unicode code points and requires an enabled, explicitly granted tenant tool version.

### Verify a live tool call with Ollama

On Docker Desktop, start Ollama on the host and run these commands from the repository root. The environment settings apply to workers created from this PowerShell session. To reuse them after recreating the workers from another session, put the same values in the root `.env`.

```powershell
ollama pull qwen3:0.6b
$env:AGENTOS_MODEL_BASE_URL = 'http://host.docker.internal:11434/v1'
$env:AGENTOS_REMOTE_MODEL = 'qwen3:0.6b'
$env:AGENTOS_MODEL_TIMEOUT_SECONDS = '120'
$env:AGENTOS_MODEL_INPUT_USD_PER_MILLION = '0'
$env:AGENTOS_MODEL_OUTPUT_USD_PER_MILLION = '0'
docker compose up -d --build --scale worker=2 --wait
```

Reuse `$versions`, `$toolId`, `$headers` and `$rootUrl` from the text-counter walkthrough above. Publish a separate version for the local model, submit a request and inspect its persisted steps.

```powershell
$config = @{
  instructions = 'You are a tool calling assistant. For a length request, call text_length with the string exactly as provided. Once you receive a tool response, return its length as a single number.'
  provider = 'openai-compatible'
  model = 'qwen3:0.6b'
  tool_version_ids = @($toolId)
  limits = @{ max_steps = 3; max_tokens = 8000; timeout_seconds = 300 }
} | ConvertTo-Json -Depth 4
$v = Invoke-RestMethod -Method Post -Uri $versions -Headers $headers -ContentType application/json -Body $config
Invoke-RestMethod -Method Post -Uri "$versions/$($v.id)/publish" -Headers $headers
$submit = $headers.Clone()
$submit['Idempotency-Key'] = [guid]::NewGuid().ToString()
$body = @{ agent_version_id = $v.id; input = @{ request = 'Use the text_length tool to count the characters in strawberry. /no_think' } } | ConvertTo-Json
$run = Invoke-RestMethod -Method Post -Uri "$rootUrl/runs" -Headers $submit -ContentType application/json -Body $body
$url = "$rootUrl/runs/$($run.id)"
Invoke-RestMethod -Uri $url -Headers $headers
Invoke-RestMethod -Uri "$url/steps" -Headers $headers | ConvertTo-Json -Depth 6
(Invoke-RestMethod -Uri "$url/usage" -Headers $headers).items | Format-Table model,status,input_tokens,output_tokens
```

Refresh the last three commands after the run finishes. A successful tool check has a model proposal with `text = "strawberry"`, a completed tool step with `length = 10`, and a final model answer of `10`. Both model invocations should have reported token counts. A model can also answer directly, so inspect the middle step when verifying tool use.

## Limits and failure records

Each model or tool action consumes one logical step. Before provider I/O the worker commits the step intent, invocation number, token reservation and price snapshot. It releases the database transaction while awaiting the network. Completed steps rebuild the conversation after a handoff. An outstanding invocation whose outcome was never recorded fails with `model_outcome_unknown`.

Prompt reservation uses serialized UTF-8 bytes plus 128 units for framing. This estimates input tokens; actual tokenization and hidden provider overhead can differ. Output is capped before dispatch. Reported usage above a reservation is persisted and ends the run with `provider_usage_overrun`. Estimated USD costs use the saved invocation rates.

| Invocation status | Budget accounting |
| --- | --- |
| reserved | The full reservation counts against the run before dispatch. |
| reported | Actual input and output counts replace the reservation, including when action validation fails. |
| rejected | A definitive rejection or rate limit releases the reservation. |
| unknown | A timeout, ambiguous failure or interrupted invocation retains its full reservation. Actual usage remains unknown. |

Transient failures allow up to three invocations per logical step with exponential backoff, jitter and Retry-After handling. Every retry requires a fresh reservation. Inspect `failure_reason` on the run and `/usage` for normalized errors, request IDs and latency. During backoff the run exposes its database retry time and releases its worker slot. A replacement worker can restore the persisted prompt and reservation history. [Checkpoint recovery](worker-failure-runbook.md#verify-checkpoint-recovery)
