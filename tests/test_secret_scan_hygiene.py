#!/usr/bin/env python3
"""Regression tests for AQG's own secret-scan hygiene."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class SecretScanHygieneTest(unittest.TestCase):
    def test_secret_pattern_self_tests_do_not_store_full_token_literals(self) -> None:
        """0.1.3 finding #1 and 0.1.4 finding #7 guard jointly:

        _secret_patterns.py's built-in self_test must use fragment-built samples
        (`"gh" + "p_" + "a" * 32`) so a repo-wide secret scan does not hit the source.
        This test guards against regressions by asserting, one by one, that the full
        literals of all 18 token shapes (9 original + 9 new) do not appear in the source.
        0.1.3 covered only 6; 0.1.4 extends to all 18.
        """
        text = (REPO / "scripts/_secret_patterns.py").read_text(encoding="utf-8")
        # these literals are themselves rebuilt in fragment form in the test source, to
        # avoid the hygiene test being hit by the secret scan itself (meta dogfooding, closed loop)
        forbidden_literals = [
            # === original 9 (0.1.3 covered 6 + 0.1.4 added 3) ===
            "gh" + "p_" + "a" * 32,                                        # github_token
            "AK" + "IA" + "1234567890ABCDEF",                              # aws_access_key
            "-----BEGIN " + "RSA PRIVATE KEY-----",                        # private_key
            "xo" + "xb-" + "1234-5678-abcdef",                             # slack_token
            "sk-" + "proj-" + "a" * 30,                                    # openai_key  [+ new]
            "sk-ant-" + "api03-" + "a" * 60,                               # anthropic_key  [+ new]
            "AI" + "za" + "B" * 35,                                        # google_api_key  [+ new]
            '"type": "' + "service_" + 'account"',                         # service_account
            "Authorization: " + "Bearer " + "abcdefghijklmnopqrstuvwx",    # bearer_token
            # === new 9 (all newly covered in 0.1.4) ===
            "sk_test_" + "a" * 24,                                         # stripe_secret_key
            "whsec_" + "a" * 24,                                           # stripe_webhook_secret
            'AWS_SECRET_ACCESS_KEY="' + "a" * 40 + '"',                    # aws_secret_access_key
            "M" + "a" * 24 + "." + "a" * 6 + "." + "a" * 30,               # discord_bot_token
            "mfa." + "a" * 84,                                             # discord_mfa_token
            "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12,            # jwt_token
            "hf_" + "a" * 35,                                              # huggingface_token
            "AccountKey=" + "a" * 80 + "==",                               # azure_storage_key
            "npm_" + "a" * 36,                                             # npm_token
        ]
        for literal in forbidden_literals:
            self.assertNotIn(literal, text, f"forbidden literal leaked into source: {literal[:30]}...")


if __name__ == "__main__":
    unittest.main()

