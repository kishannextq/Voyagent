import { defineConfig } from "@playwright/test";

/**
 * Servers are started by the caller (CI job or scripts/run-e2e.sh):
 *   api (mock mode)            :8011 (app), :8012 (Cognito mode)
 *   web, auth disabled         E2E_WEB_URL      (default http://127.0.0.1:3011)
 *   web, Cognito via mock-oidc E2E_WEB_AUTH_URL (default http://127.0.0.1:3012)
 *   mock OIDC provider         http://127.0.0.1:9100
 * PW_CHANNEL=msedge uses an installed Edge instead of Playwright's Chromium.
 */
const channel = process.env.PW_CHANNEL || undefined;

export default defineConfig({
  testDir: "./specs",
  timeout: 60_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  // reducedMotion: the app honours prefers-reduced-motion, so audits see final (not mid-animation) colours.
  use: { channel, reducedMotion: "reduce", trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    { name: "app", testMatch: /(plan|a11y)\.spec\.ts/, use: { baseURL: process.env.E2E_WEB_URL || "http://127.0.0.1:3011" } },
    { name: "auth", testMatch: /auth\.spec\.ts/, use: { baseURL: process.env.E2E_WEB_AUTH_URL || "http://127.0.0.1:3012" } },
  ],
});
