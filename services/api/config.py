import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
AVIATION_STACK_API_KEY = os.getenv("AVIATION_STACK_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# Model gateway (LiteLLM, services/model-gateway). When set, every model call goes through it:
# routing, retries, fallbacks, caching, budgets and spend logs live there, not in this service.
LLM_GATEWAY_URL = os.getenv("LLM_GATEWAY_URL", "").strip().rstrip("/")

# Model aliases defined in the gateway config. "fast": guardrail + routing (short JSON);
# "smart": specialist analysis, itinerary, final plan.
MODEL_ALIASES = {
    "fast": os.getenv("LLM_MODEL_FAST", "voyagent-fast"),
    "smart": os.getenv("LLM_MODEL_SMART", "voyagent-smart"),
}
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))


def gateway_enabled() -> bool:
    return bool(LLM_GATEWAY_URL)


@lru_cache(maxsize=32)
def get_llm(api_key: str, role: str = "smart"):
    """Chat model for a key and role (one cached client per pair).

    Gateway: OpenAI-compatible client on the gateway with a virtual key; the gateway retries
    and falls back, so the client does not (max_retries=0) to avoid multiplying attempts.
    Direct: Groq (local development without the gateway).
    """
    if gateway_enabled():
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            base_url=f"{LLM_GATEWAY_URL}/v1",
            api_key=api_key,
            model=MODEL_ALIASES[role],
            timeout=LLM_TIMEOUT,
            max_retries=0,
        )
    from langchain_groq import ChatGroq

    return ChatGroq(
        model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"), api_key=api_key, timeout=LLM_TIMEOUT, max_retries=1
    )
