from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pdf2md_core import customer_mcp, remote_client
from pdf2md_core.customer_mcp import handle_request
from pdf2md_core.remote_client import RemoteClient, RemoteClientError


ROOT = Path(__file__).resolve().parents[1]


class PublicDistributionContractTests(unittest.TestCase):
    def test_repository_contains_only_public_distribution_files(self) -> None:
        forbidden = (
            "quality_lab",
            "dev-canary",
            "account-payment-systems",
            "fisher-byte/pdf2md",
            "RATE_CARD_VERSION",
            "rate_credit_units",
            "rate_v1_credit_units",
        )
        public_files = [
            path
            for path in ROOT.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and "__pycache__" not in path.parts
            and path != Path(__file__).resolve()
        ]
        corpus = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in public_files
        )
        for marker in forbidden:
            self.assertNotIn(marker, corpus)

    def test_mcp_lists_customer_tools(self) -> None:
        response = handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        self.assertIsNotNone(response)
        tools = response["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertEqual(len(names), 11)
        self.assertIn("markovo_convert", names)
        self.assertIn("markovo_billing", names)
        self.assertIn("markovo_doctor", names)
        projection = json.dumps(tools, ensure_ascii=False)
        self.assertNotIn("api_key", projection)
        self.assertNotIn("base_url", projection)

    def test_mcp_paths_are_bounded_to_configured_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "allowed"
            root.mkdir()
            inside = root / "input.pdf"
            inside.write_bytes(b"%PDF")
            outside = workspace / "outside.pdf"
            outside.write_bytes(b"%PDF")
            with patch.dict(os.environ, {"MARKOVO_MCP_ROOT": str(root)}):
                self.assertEqual(
                    customer_mcp._required_input_path(
                        {"input_path": "input.pdf"}, "input_path"
                    ),
                    inside.resolve(),
                )
                with self.assertRaisesRegex(ValueError, "MARKOVO_MCP_ROOT"):
                    customer_mcp._required_input_path(
                        {"input_path": str(outside)}, "input_path"
                    )

    def test_public_client_rejects_untrusted_origin_before_using_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MARKOVO_BASE_URL": "https://attacker.invalid",
                "MARKOVO_API_KEY": "mk_secret_must_not_leave",
            },
            clear=False,
        ):
            with patch.object(
                remote_client,
                "_open_url",
                side_effect=AssertionError("network must not be reached"),
            ):
                with self.assertRaises(RemoteClientError) as raised:
                    RemoteClient.from_env()
        self.assertEqual(raised.exception.to_dict()["code"], "unsafe_api_origin")

        with patch.object(
            remote_client,
            "_open_url",
            side_effect=AssertionError("network must not be reached"),
        ):
            with self.assertRaises(RemoteClientError) as direct:
                RemoteClient(
                    "https://attacker.invalid", "mk_secret_must_not_leave"
                ).usage()
        self.assertEqual(direct.exception.to_dict()["code"], "unsafe_api_origin")

    def test_authenticated_requests_do_not_follow_redirects(self) -> None:
        handler = remote_client._NoRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(None, None, 302, "Found", {}, "https://attacker.invalid")
        )

    def test_missing_key_returns_developer_portal_guidance(self) -> None:
        with patch.dict(
            os.environ,
            {"MARKOVO_API_KEY": "", "PDF2MD_API_KEY": ""},
            clear=False,
        ):
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "markovo_usage", "arguments": {}},
                }
            )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["error"]["code"], "missing_api_key")
        self.assertEqual(
            payload["error"]["portal_url"],
            "https://markovo.net/app#developer",
        )

    def test_registry_declares_key_as_required_secret(self) -> None:
        server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
        variables = server["packages"][0]["environmentVariables"]
        api_key = next(item for item in variables if item["name"] == "MARKOVO_API_KEY")
        self.assertTrue(api_key["isRequired"])
        self.assertTrue(api_key["isSecret"])
        self.assertIn("/app#developer", api_key["description"])
        names = {item["name"] for item in variables}
        self.assertNotIn("MARKOVO_BASE_URL", names)
        self.assertIn("MARKOVO_MCP_ROOT", names)
        mcp_root = next(item for item in variables if item["name"] == "MARKOVO_MCP_ROOT")
        self.assertTrue(mcp_root["isRequired"])

    def test_missing_mcp_root_fails_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "MARKOVO_MCP_ROOT is required"):
                customer_mcp._mcp_root()

    def test_release_workflow_is_main_only_and_hash_locked(self) -> None:
        workflow = (ROOT / ".github/workflows/publish-pypi.yml").read_text(
            encoding="utf-8"
        )
        requirements = (ROOT / ".github/build-requirements.txt").read_text(
            encoding="utf-8"
        )
        self.assertGreaterEqual(workflow.count("github.ref == 'refs/heads/main'"), 2)
        self.assertIn("--require-hashes", workflow)
        self.assertIn("--index-url https://pypi.org/simple", workflow)
        self.assertIn("--no-isolation", workflow)
        self.assertIn("attestations: true", workflow)
        self.assertEqual(requirements.count("--hash=sha256:"), 5)

    def test_quota_error_preserves_billing_portal(self) -> None:
        class EmptyBalanceClient:
            def usage(self) -> dict[str, object]:
                raise RemoteClientError(
                    "Not enough credits.",
                    status=402,
                    payload={
                        "error": {
                            "code": "quota_exceeded",
                            "message": "Not enough credits.",
                            "action": "open_checkout",
                            "portal_url": "https://markovo.net/app#billing",
                        }
                    },
                )

        with patch(
            "pdf2md_core.customer_mcp.RemoteClient.from_env",
            return_value=EmptyBalanceClient(),
        ):
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "markovo_usage", "arguments": {}},
                }
            )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["error"]["code"], "quota_exceeded")
        self.assertEqual(
            payload["error"]["portal_url"],
            "https://markovo.net/app#billing",
        )

    def test_quota_error_adds_billing_guidance_when_service_omits_it(self) -> None:
        class LegacyQuotaClient:
            def usage(self) -> dict[str, object]:
                raise RemoteClientError(
                    "Not enough credits.",
                    status=402,
                    payload={
                        "error": {
                            "code": "quota_exceeded",
                            "message": "Not enough credits.",
                        }
                    },
                )

        with patch(
            "pdf2md_core.customer_mcp.RemoteClient.from_env",
            return_value=LegacyQuotaClient(),
        ):
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "markovo_usage", "arguments": {}},
                }
            )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["error"]["action"], "open_checkout")
        self.assertEqual(
            payload["error"]["portal_url"],
            "https://markovo.net/app#billing",
        )


if __name__ == "__main__":
    unittest.main()
