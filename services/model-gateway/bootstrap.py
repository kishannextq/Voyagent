"""One-time gateway setup through the LiteLLM admin API (stdlib only).

Creates the per-end-user budget referenced by config.yaml and a virtual key for the API,
limited to Voyagent's model aliases, with its own budget and rate limit. Prints the key once:
store it as LLM_GATEWAY_KEY (Secrets Manager in AWS); it is not shown again.

    GATEWAY_URL=http://localhost:4000 LITELLM_MASTER_KEY=... python bootstrap.py
Optional: END_USER_BUDGET_USD (default 2), END_USER_BUDGET_PERIOD (default 30d),
          API_KEY_BUDGET_USD (default 200), API_KEY_RPM (default 600).
"""

import json
import os
import sys
import urllib.error
import urllib.request


def call(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        os.environ.get("GATEWAY_URL", "http://localhost:4000").rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - admin URL from operator env
        return json.loads(r.read() or b"{}")


def main() -> int:
    if "LITELLM_MASTER_KEY" not in os.environ:
        print("LITELLM_MASTER_KEY is required", file=sys.stderr)
        return 2
    try:
        call(
            "/budget/new",
            {
                "budget_id": "default-end-user-budget",
                "max_budget": float(os.environ.get("END_USER_BUDGET_USD", "2")),
                "budget_duration": os.environ.get("END_USER_BUDGET_PERIOD", "30d"),
            },
        )
    except urllib.error.HTTPError as e:
        if e.code not in (400, 409):  # already exists
            raise
    key = call(
        "/key/generate",
        {
            "key_alias": "voyagent-api",
            "models": ["voyagent-fast", "voyagent-smart"],
            "max_budget": float(os.environ.get("API_KEY_BUDGET_USD", "200")),
            "budget_duration": "30d",
            "rpm_limit": int(os.environ.get("API_KEY_RPM", "600")),
        },
    )["key"]
    print(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
