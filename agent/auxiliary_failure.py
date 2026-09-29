"""Structured, lossless diagnostics for auxiliary LLM failures.

Provider SDK exceptions are deliberately heterogeneous: some expose ``body``,
some an HTTP ``response``, and Responses-API terminal failures arrive as normal
response objects.  This module gives those paths one JSON-safe diagnostic shape
without truncating the provider payload.
"""

from __future__ import annotations

import base64
import dataclasses
from typing import Any, Dict, Optional


_MISSING = object()


def json_safe(value: Any, *, _seen: Optional[set[int]] = None) -> Any:
    """Return a JSON-serialisable, untruncated representation of ``value``."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}

    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return "<recursive reference>"
    seen.add(identity)
    try:
        if isinstance(value, dict):
            return {
                str(key): json_safe(item, _seen=seen) for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [json_safe(item, _seen=seen) for item in value]
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return json_safe(dataclasses.asdict(value), _seen=seen)

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return json_safe(model_dump(mode="json"), _seen=seen)
            except TypeError:
                return json_safe(model_dump(), _seen=seen)

        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return json_safe(to_dict(), _seen=seen)

        values = getattr(value, "__dict__", None)
        if isinstance(values, dict):
            return json_safe(values, _seen=seen)
        return str(value)
    finally:
        seen.discard(identity)


def response_payload(response: Any) -> Any:
    """Full provider payload retained on a completion or Responses object."""
    retained = getattr(response, "_hermes_raw_response", _MISSING)
    return json_safe(response if retained is _MISSING else retained)


def exception_payload(exc: BaseException) -> Any:
    """Best available full provider response carried by ``exc``."""
    current = getattr(exc, "hermes_llm_failure", None)
    if isinstance(current, dict) and current.get("raw_response") is not None:
        return current["raw_response"]

    retained = getattr(exc, "raw_response", _MISSING)
    if retained is not _MISSING:
        return json_safe(retained)

    body = getattr(exc, "body", _MISSING)
    if body is not _MISSING and body is not None:
        return json_safe(body)

    http_response = getattr(exc, "response", None)
    if http_response is not None:
        json_method = getattr(http_response, "json", None)
        if callable(json_method):
            try:
                return json_safe(json_method())
            except Exception:
                pass
        text = getattr(http_response, "text", _MISSING)
        if text is not _MISSING:
            return json_safe(text)
    return str(exc)


def response_status(response: Any) -> tuple[str, str]:
    """Return ``(status, finish_reason)`` from Responses or chat shapes."""
    status = getattr(response, "_hermes_response_status", None) or getattr(
        response, "status", None
    )
    finish_reason = getattr(response, "_hermes_finish_reason", None)
    if finish_reason is None:
        try:
            finish_reason = response.choices[0].finish_reason
        except (AttributeError, IndexError, TypeError):
            finish_reason = None
    return str(status or ""), str(finish_reason or "")


def attach_llm_failure(
    exc: BaseException,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    agent_id: Optional[str] = None,
    status: Optional[str] = None,
    finish_reason: Optional[str] = None,
    raw_response: Any = _MISSING,
    validation_error: Any = None,
    audit: Optional[Dict[str, Any]] = None,
    usage: Any = None,
) -> Dict[str, Any]:
    """Attach/merge the canonical ``hermes_llm_failure`` mapping on ``exc``.

    Existing, more specific fields win unless an explicit non-empty value is
    supplied.  The mapping and compatibility attributes are additive, so older
    callers that only inspect the original exception type/message keep working.
    """
    existing = getattr(exc, "hermes_llm_failure", None)
    failure: Dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}

    scalar_fields = {
        "provider": provider,
        "model": model,
        "agent_id": agent_id,
        "status": status,
        "finish_reason": finish_reason,
    }
    for key, value in scalar_fields.items():
        if value not in (None, ""):
            failure[key] = str(value)
        else:
            failure.setdefault(key, "")

    if raw_response is not _MISSING:
        failure["raw_response"] = json_safe(raw_response)
    else:
        failure.setdefault("raw_response", exception_payload(exc))
    if validation_error is not None:
        failure["validation_error"] = json_safe(validation_error)
    else:
        failure.setdefault("validation_error", None)
    if audit is not None:
        merged_audit = dict(failure.get("audit") or {})
        merged_audit.update(json_safe(audit))
        failure["audit"] = merged_audit
    else:
        failure.setdefault("audit", {})
    if usage is not None:
        failure["usage"] = json_safe(usage)
    else:
        failure.setdefault("usage", {})

    setattr(exc, "hermes_llm_failure", failure)
    for name in (
        "provider",
        "model",
        "agent_id",
        "status",
        "finish_reason",
        "raw_response",
    ):
        with_value = failure.get(name)
        if with_value not in (None, ""):
            try:
                setattr(exc, name, with_value)
            except Exception:
                pass
    return failure


class AuxiliaryResponseFailure(RuntimeError):
    """A terminal provider response that cannot be treated as a completion."""

    def __init__(self, message: str, **diagnostic: Any) -> None:
        super().__init__(message)
        attach_llm_failure(self, **diagnostic)


__all__ = [
    "AuxiliaryResponseFailure",
    "attach_llm_failure",
    "exception_payload",
    "json_safe",
    "response_payload",
    "response_status",
]
