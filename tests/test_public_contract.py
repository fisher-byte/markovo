from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from pdf2md_core.customer_mcp import handle_request
from pdf2md_core.remote_client import RemoteClientError


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


if __name__ == "__main__":
    unittest.main()
