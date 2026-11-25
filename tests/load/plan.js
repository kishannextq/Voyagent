// Load test (k6). Measures the platform, not model latency: run against MOCK_MODE=true with a
// fixed per-call delay (MOCK_DELAY) standing in for model/tool time, and rate limits disabled
// (they are tested separately). Results land in summary.json and the CI job summary.
//
//   docker run --rm --network host -v "$PWD/tests/load:/scripts" grafana/k6:2.3.0 run /scripts/plan.js
import http from "k6/http";
import { check } from "k6";
import { Trend, Rate } from "k6/metrics";

const BASE = __ENV.BASE_URL || "http://127.0.0.1:8000";
const planDuration = new Trend("plan_duration", true);
const approveDuration = new Trend("approve_duration", true);
const planErrors = new Rate("plan_errors");

export const options = {
  scenarios: {
    // Read path: health + plan listing at a steady rate.
    reads: {
      executor: "constant-arrival-rate",
      rate: 50,
      timeUnit: "1s",
      duration: "1m",
      preAllocatedVUs: 20,
      exec: "reads",
    },
    // Full plan + approval, ramping concurrent users.
    plans: {
      executor: "ramping-vus",
      startVUs: 1,
      stages: [
        { duration: "20s", target: 10 },
        { duration: "40s", target: 25 },
        { duration: "20s", target: 0 },
      ],
      exec: "plans",
    },
  },
  thresholds: {
    "http_req_failed{scenario:reads}": ["rate<0.01"],
    "http_req_duration{scenario:reads}": ["p(95)<300"],
    plan_errors: ["rate<0.02"],
    // Mock mode: ~10 model/tool calls x MOCK_DELAY plus platform overhead per plan.
    plan_duration: ["p(95)<8000"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

const json = { headers: { "Content-Type": "application/json" }, timeout: "60s" };

export function reads() {
  const r1 = http.get(`${BASE}/api/v1/health`);
  check(r1, { "health 200": (r) => r.status === 200 });
  const r2 = http.get(`${BASE}/api/v1/plans?limit=5`);
  check(r2, { "plans 200": (r) => r.status === 200 });
}

export function plans() {
  const r = http.post(`${BASE}/api/v1/plan`, JSON.stringify({ query: "4 day trip to Kyoto with a mid-range budget" }), json);
  planDuration.add(r.timings.duration);
  const ok = check(r, { "plan awaiting approval": (res) => res.status === 200 && res.json("status") === "awaiting_approval" });
  planErrors.add(!ok);
  if (!ok) return;
  const a = http.post(`${BASE}/api/v1/plan/${r.json("thread_id")}/approve`, JSON.stringify({ approved: true }), json);
  approveDuration.add(a.timings.duration);
  planErrors.add(!check(a, { "approved": (res) => res.status === 200 && res.json("status") === "completed" }));
}

export function handleSummary(data) {
  return { "/scripts/summary.json": JSON.stringify(data, null, 2), stdout: textSummary(data) };
}

function textSummary(data) {
  const m = data.metrics;
  const f = (name, stat) => (m[name] && m[name].values[stat] !== undefined ? m[name].values[stat].toFixed(1) : "n/a");
  return [
    "",
    `plans completed:        ${m.plan_duration ? m.plan_duration.values.count : 0}`,
    `plan p50/p95/max (ms):  ${f("plan_duration", "med")} / ${f("plan_duration", "p(95)")} / ${f("plan_duration", "max")}`,
    `approve p95 (ms):       ${f("approve_duration", "p(95)")}`,
    `plan error rate:        ${f("plan_errors", "rate")}`,
    `http reqs/s (total):    ${f("http_reqs", "rate")}`,
    `read p95 (ms):          ${m["http_req_duration{scenario:reads}"] ? m["http_req_duration{scenario:reads}"].values["p(95)"].toFixed(1) : "n/a"}`,
    "",
  ].join("\n");
}
