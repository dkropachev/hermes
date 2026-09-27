"""Identifiers, bearer handles, and idempotent-operation contracts for workspace leases."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import uuid
from typing import Any, Mapping

from hermes_cli.plugin_workspace_errors import InvalidWorkspaceHandleError
from hermes_cli.plugins_manifest import _portable_skill_namespace


HANDLE_VERSION = 1
DEFAULT_TTL_SECONDS = 300.0
MIN_TTL_SECONDS = 1.0
MAX_TTL_SECONDS = 24 * 60 * 60.0

_WORKSPACE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_PLUGIN_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def native_hashed_namespace(plugin_id: str) -> str:
    slug = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in "_-") else "-"
        for ch in plugin_id.casefold()
    ).strip("-_") or "plugin"
    digest = hashlib.sha256(plugin_id.encode("utf-8")).hexdigest()[:12]
    return f"hermes-native-{slug[:96]}-{digest}"


def plugin_namespace(plugin_id: str, skill_namespace: str) -> str:
    if skill_namespace:
        return (
            skill_namespace
            if skill_namespace.startswith("agent-plugin-")
            and _PLUGIN_NAMESPACE_RE.fullmatch(skill_namespace)
            and ".." not in skill_namespace
            and not skill_namespace.endswith(".")
            else _portable_skill_namespace(plugin_id)
        )
    folded = plugin_id.casefold()
    if (
        plugin_id == folded
        and _PLUGIN_NAMESPACE_RE.fullmatch(plugin_id)
        and ".." not in plugin_id
        and not plugin_id.endswith(".")
        and plugin_id.split(".", 1)[0] not in _WINDOWS_RESERVED
        and not plugin_id.startswith(("agent-plugin-", "hermes-native-"))
    ):
        return plugin_id
    return native_hashed_namespace(plugin_id)


def plugin_identity(plugin_id: str, skill_namespace: str) -> str:
    kind = "portable" if skill_namespace else "native"
    material = json.dumps(
        [kind, plugin_id, skill_namespace], ensure_ascii=True, separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def validated_workspace_id(workspace_id: str) -> str:
    if (
        not isinstance(workspace_id, str)
        or not _WORKSPACE_ID_RE.fullmatch(workspace_id)
        or ".." in workspace_id
        or workspace_id.endswith(".")
        or workspace_id.split(".", 1)[0] in _WINDOWS_RESERVED
    ):
        raise ValueError(
            "workspace_id must be 1-128 lowercase ASCII letters, numbers, '.', '_', or '-' "
            "(without '..', a trailing '.', or a reserved device name)"
        )
    return workspace_id


def ttl(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("ttl_seconds must be a finite number from 1 through 86400") from exc
    if not math.isfinite(parsed) or not MIN_TTL_SECONDS <= parsed <= MAX_TTL_SECONDS:
        raise ValueError("ttl_seconds must be a finite number from 1 through 86400")
    return parsed


def intent() -> dict[str, Any]:
    return {
        "contract_version": HANDLE_VERSION,
        "operation_id": str(uuid.uuid4()),
        "capability": secrets.token_urlsafe(32),
    }


def parse_intent(value: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "contract_version", "operation_id", "capability",
    }:
        raise InvalidWorkspaceHandleError("malformed workspace operation intent")
    if value.get("contract_version") != HANDLE_VERSION:
        raise InvalidWorkspaceHandleError("unsupported workspace operation intent version")
    operation_id, capability = value.get("operation_id"), value.get("capability")
    try:
        parsed = uuid.UUID(str(operation_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidWorkspaceHandleError("malformed workspace operation intent") from exc
    if str(parsed) != str(operation_id) or not isinstance(capability, str) or not 32 <= len(
        capability
    ) <= 256:
        raise InvalidWorkspaceHandleError("malformed workspace operation intent")
    return str(operation_id), capability


def handle(lease_id: str, capability: str) -> dict[str, Any]:
    return {"contract_version": HANDLE_VERSION, "lease_id": lease_id, "capability": capability}


def parse_handle(value: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "contract_version", "lease_id", "capability",
    }:
        raise InvalidWorkspaceHandleError("malformed workspace lease handle")
    if value.get("contract_version") != HANDLE_VERSION:
        raise InvalidWorkspaceHandleError("unsupported workspace lease handle version")
    lease_id, capability = value.get("lease_id"), value.get("capability")
    try:
        uuid.UUID(str(lease_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidWorkspaceHandleError("malformed workspace lease handle") from exc
    if not isinstance(capability, str) or not 32 <= len(capability) <= 256:
        raise InvalidWorkspaceHandleError("malformed workspace lease handle")
    return str(lease_id), capability


def capability_hash(capability: str) -> str:
    return hashlib.sha256(capability.encode("utf-8")).hexdigest()


def operation_fingerprint(
    layout: Any, kind: str, workspace_id: str, ttl_seconds: float,
    input_lease_id: str | None = None,
) -> str:
    encoded = json.dumps(
        [
            kind, layout.plugin_identity, layout.profile_key, workspace_id,
            float(ttl_seconds), input_lease_id,
        ],
        ensure_ascii=True, separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_operation(
    row: Mapping[str, Any], *, kind: str, workspace_id: str, fingerprint: str,
    output_capability_hash: str, input_lease_id: str | None = None,
    input_capability_hash: str | None = None,
) -> None:
    valid = (
        row["kind"] == kind
        and row["workspace_id"] == workspace_id
        and hmac.compare_digest(row["request_fingerprint"], fingerprint)
        and hmac.compare_digest(row["output_capability_hash"], output_capability_hash)
        and row["input_lease_id"] == input_lease_id
    )
    if input_capability_hash is not None:
        valid = valid and hmac.compare_digest(
            row["input_capability_hash"] or "", input_capability_hash,
        )
    if not valid:
        raise InvalidWorkspaceHandleError("workspace operation intent does not match its request")
