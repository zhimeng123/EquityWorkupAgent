from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_KEY_ENV = "DEEPSEEK_API_KEY"
BASE_URL_ENV = "LLM_BASE_URL"
MODEL_ENV = "LLM_MODEL"
MAX_DESCRIPTION_WORDS = 30
LLM_TIMEOUT_ENV = "LLM_TIMEOUT_SECONDS"
LLM_MAX_RETRIES_ENV = "LLM_MAX_RETRIES"
DEFAULT_LLM_TIMEOUT_SECONDS = 300.0
DEFAULT_LLM_MAX_RETRIES = 1


def _load_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def _required_setting(name: str) -> str:
    _load_env()
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set")
    return value


def get_llm_api_key() -> str:
    return _required_setting(API_KEY_ENV)


def get_llm_base_url() -> str:
    return _required_setting(BASE_URL_ENV)


def get_llm_model() -> str:
    return _required_setting(MODEL_ENV)


def get_llm_timeout() -> float:
    _load_env()
    raw = os.getenv(LLM_TIMEOUT_ENV, "").strip()
    return float(raw) if raw else DEFAULT_LLM_TIMEOUT_SECONDS


def get_llm_max_retries() -> int:
    _load_env()
    raw = os.getenv(LLM_MAX_RETRIES_ENV, "").strip()
    return int(raw) if raw else DEFAULT_LLM_MAX_RETRIES


def chat_completion_text(llm_client, *, model, messages, temperature=0.0) -> str:
    """Return completion text, streaming so gateways do not cut long responses at 60s."""
    result = llm_client.chat.completions.create(
        model=model, messages=messages, temperature=temperature, stream=True
    )
    # Test doubles and some compatible servers return a complete response object.
    if hasattr(result, "choices"):
        content = result.choices[0].message.content
        return content or ""
    parts: list[str] = []
    for event in result:
        choices = getattr(event, "choices", None)
        if not choices:
            continue
        piece = getattr(choices[0].delta, "content", None)
        if piece:
            parts.append(piece)
    return "".join(parts)


def _limit_words(text: str, maximum: int) -> str:
    normalized = " ".join(text.strip().split())
    words = normalized.split()
    if len(words) <= maximum:
        return normalized
    return " ".join(words[:maximum]).rstrip(",;:") + "."


def summarize_business_description(
    api_key: str,
    source_text: str,
    *,
    company_name: str,
    founded_date: str | None,
    listing_date: str | None,
    main_business: str | None,
) -> str:
    client = OpenAI(api_key=api_key, base_url=get_llm_base_url(), timeout=get_llm_timeout())
    facts = (
        f"Company: {company_name}\n"
        f"Founded date: {founded_date or 'not provided'}\n"
        f"Listing date: {listing_date or 'not provided'}\n"
        f"Main business: {main_business or 'not provided'}"
    )
    content = chat_completion_text(
        client,
        model=get_llm_model(),
        messages=[
            {
                "role": "system",
                "content": (
                    "Write a factual English business description of at most 30 words. "
                    "Prioritize founding date, listing date, principal business, and core products or services. "
                    "Use only the supplied verified facts and source text. Do not infer, embellish, "
                    "or add claims. Omit any fact that is not supplied. Return only the paragraph."
                ),
            },
            {"role": "user", "content": f"Verified facts:\n{facts}\n\nSource text:\n{source_text}"},
        ],
        temperature=0,
    )
    if not content or not content.strip():
        raise ValueError("LLM returned an empty business description.")
    return _limit_words(content, MAX_DESCRIPTION_WORDS)
