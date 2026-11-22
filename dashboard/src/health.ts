export type Status = "up" | "down" | "unknown";
export type Service = "api" | "postgresql" | "redis" | "jetstream";
export type Snapshot = Record<Service, Status>;

async function readProbe(path: string, signal: AbortSignal) {
  try {
    const response = await fetch(path, {
      cache: "no-store",
      signal: AbortSignal.any([signal, AbortSignal.timeout(12000)]),
    });
    if (!response.headers.get("content-type")?.includes("application/json")) return null;
    const body = await response.json();
    return { ok: response.ok, body };
  } catch (error) {
    if (signal.aborted) throw error;
    return null;
  }
}

function dependency(value: unknown): Status {
  return value === "up" || value === "down" ? value : "unknown";
}

export async function loadHealth(signal: AbortSignal): Promise<Snapshot> {
  const [live, ready, jetstream] = await Promise.all(
    ["/health/live", "/health/ready", "/health/jetstream"].map((path) => readProbe(path, signal)),
  );
  return {
    api: live?.ok && live.body?.status === "alive" ? "up" : "down",
    postgresql: dependency(ready?.body?.checks?.postgresql),
    redis: dependency(ready?.body?.checks?.redis),
    jetstream: jetstream?.ok && jetstream.body?.status === "ok" ? "up" : "down",
  };
}
