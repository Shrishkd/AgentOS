import { expect, test } from "@playwright/test";

test.use({ baseURL: process.env.AGENTOS_TEST_URL ?? "http://127.0.0.1:8080", channel: process.env.AGENTOS_BROWSER_CHANNEL });

test("approve the displayed report proposal", async ({ page }) => {
  test.skip(!process.env.AGENTOS_TEST_REVIEWER, "Needs a pending proposal in the disposable tenant.");
  await page.goto("/");
  await page.getByRole("link", { name: "Approvals" }).click();
  await page.getByLabel("Tenant ID").fill(process.env.AGENTOS_TEST_TENANT!);
  await page.getByLabel("Access token").fill(process.env.AGENTOS_TEST_REVIEWER!);
  await page.getByRole("button", { name: "Open inbox" }).click();
  await page.getByRole("button", { name: /save_report/ }).click();
  await expect(page.locator("pre")).toContainText("Queue drained.");
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("save_report approved.");
  await expect(page.getByText("No actions are waiting for approval.")).toBeVisible();
  await page.getByRole("combobox", { name: "State", exact: true }).selectOption("approved");
  await expect(page.getByRole("row").filter({ hasText: "save_report" }).first()).toContainText("approved");
  await page.getByRole("button", { name: "Disconnect" }).click();
  await expect(page.getByLabel("Access token")).toBeEmpty();
});
