import { useEffect, useRef, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Blocks,
  BookOpen,
  Boxes,
  ChartNoAxesCombined,
  ClipboardCheck,
  Cpu,
  LayoutDashboard,
  ListTree,
  LogOut,
  Moon,
  Plug,
  Settings2,
  Sun,
} from "lucide-react";
import Approvals from "./Approvals";
import Health from "./SystemHealth";
import { loadHealth } from "./health";
import { Agents, AgentDetail } from "./Agents";
import { Runs, RunDetail } from "./Runs";
import { Analytics, Overview, Workers } from "./Operations";
import { navigate, request, type Session, type Workspace } from "./api";
import { Heading, Panel, useAction } from "./ui";
import "@fontsource-variable/inter/wght.css";
import "./style.css";
import "./workspace.css";

const routes = [
  { path: "/", label: "Overview", icon: LayoutDashboard },
  { path: "/agents", label: "Agents", icon: Boxes },
  { path: "/runs", label: "Runs", icon: ListTree },
  { path: "/approvals", label: "Approvals", icon: ClipboardCheck },
  { path: "/workers", label: "Workers", icon: Cpu },
  { path: "/analytics", label: "Usage & analytics", icon: ChartNoAxesCombined },
  { path: "/system-health", label: "System health", icon: Activity },
  { path: "/settings", label: "Settings", icon: Settings2 },
];
type Theme = "system" | "light" | "dark";
function Connect({ onConnect, inbox }: { onConnect: (s: Session) => void; inbox: boolean }) {
  const action = useAction();
  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    const credentials = {
      tenant: String(data.get("tenant")).trim(),
      token: String(data.get("token")).trim(),
    };
    void action.perform(async () => {
      const workspace = await request<Workspace>(credentials, "/workspace");
      onConnect({ ...credentials, key: crypto.randomUUID(), workspace });
    });
  }
  return (
    <form className="panel connect" onSubmit={submit}>
      <span className="form-icon">
        <Plug size={22} />
      </span>
      <h2>{inbox ? "Open a tenant inbox" : "Connect to a workspace"}</h2>
      <p>Use a tenant ID and access token issued by your AgentOS operator.</p>
      <label>
        Tenant ID
        <input
          name="tenant"
          required
          placeholder="Tenant UUID"
          autoComplete="off"
          pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        />
      </label>
      <label>
        Access token
        <input type="password" name="token" required autoComplete="off" />
      </label>
      {action.error}
      <button className="primary" disabled={action.busy}>
        {action.busy ? "Connecting…" : inbox ? "Open inbox" : "Connect workspace"}
      </button>
    </form>
  );
}

function ServiceSummary() {
  const health = useQuery({
    queryKey: ["system-health"],
    queryFn: ({ signal }) => loadHealth(signal),
    refetchInterval: 10000,
    retry: false,
  });
  const services = [
    ["api", "Control plane"],
    ["postgresql", "PostgreSQL"],
    ["redis", "Redis"],
    ["jetstream", "NATS JetStream"],
  ] as const;
  return (
    <Panel title="Service availability" action={<Activity size={18} />}>
      <dl className="readings">
        {services.map(([key, name]) => {
          const state = health.data?.[key];
          return (
            <div className="reading-pair" key={key}>
              <dt>{name}</dt>
              <dd>
                <span className={"state state-" + (state === "up" ? "healthy" : "stale")}>
                  {state === "up"
                    ? "Healthy"
                    : state === "down"
                      ? "Unavailable"
                      : state === "unknown"
                        ? "Unknown"
                        : "Checking…"}
                </span>
              </dd>
            </div>
          );
        })}
      </dl>
      <div className="service-link">
        <a className="text-link" href="/system-health">
          Inspect system health
        </a>
      </div>
    </Panel>
  );
}

