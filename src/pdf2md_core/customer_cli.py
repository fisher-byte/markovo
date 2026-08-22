from __future__ import annotations

import argparse
import json
from pathlib import Path

from .bundle_manifest import verify_bundle_manifest
from .remote_client import (
    DEFAULT_POLL_ATTEMPTS,
    RemoteClient,
    RemoteClientError,
    remote_product_diagnostics,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="markovo")
    parser.add_argument("--version", action="version", version="markovo 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)

    convert = commands.add_parser("convert", help="Convert a file through the Markovo product API.")
    convert.add_argument("input", type=Path)
    convert.add_argument("--out", type=Path, required=True)
    convert.add_argument("--mode", choices=["fast", "accurate"], default="fast")
    convert.add_argument(
        "--layout-fidelity",
        choices=["standard", "formula"],
        default="standard",
        help=(
            "Use the opt-in PDF formula-region Beta. Detected equations are returned "
            "as LaTeX and must be reviewed against the original."
        ),
    )
    convert.add_argument("--base-url")
    convert.add_argument("--api-key")
    convert.add_argument("--max-credits", required=True)
    convert.add_argument("--poll-interval-seconds", type=float, default=1.8)
    convert.add_argument("--poll-attempts", type=int, default=DEFAULT_POLL_ATTEMPTS)
    convert.add_argument("--download-format", choices=["zip", "md"], default="zip")
    convert.add_argument("--capability-id")

    url_convert = commands.add_parser(
        "url-convert",
        help="Convert one public HTTPS page after explicit remote-fetch consent.",
    )
    url_convert.add_argument("url")
    url_convert.add_argument("--out", type=Path, required=True)
    url_convert.add_argument("--base-url")
    url_convert.add_argument("--api-key")
    url_convert.add_argument("--max-credits", required=True)
    url_convert.add_argument("--poll-interval-seconds", type=float, default=1.8)
    url_convert.add_argument("--poll-attempts", type=int, default=DEFAULT_POLL_ATTEMPTS)
    url_convert.add_argument("--download-format", choices=["zip", "md"], default="zip")
    url_convert.add_argument(
        "--accept-remote-fetch",
        action="store_true",
        help="Confirm that Markovo may fetch this public URL in its isolated sandbox.",
    )

    for name, help_text in (
        ("account", "Show the account attached to MARKOVO_API_KEY."),
        ("usage", "Show account Credit usage."),
        ("capabilities", "Show the live Web/API/CLI/MCP capability registry."),
        ("billing", "Return the secure browser billing URL."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--base-url")
        command.add_argument("--api-key")

    assets = commands.add_parser(
        "assets", help="List manifest-verified image assets for a completed job."
    )
    assets.add_argument("job_id")
    assets.add_argument("--base-url")
    assets.add_argument("--api-key")

    asset_url = commands.add_parser(
        "asset-url", help="Create a revocable short-lived URL for one Bundle image."
    )
    asset_url.add_argument("job_id")
    asset_url.add_argument("asset_path")
    asset_url.add_argument("--expires-in-seconds", type=int, default=300)
    asset_url.add_argument("--base-url")
    asset_url.add_argument("--api-key")

    asset_revoke = commands.add_parser(
        "asset-revoke", help="Revoke a previously issued temporary asset URL."
    )
    asset_revoke.add_argument("job_id")
    asset_revoke.add_argument("grant_id")
    asset_revoke.add_argument("--base-url")
    asset_revoke.add_argument("--api-key")

    doctor = commands.add_parser("doctor", help="Check client, service, and API-key readiness.")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--base-url")
    doctor.add_argument("--api-key")

    verify = commands.add_parser("bundle-verify", help="Verify a downloaded Markovo bundle.")
    verify.add_argument("bundle", type=Path)

    commands.add_parser("mcp", help="Run the Markovo MCP server over stdio.")
    args = parser.parse_args(argv)
    try:
        if args.command == "convert":
            options: dict[str, object] = {
                "max_credits": args.max_credits,
                "mode": args.mode,
                "layout_fidelity": args.layout_fidelity,
                "poll_interval_seconds": args.poll_interval_seconds,
                "poll_attempts": args.poll_attempts,
                "download_format": args.download_format,
            }
            if args.capability_id:
                options["capability_id"] = args.capability_id
            payload = RemoteClient.from_env(
                base_url=args.base_url, api_key=args.api_key
            ).convert_file(args.input, args.out, **options)
        elif args.command == "url-convert":
            payload = RemoteClient.from_env(
                base_url=args.base_url, api_key=args.api_key
            ).convert_url(
                args.url,
                args.out,
                max_credits=args.max_credits,
                accept_remote_fetch=args.accept_remote_fetch,
                poll_interval_seconds=args.poll_interval_seconds,
                poll_attempts=args.poll_attempts,
                download_format=args.download_format,
            )
        elif args.command in {"account", "usage", "capabilities", "billing"}:
            client = (
                RemoteClient.for_capability_discovery(
                    base_url=args.base_url, api_key=args.api_key
                )
                if args.command == "capabilities"
                else RemoteClient.from_env(base_url=args.base_url, api_key=args.api_key)
            )
            payload = getattr(client, args.command)()
        elif args.command == "assets":
            payload = RemoteClient.from_env(
                base_url=args.base_url, api_key=args.api_key
            ).list_job_assets(args.job_id)
        elif args.command == "asset-url":
            payload = RemoteClient.from_env(
                base_url=args.base_url, api_key=args.api_key
            ).create_asset_grant(
                args.job_id,
                args.asset_path,
                expires_in_seconds=args.expires_in_seconds,
            )
        elif args.command == "asset-revoke":
            payload = RemoteClient.from_env(
                base_url=args.base_url, api_key=args.api_key
            ).revoke_asset_grant(args.job_id, args.grant_id)
        elif args.command == "doctor":
            payload = remote_product_diagnostics(
                base_url=args.base_url, api_key=args.api_key
            )
        elif args.command == "bundle-verify":
            payload = verify_bundle_manifest(args.bundle)
        elif args.command == "mcp":
            from .customer_mcp import main as mcp_main

            mcp_main()
            return 0
        else:  # pragma: no cover - argparse prevents this
            return 2
    except (RemoteClientError, ValueError) as exc:
        error = exc.to_dict() if isinstance(exc, RemoteClientError) else {
            "code": "invalid_request",
            "message": str(exc),
        }
        print(json.dumps({"error": error}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
