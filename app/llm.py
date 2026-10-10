"""Direct HTTP Gemini client with multi-step text concatenation, caching, and health probes."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional

import httpx

from app.config import (
    GEMINI_API_KEY,
    GEMINI_INTERACTIONS_URL,
    GEMINI_MODEL,
    LLM_CACHE_TTL,
)

log = logging.getLogger(__name__)

# Answer cache: sha256_hash -> {"answer": str, "timestamp": float}
_ANSWER_CACHE: Dict[str, Dict[str, Any]] = {}
_LAST_LLM_STATUS: Dict[str, Any] = {"status": "unknown", "timestamp": 0}


class LLMError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, error_type: str = "llm_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type


def get_answer_cache(cache_key: str) -> Optional[str]:
    entry = _ANSWER_CACHE.get(cache_key)
    if entry and (time.time() - entry["timestamp"]) < LLM_CACHE_TTL:
        return entry["answer"]
    return None


def set_answer_cache(cache_key: str, answer: str) -> None:
    _ANSWER_CACHE[cache_key] = {"answer": answer, "timestamp": time.time()}


def compute_cache_key(query: str, intent: str, data_fingerprint: str) -> str:
    h = hashlib.sha256()
    h.update(f"{intent}|{query.strip().lower()}|{data_fingerprint}".encode("utf-8"))
    return h.hexdigest()


async def ask_gemini(
    prompt: str,
    system_instruction: str,
    cache_key: Optional[str] = None,
    model_name: Optional[str] = None,
    timeout: float = 45.0,
) -> str:
    """
    Calls Gemini Interactions API directly.
    Concatenates all model output text fragments to prevent truncation.
    """
    global _LAST_LLM_STATUS

    if cache_key:
        cached = get_answer_cache(cache_key)
        if cached:
            log.info(f"LLM Answer cache hit for key {cache_key[:8]}")
            return cached

    api_key = GEMINI_API_KEY
    if not api_key or api_key == "test-mock-key":
        raise LLMError("Gemini API key is not configured.", error_type="config_error")

    model = model_name or GEMINI_MODEL
    url = GEMINI_INTERACTIONS_URL

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json={
                    "model": model,
                    "input": prompt,
                    "system_instruction": system_instruction,
                    "store": False,
                },
            )

            if resp.status_code == 429:
                _LAST_LLM_STATUS = {"status": "rate_limited", "timestamp": time.time()}
                raise LLMError(f"Gemini API rate limit exceeded (429) for model {model}.", status_code=429, error_type="rate_limit")

            if resp.status_code != 200:
                err_text = resp.text[:300]
                _LAST_LLM_STATUS = {"status": "error", "error": err_text, "timestamp": time.time()}
                raise LLMError(f"Gemini API HTTP {resp.status_code}: {err_text}", status_code=resp.status_code, error_type="api_error")

            data = resp.json()
            _LAST_LLM_STATUS = {"status": "ok", "timestamp": time.time()}
            log.info(f"Gemini {model} responded in {time.time() - t0:.2f}s")

            # Extract ALL text content pieces across all steps
            text_chunks = []
            if "steps" in data:
                for step in data["steps"]:
                    if step.get("type") == "model_output" and "content" in step:
                        for c in step["content"]:
                            if isinstance(c, dict) and c.get("type") == "text" and "text" in c:
                                text_chunks.append(c["text"])

            if text_chunks:
                full_text = "".join(text_chunks)
                if cache_key:
                    set_answer_cache(cache_key, full_text)
                return full_text

            # Fallbacks for standard formats
            if "outputText" in data:
                res = data["outputText"]
            elif "output_text" in data:
                res = data["output_text"]
            elif "text" in data:
                res = data["text"]
            elif "interaction" in data and "outputText" in data["interaction"]:
                res = data["interaction"]["outputText"]
            else:
                res = ""

            if not res:
                raise LLMError("Gemini response contained no text output.", error_type="empty_output")

            if cache_key:
                set_answer_cache(cache_key, res)
            return res

    except httpx.TimeoutException:
        _LAST_LLM_STATUS = {"status": "timeout", "timestamp": time.time()}
        raise LLMError(f"Gemini request timed out after {timeout}s.", error_type="timeout")
    except Exception as e:
        if isinstance(e, LLMError):
            raise
        _LAST_LLM_STATUS = {"status": "error", "error": str(e), "timestamp": time.time()}
        raise LLMError(f"LLM interaction failure: {str(e)}", error_type="network_error")


async def probe_gemini_health() -> Dict[str, Any]:
    """
    Performs a lightweight, quota-free GET request to verify Gemini API key validity.
    """
    api_key = GEMINI_API_KEY
    if not api_key or api_key == "test-mock-key":
        return {
            "status": "degraded",
            "model": GEMINI_MODEL,
            "detail": "GEMINI_API_KEY is not set or using mock key",
        }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers={"x-goog-api-key": api_key})
            if resp.status_code == 200:
                # Check recent status
                if _LAST_LLM_STATUS.get("status") == "rate_limited" and (time.time() - _LAST_LLM_STATUS.get("timestamp", 0)) < 300:
                    return {
                        "status": "degraded",
                        "model": GEMINI_MODEL,
                        "detail": "Rate limit encountered in last 5 minutes",
                    }
                return {
                    "status": "ok",
                    "model": GEMINI_MODEL,
                    "detail": "Model endpoint reachable and authorized",
                }
            elif resp.status_code == 400 or resp.status_code == 403:
                return {
                    "status": "error",
                    "model": GEMINI_MODEL,
                    "detail": f"Gemini API key rejected ({resp.status_code})",
                }
            else:
                return {
                    "status": "degraded",
                    "model": GEMINI_MODEL,
                    "detail": f"HTTP {resp.status_code}",
                }
    except Exception as e:
        return {
            "status": "degraded",
            "model": GEMINI_MODEL,
            "detail": f"Probe error: {str(e)}",
        }
