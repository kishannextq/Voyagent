import { expect, test } from "@playwright/test";

// Web built with NEXT_PUBLIC_AUTH_MODE=cognito against tools/mock-oidc; API in AUTH_MODE=cognito.

test("sign in with PKCE, plan with a bearer token, sign out", async ({ page }) => {
  const planCalls: boolean[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/api/v1/plan") && r.method() === "POST") planCalls.push(Boolean(r.headers()["authorization"]));
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText(/e2e-user@example\.test/)).toBeVisible();

  await page.locator("textarea").first().fill("4 day trip to Kyoto");
  await page.locator('button[type="submit"]').first().click();
  await expect(page.getByRole("button", { name: /Approve & finalize/ })).toBeVisible();
  expect(planCalls.length).toBeGreaterThan(0);
  expect(planCalls.every(Boolean)).toBe(true);

  await page.getByRole("button", { name: /Sign out/ }).click();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
});

test("the API refuses calls without a token", async ({ request }) => {
  const r = await request.get("http://127.0.0.1:8012/api/v1/plans");
  expect(r.status()).toBe(401);
});
