import { expect, test } from "@playwright/test";

test.use({
  baseURL: process.env.AGENTOS_TEST_URL ?? "http://127.0.0.1:8080",
  channel: process.env.AGENTOS_BROWSER_CHANNEL,
});
test("configure an agent, execute it and inspect real tenant data", async ({ page }) => {
  test.skip(!process.env.AGENTOS_DASHBOARD_TOKEN, "Requires the disposable dashboard tenant.");
  test.setTimeout(120000);
  await page.goto("/");
  await page.getByLabel("Tenant ID").fill(process.env.AGENTOS_TEST_TENANT!);
  await page.getByLabel("Access token").fill(process.env.AGENTOS_DASHBOARD_TOKEN!);
  await page.getByRole("button", { name: "Connect workspace" }).click();
  await expect(page.getByText("No runs match this view.")).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Workspace" });
  await nav.getByRole("link", { name: "Agents", exact: true }).click();
  await page.getByRole("button", { name: "Create agent" }).click();
  await page.getByLabel("Name", { exact: true }).fill("Workspace smoke agent");
  await page.getByRole("button", { name: "Save agent" }).click();
  await expect(page.getByRole("heading", { name: "Workspace smoke agent" })).toBeVisible();
  await page.getByRole("button", { name: "New version" }).click();
  await page.getByLabel("Instructions").fill("Return the supplied input.");
  await page.getByRole("button", { name: "Save draft version" }).click();
  await page.getByRole("button", { name: "Publish version" }).click();
  await page.getByRole("button", { name: "Start run" }).click();
  await page.getByLabel("Run input (JSON)").fill('{"hello":"phase-eight"}');
  await page.getByRole("button", { name: "Submit run" }).click();
  await expect(page.getByRole("heading", { name: "Run details" })).toBeVisible();
  await expect(page.locator(".timeline")).toContainText("succeeded", { timeout: 40000 });
  await page.getByRole("button", { name: "Steps & checkpoints" }).click();
  await expect(page.getByRole("cell", { name: "completed", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Calls & attempts" }).click();
  await expect(page.getByRole("cell", { name: "reported", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Input & output" }).click();
  await expect(
    page.locator(".panel").filter({ has: page.getByRole("heading", { name: "Final output" }) }),
  ).toContainText("phase-eight");
  const id = new URL(page.url()).pathname.split("/").pop()!;
  const tenantApi = "/v1/tenants/" + process.env.AGENTOS_TEST_TENANT;
  async function read(path: string) {
    const response = await page.request.get(tenantApi + path, {
      headers: { Authorization: "Bearer " + process.env.AGENTOS_DASHBOARD_TOKEN },
    });
    expect(response.status()).toBe(200);
    return response.json();
  }
  const [overview, analytics, invocations] = await Promise.all([
    read("/overview"),
    read("/analytics?days=7"),
    read("/runs/" + id + "/usage"),
  ]);
  expect(overview).toMatchObject({ total: 1, succeeded: 1, failed: 0, queued: 0 });
  expect(analytics.models).toHaveLength(1);
  expect(analytics.models[0]).toMatchObject({
    input_tokens: invocations.items[0].input_tokens,
    output_tokens: invocations.items[0].output_tokens,
  });
  expect(analytics.days[0]).toMatchObject({ runs: 1, finished: 1, failures: 0 });
  await nav.getByRole("link", { name: "Runs", exact: true }).click();
  await page.getByLabel("Run ID or agent name").fill("no matching agent");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByText("No runs match this view.")).toBeVisible();
  await page.getByLabel("Run ID or agent name").fill("Workspace smoke");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByRole("link", { name: id.slice(0, 8) })).toBeVisible();
  await nav.getByRole("link", { name: "Workers", exact: true }).click();
  await expect(page.getByRole("cell", { name: "healthy", exact: true })).toBeVisible();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    "**/analytics?days=7",
    async (route) => {
      await gate;
      await route.continue();
    },
    { times: 1 },
  );
  await nav.getByRole("link", { name: "Usage & analytics" }).click();
  try {
    await expect(page.getByRole("status")).toContainText("Loading");
  } finally {
    release();
  }
  await expect(page.getByRole("cell", { name: "deterministic", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Workspace smoke agent" })).toBeVisible();
  await page.getByLabel("Period").selectOption("30");
  await expect(page.getByRole("cell", { name: "deterministic", exact: true })).toBeVisible();
  await nav.getByRole("link", { name: "Settings" }).click();
  await page.getByLabel("Workspace theme").selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.setViewportSize({ width: 375, height: 812 });
  await nav.getByRole("link", { name: "Overview" }).click();
  await expect(page.getByRole("link", { name: id.slice(0, 8) })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.route("**/v1/tenants/*/overview", (r) =>
    r.fulfill({
      status: 503,
      contentType: "application/json",
      body: '{"error":{"message":"Overview temporarily unavailable."}}',
    }),
  );
  await nav.getByRole("link", { name: "Settings" }).click();
  await nav.getByRole("link", { name: "Overview" }).click();
  await expect(page.getByRole("alert")).toContainText("Overview temporarily unavailable.");
  await page.unroute("**/v1/tenants/*/overview");
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.getByRole("button", { name: "Disconnect" }).click();
  await expect(page.getByLabel("Access token")).toBeEmpty();
  await page.reload();
  await expect(page.getByLabel("Access token")).toBeEmpty();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
});
