"""Client for the prompt-injection classifier service (services/guard).

Enabled when GUARD_URL is set. Used on: user requests and feedback (block), document chunks at
ingestion (drop flagged chunks), live tool results (replace flagged results). Works alongside the
deterministic rails in guardrails/: patterns catch known phrasings, the model generalises.

GUARD_FAIL_MODE: what to do when the guard is unreachable or slow (GUARD_TIMEOUT_SECONDS):
  closed (default in staging/production): treat content as unsafe (requests get 503, chunks and
                                          tool results are withheld)
  open   (default in local/test): allow, and log `guard_unavailable`
"""

import logging
import os

import httpx

import metrics
import settings

log = logging.getLogger("voyagent.guard")


class GuardUnavailable(Exception):
    pass


def enabled() -> bool:
    return bool(os.getenv("GUARD_URL", "").strip())


def fail_closed() -> bool:
    default = "closed" if settings.durable_required() else "open"
    return os.getenv("GUARD_FAIL_MODE", default).strip().lower() == "closed"


async def classify(texts: list[str]) -> list[bool]:
    """True per text if it looks like a prompt injection. Raises GuardUnavailable."""
    if not texts:
        return []
    url = os.environ["GUARD_URL"].rstrip("/") + "/v1/classify"
    headers = {"X-Internal-Token": os.getenv("GUARD_TOKEN", "")}
    timeout = float(os.getenv("GUARD_TIMEOUT_SECONDS", "5"))
    out: list[bool] = []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for i in range(0, len(texts), 32):  # service accepts up to 32 texts per call
                r = await client.post(url, json={"texts": texts[i : i + 32]}, headers=headers)
                r.raise_for_status()
                out.extend(bool(x["injection"]) for x in r.json()["results"])
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("guard_unavailable", extra={"error_type": type(e).__name__})
        metrics.guardrail_events.labels("guard_unavailable", "classifier").inc()
        raise GuardUnavailable(type(e).__name__) from None
    return out


async def is_injection(text: str, stage: str) -> bool | None:
    """Classify one text for `stage` (input|tool_output|document). None = guard disabled or
    unavailable in fail-open mode; True/False otherwise. Fail-closed unavailability -> True."""
    if not enabled():
        return None
    try:
        flagged = (await classify([text]))[0]
    except GuardUnavailable:
        return True if fail_closed() else None
    if flagged:
        log.warning("guardrail_classifier_flagged", extra={"stage": stage})
        metrics.guardrail_events.labels(f"{stage}_flagged", "classifier").inc()
    return flagged
