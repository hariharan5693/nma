"""Environment-driven configuration for the nma agent.

All values are read from environment variables (loaded from `.env` locally,
injected by the LangSmith deployment in production). Nothing secret is hard-coded.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (nma/) if present. In the deployed image the
# platform injects env vars directly, so a missing .env is fine.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=False)


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """Resolved runtime settings. Instantiate via `get_settings()`."""

    # ── Paths ──
    project_root: Path = _PROJECT_ROOT
    template_path: Path = _PROJECT_ROOT / "templates" / "global_template.xlsx"
    outputs_dir: Path = _PROJECT_ROOT / "outputs"

    def __init__(self) -> None:
        # ── LiteLLM (OpenAI-compatible /v1) ──
        self.openai_api_key: str = os.environ.get("OPENAI_API_KEY", "")
        self.openai_base_url: str = os.environ.get(
            "OPENAI_BASE_URL",
            "https://litellm.aiplat-dev.aws.private-pmideep.biz/v1",
        ).rstrip("/")

        # ── Model ──
        # Primary (narrative / mapping quality) and a cheaper 'fast' model used for
        # the simpler classify + catalog-enrich steps to cut token cost.
        self.model: str = os.environ.get(
            "NMA_LLM_MODEL", "bedrock/us.anthropic.claude-sonnet-4-6"
        )
        self.fast_model: str = os.environ.get(
            "NMA_FAST_LLM_MODEL", "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
        )
        self.max_tokens: int = int(os.environ.get("NMA_LLM_MAX_TOKENS", "8192"))
        self.temperature: float = float(os.environ.get("NMA_LLM_TEMPERATURE", "0"))

        # ── LangSmith (tracing) ──
        self.langsmith_project: str = os.environ.get(
            "LANGSMITH_PROJECT", "new_market_onboarding"
        )
        self.langsmith_endpoint: str = os.environ.get("LANGSMITH_ENDPOINT", "")
        self.langsmith_api_key: str = os.environ.get("LANGSMITH_API_KEY", "")
        self.langsmith_tracing: bool = _as_bool(
            os.environ.get("LANGSMITH_TRACING"), default=False
        )

        # ── TLS: PMI internal certs often need verification disabled locally ──
        # Deployed inside the platform this should be True.
        self.verify_ssl: bool = _as_bool(os.environ.get("NMA_VERIFY_SSL"), default=True)

        # ── Mapping behaviour ──
        self.confidence_threshold: float = float(
            os.environ.get("NMA_CONFIDENCE_THRESHOLD", "0.80")
        )

        # ── Deploy identity (informational; used at registration time) ──
        self.tenant_name: str = os.environ.get("NMA_TENANT_NAME", "mmiana-dev")

    def validate_for_llm(self) -> None:
        """Raise a clear error if the LLM cannot be called."""
        if not self.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Fill it in nma/.env (your LiteLLM key)."
            )
        if not self.openai_base_url.endswith("/v1"):
            raise RuntimeError(
                f"OPENAI_BASE_URL must end with /v1 (OpenAI-compatible), got "
                f"'{self.openai_base_url}'. Do NOT use the /bedrock endpoint."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
