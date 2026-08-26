"""Configuration loading: .env for secrets, profile.yml for everything about you."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from pipeline.models import Profile

DEFAULT_PROFILE_PATH = Path("config/profile.yml")
DEFAULT_DB_PATH = Path("data/pipeline.db")

# Providers that speak the OpenAI wire format. Anything with an OpenAI-compatible
# /chat/completions endpoint works here, which covers OpenRouter, Groq, Together,
# and a local Ollama server.
OPENAI_COMPATIBLE_DEFAULTS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "ollama": "http://localhost:11434/v1",
}

SUPPORTED_PROVIDERS = ("anthropic",) + tuple(OPENAI_COMPATIBLE_DEFAULTS)


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed. Always actionable."""


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: Optional[str]
    base_url: Optional[str]

    @property
    def is_anthropic(self) -> bool:
        return self.provider == "anthropic"


@dataclass(frozen=True)
class Settings:
    # None until an LLM is actually needed. `pipeline scrape`, `status`, and
    # `unblock` never call a model, and demanding an API key from someone who
    # only wants to see whether the tool finds real jobs is the fastest way to
    # lose them before they ever see it work.
    llm: Optional[LLMConfig]
    llm_error: Optional[str]
    db_path: Path
    profile_path: Path
    search_api_key: Optional[str] = None
    search_provider: Optional[str] = None
    browser_enabled: bool = False
    linkedin_enabled: bool = False

    def require_llm(self) -> LLMConfig:
        """The LLM config, or the configuration error that stopped it loading."""
        if self.llm is None:
            raise ConfigError(self.llm_error or "No LLM configured.")
        return self.llm


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader.

    Deliberately not python-dotenv: this is fifteen lines and drops a dependency
    that every user would otherwise have to install.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Real environment always wins over the file.
        os.environ.setdefault(key, value)


def _resolve_llm() -> LLMConfig:
    provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(
            "LLM_PROVIDER is '{0}'. Supported: {1}.".format(
                provider, ", ".join(SUPPORTED_PROVIDERS)
            )
        )

    model = os.environ.get("LLM_MODEL", "").strip()
    if not model:
        raise ConfigError(
            "LLM_MODEL is not set. Example: LLM_MODEL=claude-sonnet-5 "
            "(anthropic) or LLM_MODEL=llama3.1 (ollama)."
        )

    api_key = os.environ.get("LLM_API_KEY", "").strip() or None
    base_url = os.environ.get("LLM_BASE_URL", "").strip() or None
    if base_url is None and provider in OPENAI_COMPATIBLE_DEFAULTS:
        base_url = OPENAI_COMPATIBLE_DEFAULTS[provider]

    # Ollama runs locally with no auth. Everything else needs a key, and failing
    # here beats a confusing 401 twenty seconds into a scan.
    if api_key is None and provider != "ollama":
        raise ConfigError(
            "LLM_API_KEY is not set for provider '{0}'. Put it in .env.".format(provider)
        )

    return LLMConfig(provider=provider, model=model, api_key=api_key, base_url=base_url)


def load_settings(profile_path: Optional[Path] = None) -> Settings:
    load_dotenv()
    search_key = (
        os.environ.get("BRAVE_API_KEY", "").strip()
        or os.environ.get("SERPER_API_KEY", "").strip()
        or None
    )
    search_provider = None
    if os.environ.get("BRAVE_API_KEY", "").strip():
        search_provider = "brave"
    elif os.environ.get("SERPER_API_KEY", "").strip():
        search_provider = "serper"

    llm: Optional[LLMConfig] = None
    llm_error: Optional[str] = None
    try:
        llm = _resolve_llm()
    except ConfigError as exc:
        llm_error = str(exc)

    return Settings(
        llm=llm,
        llm_error=llm_error,
        db_path=Path(os.environ.get("PIPELINE_DB", str(DEFAULT_DB_PATH))),
        profile_path=profile_path or DEFAULT_PROFILE_PATH,
        search_api_key=search_key,
        search_provider=search_provider,
        browser_enabled=_env_flag("PIPELINE_BROWSER"),
        linkedin_enabled=_env_flag("PIPELINE_LINKEDIN"),
    )


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _require(data: Dict[str, Any], key: str, where: str) -> Any:
    if key not in data or data[key] in (None, ""):
        raise ConfigError("profile.yml is missing '{0}' under {1}.".format(key, where))
    return data[key]


def load_profile(path: Path = DEFAULT_PROFILE_PATH) -> Profile:
    """Read and validate profile.yml into an immutable Profile."""
    if not path.is_file():
        raise ConfigError(
            "No profile at {0}. Run `pipeline init` to create one.".format(path)
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError("profile.yml is not valid YAML: {0}".format(exc)) from exc

    if not isinstance(raw, dict):
        raise ConfigError("profile.yml must be a mapping at the top level.")

    candidate = raw.get("candidate") or {}
    if not isinstance(candidate, dict):
        raise ConfigError("profile.yml 'candidate' must be a mapping.")

    name = str(_require(candidate, "full_name", "candidate")).strip()
    email = str(_require(candidate, "email", "candidate")).strip()

    targets = raw.get("target_roles") or {}
    primary = targets.get("primary") or []
    if not isinstance(primary, list) or not primary:
        raise ConfigError(
            "profile.yml needs at least one entry under target_roles.primary."
        )

    prefs = raw.get("preferences") or {}
    links = {
        k: str(v).strip()
        for k, v in candidate.items()
        if k in ("linkedin", "github", "portfolio_url", "twitter") and v
    }

    return Profile(
        raw=raw,
        name=name,
        email=email,
        phone=_opt(candidate.get("phone")),
        location=_opt(candidate.get("location")),
        links=links,
        target_titles=[str(t).strip() for t in primary],
        archetypes=list(targets.get("archetypes") or []),
        narrative=dict(raw.get("narrative") or {}),
        work_authorization=_opt(prefs.get("work_authorization")),
        requires_sponsorship=bool(prefs.get("requires_sponsorship", False)),
        min_score_to_generate=int(prefs.get("min_score_to_generate", 70)),
        resume_path=_opt(prefs.get("baseline_resume")),
    )


def _opt(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value).strip()


def profile_titles(profile: Profile) -> List[str]:
    """Every title worth searching for, primary first, deduped."""
    seen: List[str] = []
    for title in profile.target_titles:
        if title not in seen:
            seen.append(title)
    for arch in profile.archetypes:
        name = arch.get("name") if isinstance(arch, dict) else None
        if name and name not in seen:
            seen.append(str(name))
    return seen
