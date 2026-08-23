from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .bundle_manifest import verify_bundle_manifest
from .remote_client import (
    DEFAULT_POLL_ATTEMPTS,
    REMOTE_FILE_CAPABILITY_IDS,
    RemoteClient,
    RemoteClientError,
    remote_product_diagnostics,
)

SERVER_INFO = {"name": "markovo", "version": "0.1.1"}
PROTOCOL_VERSION = "2024-11-05"


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _error(None, -32700, f"Parse error: {exc}")
        else:
            response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    try:
        if method == "initialize":
            return _result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": SERVER_INFO,
                    "capabilities": {"tools": {}},
                },
            )
        if method == "tools/list":
            return _result(request_id, {"tools": _tools()})
        if method == "tools/call":
            params = request.get("params") or {}
            return _result(
                request_id,
                _call_tool(str(params.get("name", "")), params.get("arguments") or {}),
            )
        return _error(request_id, -32601, f"Unknown method: {method}")
    except ValueError as exc:
        return _error(request_id, -32602, str(exc))
    except Exception:  # pragma: no cover - defensive JSON-RPC boundary
        return _error(request_id, -32603, "Internal error.")


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "markovo_convert":
            input_path = _required_input_path(arguments, "input_path")
            out_dir = _required_output_dir(arguments, "out_dir")
            options: dict[str, Any] = {
                "max_credits": _required(arguments, "max_credits"),
                "mode": str(arguments.get("mode", "fast")),
                "layout_fidelity": str(arguments.get("layout_fidelity", "standard")),
                "poll_interval_seconds": float(
                    arguments.get("poll_interval_seconds", 1.8)
                ),
                "poll_attempts": int(
                    arguments.get("poll_attempts", DEFAULT_POLL_ATTEMPTS)
                ),
                "download_format": str(arguments.get("download_format", "zip")),
            }
            if arguments.get("capability_id"):
                options["capability_id"] = str(arguments["capability_id"])
            return _content(
                _client().convert_file(input_path, out_dir, **options)
            )
        if name == "markovo_convert_url":
            if arguments.get("accept_remote_fetch") is not True:
                raise ValueError(
                    "URL conversion requires explicit remote-fetch consent."
                )
            return _content(
                _client().convert_url(
                    str(_required(arguments, "url")),
                    _required_output_dir(arguments, "out_dir"),
                    max_credits=_required(arguments, "max_credits"),
                    accept_remote_fetch=True,
                    poll_interval_seconds=float(
                        arguments.get("poll_interval_seconds", 1.8)
                    ),
                    poll_attempts=int(
                        arguments.get("poll_attempts", DEFAULT_POLL_ATTEMPTS)
                    ),
                    download_format=str(arguments.get("download_format", "zip")),
                )
            )
        if name == "markovo_job_status":
            job_id = str(_required(arguments, "job_id"))
            return _content(_client().get_job(job_id))
        if name == "markovo_job_assets":
            job_id = str(_required(arguments, "job_id"))
            return _content(_client().list_job_assets(job_id))
        if name == "markovo_asset_url":
            return _content(
                _client().create_asset_grant(
                    str(_required(arguments, "job_id")),
                    str(_required(arguments, "asset_path")),
                    expires_in_seconds=int(arguments.get("expires_in_seconds", 300)),
                )
            )
        if name == "markovo_asset_revoke":
            return _content(
                _client().revoke_asset_grant(
                    str(_required(arguments, "job_id")),
                    str(_required(arguments, "grant_id")),
                )
            )
        if name == "markovo_usage":
            return _content(_client().usage())
        if name == "markovo_capabilities":
            client = RemoteClient.for_capability_discovery()
            return _content(client.capabilities())
        if name == "markovo_billing":
            return _content(_client().billing())
        if name == "markovo_bundle_verify":
            return _content(
                verify_bundle_manifest(_required_input_path(arguments, "bundle"))
            )
        if name == "markovo_doctor":
            return _content(remote_product_diagnostics())
    except RemoteClientError as exc:
        return _content({"error": exc.to_dict()})
    raise ValueError(f"Unknown tool: {name}")


def _client() -> RemoteClient:
    """Load credentials only from the host environment, never model-visible input."""

    return RemoteClient.from_env()


def _required(arguments: dict[str, Any], name: str) -> Any:
    value = arguments.get(name)
    if value is None or value == "":
        raise ValueError(f"`{name}` is required.")
    return value


