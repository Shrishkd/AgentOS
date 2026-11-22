import { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import * as Toggle from "@radix-ui/react-toggle";
import { Activity, AudioLines, BookOpen, Boxes, Cable, Check, CircleHelp, Database, Layers3, Pause, Play, RefreshCw, Server, TriangleAlert } from "lucide-react";
import { loadHealth, type Service, type Status } from "./health";
import "@fontsource-variable/inter/wght.css";
import "./style.css";

const services = [
  { key: "api", name: "Control plane", detail: "FastAPI process liveness", icon: Server },
  { key: "postgresql", name: "PostgreSQL", detail: "Connection and schema revision", icon: Database },
  { key: "redis", name: "Redis", detail: "Cache connectivity", icon: Layers3 },
  { key: "jetstream", name: "NATS JetStream", detail: "Message broker availability", icon: Cable },
] as const;

function HealthDashboard() {
  const [automatic, setAutomatic] = useState(true);
  const { data, dataUpdatedAt, isFetching, refetch } = useQuery({
    queryKey: ["system-health"],
    queryFn: ({ signal }) => loadHealth(signal),
    refetchInterval: automatic ? 10000 : false,
    refetchOnWindowFocus: automatic,
    retry: false,
  });
  const healthy = data ? Object.values(data).filter((status) => status === "up").length : 0;
  const overall = !data ? "Checking services" : healthy === services.length ? "All services operational" : "Service attention required";
  const BannerIcon = !data ? Activity : healthy === services.length ? Check : TriangleAlert;
  const labels: Record<Status, string> = { up: "Healthy", down: "Unavailable", unknown: "Unknown" };
  function statusOf(key: Service) { return data?.[key]; }

  return <div className="shell">
    <aside>
      <a className="brand" href="/" aria-label="AgentOS home"><Boxes size={27} /> AgentOS</a>
      <nav aria-label="Workspace"><a className="selected" href="/" aria-current="page"><Activity /> System health</a></nav>
      <a className="api-link" href="/docs"><BookOpen /> API reference</a>
    </aside>
    <main>
      <header><span>Workspace</span><span aria-hidden="true">/</span> System health</header>
      <section className="content">
        <div className="heading flex flex-wrap items-center justify-between gap-4">
          <h1>System health</h1>
          <button className="refresh" disabled={isFetching} onClick={() => void refetch()}>
            <RefreshCw size={16} className={isFetching ? "spin" : ""} /> Refresh
          </button>
        </div>
        <div role="status" className={`summary ${!data ? "" : healthy === services.length ? "good" : "warn"}`}>
          <span className="summary-icon"><BannerIcon size={24} /></span>
          <div><h2>{overall}</h2><p>{data ? `${healthy} of ${services.length} services passed their health checks.` : "Waiting for the first health check."}</p></div>
        </div>
        <section className="panel" aria-labelledby="services-title">
          <div className="panel-head flex items-center justify-between gap-3">
            <h2 id="services-title">Services</h2>
            <Toggle.Root className="auto" pressed={automatic} onPressedChange={setAutomatic} aria-label="Auto-refresh every 10 seconds">
              {automatic ? <Pause size={14} /> : <Play size={14} />}
              {automatic ? "Auto-refresh on" : "Auto-refresh off"}
            </Toggle.Root>
          </div>
          <ul>{services.map(({key, name, detail, icon: Icon}) => {
            const status = statusOf(key);
            return <li key={key}>
              <span className="service-icon"><Icon size={19} /></span>
              <div className="service-name"><strong>{name}</strong><p>{detail}</p></div>
              <span className={`pill ${status ?? "unknown"}`}>
                {status === "unknown" ? <CircleHelp size={13} /> : <i aria-hidden="true" />}
                {status ? labels[status] : "Checking"}
              </span>
            </li>;
          })}</ul>
        </section>
        <footer><AudioLines size={16} /> {dataUpdatedAt ? `Last checked at ${new Date(dataUpdatedAt).toLocaleTimeString()}` : "Checking now"}<span>·</span>{automatic ? "Every 10 seconds" : "Automatic checks paused"}</footer>
      </section>
    </main>
  </div>;
}

const client = new QueryClient();
createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}><HealthDashboard /></QueryClientProvider>,
);
