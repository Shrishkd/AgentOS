import { expect, test } from "@playwright/test";

test.use({
  baseURL: process.env.AGENTOS_TEST_URL ?? "http://127.0.0.1:8080",
  channel: process.env.AGENTOS_BROWSER_CHANNEL,
});

test("shows live checks, handles an unreachable API and recovers on refresh", async ({ page }) => {
  await page.goto("/system-health");
  await expect(page.getByRole("status")).toContainText("4 of 4 services passed");
  await expect(page.getByRole("listitem")).toHaveCount(4);

  const automatic = page.getByRole("button", { name: "Auto-refresh every 10 seconds" });
  await automatic.click();
  await expect(automatic).toHaveAttribute("aria-pressed", "false");
  await page.route("**/health/ready", (route) => route.abort());
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByRole("listitem").filter({ hasText: "PostgreSQL" })).toContainText("Unknown");
  await expect(page.getByRole("status")).toContainText("2 of 4");
  await page.unroute("**/health/ready");
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("All services operational");
  await page.setViewportSize({ width: 375, height: 812 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