function App() {
  const [path, setPath] = useState(location.pathname);
  const [session, setSession] = useState<Session | null>(null);
  const [theme, setTheme] = useState<Theme>(() => {
    const t = localStorage.getItem("agentos-theme");
    return t === "light" || t === "dark" ? t : "system";
  });
  const [systemDark, setSystemDark] = useState(matchMedia("(prefers-color-scheme: dark)").matches);
  const cache = useQueryClient(),
    content = useRef<HTMLElement>(null);
  useEffect(() => {
    const onRoute = () => {
      setPath(location.pathname);
      window.scrollTo(0, 0);
      content.current?.focus();
    };
    const media = matchMedia("(prefers-color-scheme: dark)");
    const sync = () => setSystemDark(media.matches);
    window.addEventListener("popstate", onRoute);
    media.addEventListener("change", sync);
    return () => {
      window.removeEventListener("popstate", onRoute);
      media.removeEventListener("change", sync);
    };
  }, []);
  const dark = theme === "dark" || (theme === "system" && systemDark);
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    localStorage.setItem("agentos-theme", theme);
  }, [theme, dark]);
  const parts = path.split("/").filter(Boolean),
    base = parts.length ? "/" + parts[0] : "/";
  const route = routes.find((r) => r.path === base);
  const detail =
    parts.length === 2 &&
    ["runs", "agents"].includes(parts[0]) &&
    /^[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}$/i.test(parts[1]);
  const valid = route && (parts.length < 2 || detail);
  const title = valid
    ? detail
      ? base === "/runs"
        ? "Run details"
        : "Agent configuration"
      : route.label
    : "Page unavailable";
  useEffect(() => {
    document.title = title + " · AgentOS";
  }, [title]);
  function disconnect() {
    if (session) cache.removeQueries({ queryKey: [session.key] });
    setSession(null);
  }
  let page;
  if (!valid)
    page = (
      <>
        <Heading title={title} />
        <a className="text-link" href="/">
          Return to overview
        </a>
      </>
    );
  else if (base === "/system-health") page = <Health />;
  else if (base === "/settings")
    page = (
      <>
        <Heading title="Settings" />
        <Panel title="Appearance">
          <div className="settings-row">
            <label htmlFor="theme">Workspace theme</label>
            <select id="theme" value={theme} onChange={(e) => setTheme(e.target.value as Theme)}>
              <option value="system">System preference</option>
              <option value="light">Light</option>
              <option value="dark">Dark</option>
            </select>
          </div>
        </Panel>
        {session ? (
          <Panel title="Workspace connection">
            <dl className="facts">
              <dt>Tenant</dt>
              <dd>{session.tenant}</dd>
              <dt>Role</dt>
              <dd>{session.workspace.role}</dd>
              <dt>Concurrent runs</dt>
              <dd>{session.workspace.concurrency_limit}</dd>
              <dt>Pending run limit</dt>
              <dd>{session.workspace.pending_limit}</dd>
              <dt>Submissions per minute</dt>
              <dd>{session.workspace.submissions_per_minute}</dd>
            </dl>
          </Panel>
        ) : (
          <Connect onConnect={setSession} inbox={false} />
        )}
      </>
    );
  else if (!session)
    page = (
      <>
        <Heading title={title} />
        <div className="welcome">
          <Connect onConnect={setSession} inbox={base === "/approvals"} />
          {base === "/" && <ServiceSummary />}
        </div>
      </>
    );
  else if (base === "/agents")
    page = detail ? <AgentDetail session={session} id={parts[1]} /> : <Agents session={session} />;
  else if (base === "/runs")
    page = detail ? <RunDetail session={session} id={parts[1]} /> : <Runs session={session} />;
  else if (base === "/workers") page = <Workers session={session} />;
  else if (base === "/analytics") page = <Analytics session={session} />;
  else if (base === "/approvals") page = <Approvals session={session} />;
  else page = <Overview session={session} />;
  return (
    <div
      className="shell"
      onClick={(e) => {
        const a = (e.target as HTMLElement).closest("a");
        if (
          !a ||
          e.defaultPrevented ||
          e.ctrlKey ||
          e.metaKey ||
          e.shiftKey ||
          e.altKey ||
          e.button !== 0 ||
          a.origin !== location.origin ||
          a.target ||
          a.hash ||
          a.pathname === "/docs"
        )
          return;
        e.preventDefault();
        navigate(a.pathname + a.search);
      }}
    >
      <a className="skip" href="#workspace">
        Skip to content
      </a>
      <aside>
        <a className="brand" href="/">
          <span className="brand-mark">
            <Blocks size={24} />
          </span>{" "}
          AgentOS
        </a>
        <nav aria-label="Workspace">
          {routes.map(({ path: p, label, icon: Icon }) => (
            <a
              href={p}
              key={p}
              aria-current={base === p ? "page" : undefined}
              className={base === p ? "selected" : ""}
            >
              <Icon />
              {label}
            </a>
          ))}
        </nav>
        <a className="api-link" href="/docs">
          <BookOpen />
          API reference
        </a>
      </aside>
      <main id="workspace" tabIndex={-1} ref={content}>
        <header>
          <span>Workspace</span>
          <span aria-hidden="true">/</span>
          <b>{title}</b>
          <div className="session-tools">
            {session && (
              <>
                <span className="workspace-name">{session.workspace.slug}</span>
                <button aria-label="Disconnect" title="Disconnect" onClick={disconnect}>
                  <LogOut size={17} />
                </button>
              </>
            )}
            <button
              aria-label={dark ? "Use light theme" : "Use dark theme"}
              onClick={() => setTheme(dark ? "light" : "dark")}
            >
              {dark ? <Sun size={18} /> : <Moon size={18} />}
            </button>
          </div>
        </header>
        <section className="content" key={(session?.key ?? "guest") + path}>
          {page}
        </section>
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={new QueryClient()}>
    <App />
  </QueryClientProvider>,
);
