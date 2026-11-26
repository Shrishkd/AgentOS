import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

export interface Workspace {
  id: string;
  slug: string;
  role: string;
  concurrency_limit: number;
  pending_limit: number;
  submissions_per_minute: number;
}
export interface Session {
  tenant: string;
  token: string;
  key: string;
  workspace: Workspace;
}
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}
export interface Agent {
  id: string;
  name: string;
  description: string;
  created_at: string;
}
export interface Version {
  id: string;
  number: number;
  state: string;
  instructions: string;
  provider: string;
  model: string;
  limits: { max_steps: number; max_tokens: number; timeout_seconds: number; max_cost_usd: number | null };
  tool_version_ids: string[];
}
export interface Run {
  id: string;
  agent_version_id: string;
  status: string;
  priority: number;
  created_at: string;
  updated_at: string;
  input: unknown;
  output: unknown;
  failure_reason: string | null;
  retry_at: string | null;
}
export interface Usage {
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: string;
  unknown: number;
  reserved: number;
  unpriced: number;
}
export interface Tool {
  id: string;
  name: string;
  version: number;
  enabled: boolean;
}
export const states = [
  "CREATED",
  "QUEUED",
  "RUNNING",
  "WAITING_FOR_APPROVAL",
  "RETRY_WAIT",
  "CANCEL_REQUESTED",
  "SUCCEEDED",
  "FAILED",
  "TIMED_OUT",
  "CANCELLED",
];
export const canWrite = (s: Session) => ["developer", "admin"].includes(s.workspace.role);

export async function request<T>(
  s: Pick<Session, "tenant" | "token">,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", "Bearer " + s.token);
  if (init.body) headers.set("Content-Type", "application/json");
  const response = await fetch("/v1/tenants/" + encodeURIComponent(s.tenant) + path, {
    ...init,
    headers,
    cache: "no-store",
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.error?.message ?? `Request failed (HTTP ${response.status}).`);
  }
  return response.json();
}

export function useResource<T>(s: Session, path: string, interval: number | false = 10000) {
  return useQuery({
    queryKey: [s.key, path],
    queryFn: ({ signal }) => request<T>(s, path, { signal }),
    refetchInterval: interval,
    retry: false,
  });
}

export function usePages<T>(s: Session, path: string, interval = 10000) {
  return useInfiniteQuery({
    queryKey: [s.key, path, "pages"],
    initialPageParam: "",
    queryFn: ({ signal, pageParam }) =>
      request<Page<T>>(
        s,
        path +
          (path.includes("?") ? "&" : "?") +
          "limit=25" +
          (pageParam ? "&cursor=" + encodeURIComponent(pageParam) : ""),
        { signal },
      ),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    refetchInterval: interval,
    retry: false,
  });
}

export const short = (id: string) => id.slice(0, 8);
export const time = (v: string) =>
  new Date(v).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
export const number = (n: number) => n.toLocaleString();
export const money = (v: string | number) =>
  Number(v).toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 8 });
export function navigate(path: string) {
  history.pushState(null, "", path);
  dispatchEvent(new PopStateEvent("popstate"));
}
