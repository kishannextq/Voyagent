import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// WCAG 2.1 A/AA rules. Serious and critical violations fail the test; moderate/minor ones are
// reported in the output so they are visible without blocking.
async function audit(page: Page, label: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const blocking = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  for (const v of results.violations) {
    console.log(`[a11y:${label}] ${v.impact} ${v.id}: ${v.help} (${v.nodes.length} nodes)`);
    for (const n of v.nodes) console.log(`    ${n.target.join(" ")} :: ${(n.failureSummary || "").split("\n").slice(1).join(" ")}`);
  }
  expect(blocking.map((v) => `${v.id} (${v.nodes.length})`), `${label}: serious/critical a11y violations`).toEqual([]);
}

test("home page has no serious accessibility violations", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("API online")).toBeVisible();
  await audit(page, "home");
});

test("review and final plan views have no serious accessibility violations", async ({ page }) => {
  await page.goto("/");
  await page.locator("textarea").first().fill("4 day trip to Kyoto");
  await page.locator('button[type="submit"]').first().click();
  const approve = page.getByRole("button", { name: /Approve & finalize/ });
  await expect(approve).toBeVisible();
  await audit(page, "review");
  await approve.click();
  await expect(page.getByText(/approved/i).first()).toBeVisible();
  await audit(page, "final");
});