def _mcp_root() -> Path:
    configured = os.getenv("MARKOVO_MCP_ROOT")
    if not configured:
        raise ValueError(
            "MARKOVO_MCP_ROOT is required and must name a dedicated existing directory."
        )
    root = Path(configured).expanduser()
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("MARKOVO_MCP_ROOT must name an existing directory.") from exc
    if not resolved.is_dir():
        raise ValueError("MARKOVO_MCP_ROOT must name an existing directory.")
    return resolved


def _sandboxed_path(arguments: dict[str, Any], name: str, *, must_exist: bool) -> Path:
    root = _mcp_root()
    candidate = Path(str(_required(arguments, name))).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as exc:
        raise ValueError(f"`{name}` does not exist or cannot be resolved.") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"`{name}` must stay within MARKOVO_MCP_ROOT ({root})."
        ) from exc
    return resolved


def _required_input_path(arguments: dict[str, Any], name: str) -> Path:
    resolved = _sandboxed_path(arguments, name, must_exist=True)
    if not resolved.is_file() and not (name == "bundle" and resolved.is_dir()):
        raise ValueError(f"`{name}` must name an existing file.")
    return resolved


def _required_output_dir(arguments: dict[str, Any], name: str) -> Path:
    resolved = _sandboxed_path(arguments, name, must_exist=False)
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"`{name}` must name a directory.")
    return resolved


def _content(payload: Any) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}
        ]
    }


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "markovo_convert",
            "description": "Convert a supported file through the account-metered Markovo API.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "input_path": {"type": "string"},
                    "out_dir": {"type": "string"},
                    "max_credits": {
                        "type": "number",
                        "minimum": 0.001,
                        "multipleOf": 0.001,
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["fast", "accurate"],
                        "default": "fast",
                    },
                    "layout_fidelity": {
                        "type": "string",
                        "enum": ["standard", "formula"],
                        "default": "standard",
                        "description": (
                            "Opt-in formula-region Beta for PDFs. Detected equations are returned "
                            "as LaTeX and must be reviewed against the original."
                        ),
                    },
                    "capability_id": {
                        "type": "string",
                        "enum": list(REMOTE_FILE_CAPABILITY_IDS),
                    },
                    "download_format": {
                        "type": "string",
                        "enum": ["zip", "md"],
                        "default": "zip",
                    },
                },
                "required": ["input_path", "out_dir", "max_credits"],
            },
        },
        {
            "name": "markovo_convert_url",
            "description": (
                "Convert one public HTTPS page after explicit consent to Markovo's "
                "isolated remote fetch."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                    "out_dir": {"type": "string"},
                    "max_credits": {
                        "type": "number",
                        "minimum": 0.001,
                        "multipleOf": 0.001,
                    },
                    "accept_remote_fetch": {"type": "boolean", "const": True},
                    "poll_interval_seconds": {"type": "number", "default": 1.8},
                    "poll_attempts": {
                        "type": "integer",
                        "default": DEFAULT_POLL_ATTEMPTS,
                    },
                    "download_format": {
                        "type": "string",
                        "enum": ["zip", "md"],
                        "default": "zip",
                    },
                },
                "required": ["url", "out_dir", "max_credits", "accept_remote_fetch"],
            },
        },
        *[
            {
                "name": name,
                "description": description,
                "inputSchema": {"type": "object", "properties": properties},
            }
            for name, description, properties in (
                (
                    "markovo_job_status",
                    "Fetch a remote job.",
                    {"job_id": {"type": "string"}},
                ),
                (
                    "markovo_job_assets",
                    "List manifest-verified image assets for a completed job.",
                    {"job_id": {"type": "string"}},
                ),
                (
                    "markovo_asset_url",
                    "Create a revocable 60-600 second bearer URL for one Bundle image.",
                    {
                        "job_id": {"type": "string"},
                        "asset_path": {"type": "string", "pattern": "^assets/"},
                        "expires_in_seconds": {
                            "type": "integer",
                            "minimum": 60,
                            "maximum": 600,
                            "default": 300,
                        },
                    },
                ),
                (
                    "markovo_asset_revoke",
                    "Revoke a temporary asset URL before it expires.",
                    {
                        "job_id": {"type": "string"},
                        "grant_id": {"type": "string"},
                    },
                ),
                ("markovo_usage", "Fetch account Credit usage.", {}),
                (
                    "markovo_capabilities",
                    "Fetch the live capability registry.",
                    {},
                ),
                ("markovo_billing", "Return the secure billing URL.", {}),
                (
                    "markovo_bundle_verify",
                    "Verify a downloaded Markovo bundle.",
                    {"bundle": {"type": "string"}},
                ),
                (
                    "markovo_doctor",
                    "Check client, service, and API-key readiness.",
                    {},
                ),
            )
        ],
    ]


if __name__ == "__main__":
    main()
