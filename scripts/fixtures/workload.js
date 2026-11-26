import http from "k6/http";
import exec from "k6/execution";

const cases = {
  sequential: { vus: 1, iterations: 100 },
  burst: { vus: 100, iterations: 1 },
  long: { vus: 10, iterations: 1 },
  timeout: { vus: 10, iterations: 1 },
};
const name = __ENV.WORKLOAD;
if (!cases[name]) throw new Error("Select a configured benchmark workload");
export const options = {
  scenarios: { submissions: { executor: "per-vu-iterations", ...cases[name], maxDuration: "3m" } },
};

export default function () {
  const index = exec.scenario.iterationInTest;
  const input = { index, mode: name };
  const response = http.post("http://dashboard:8080/v1/tenants/" + __ENV.TENANT + "/runs",
    JSON.stringify({ agent_version_id: __ENV.VERSION, input }), {
      headers: {
        Authorization: "Bearer " + __ENV.BENCH_TOKEN,
        "Content-Type": "application/json",
        "Idempotency-Key": name + "-" + index,
      },
      redirects: 0, timeout: "60s",
    });
  console.log("AGENTOS_SAMPLE " + JSON.stringify({ index, status: response.status, ms: response.timings.duration,
    error_code: response.error_code || null }));
}

export function handleSummary() { return {}; }
