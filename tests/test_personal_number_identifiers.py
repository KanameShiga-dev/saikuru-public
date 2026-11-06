"""Synthetic identifiers must not disable personal-number protection."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from team_attachments import provider_input
from team_dlp import has_personal_number, scan_input
from team_egress_guard import (
    BLOCK_MARKER, findings, require_safe_egress, sanitize_tool_output,
)


IDENTIFIER = "abcde" + "123456789012" + "abcdef012abcdef"
SHA256 = "abcde" + "123456789012" + "abcdef" * 7 + "abcde"


class PersonalNumberIdentifierTests(unittest.TestCase):
    def test_exact_hex_identifier_is_not_split_into_a_personal_number(self):
        self.assertEqual(len(IDENTIFIER), 32)
        for value in (IDENTIFIER, IDENTIFIER.upper(), f'"task_id": "{IDENTIFIER}"',
                      f"result:{IDENTIFIER}:1", f"担当ID：{IDENTIFIER}"):
            with self.subTest(value=value):
                self.assertFalse(has_personal_number(value))
                self.assertNotIn("personal_number", findings(value))
                self.assertEqual(scan_input(value), {"block": [], "confirm": []})
                require_safe_egress(value)

    def test_sha256_digest_is_not_split_into_a_personal_number(self):
        self.assertEqual(len(SHA256), 64)
        for digest in (SHA256, SHA256.upper()):
            with self.subTest(digest=digest):
                self.assertFalse(has_personal_number(digest))
                payload = {"approval_digest": digest}
                self.assertNotIn("personal_number", findings(payload))
                require_safe_egress(payload)
        self.assertIn("personal_number", findings({"approval_digest": SHA256, "content": "1234-5678-9012"}))

    def test_arbitrary_labels_and_malformed_identifiers_are_not_exempt(self):
        for value in ("123456789012", "1234-5678-9012", "1234 5678 9012",
                      "個人番号123456789012です", "test123456789012data",
                      "abc123456789012def", "x" + IDENTIFIER, IDENTIFIER + "_suffix",
                      IDENTIFIER[:-1], IDENTIFIER + "a", SHA256[:-1]):
            with self.subTest(value=value):
                self.assertTrue(has_personal_number(value))
                self.assertIn("personal_number", findings(value))

    def test_test_data_labels_do_not_authorize_sending_numbers(self):
        for prefix in ("テスト用：", "架空：", "test_data=true ", "個人番号："):
            for number in ("123456789012", "1234-5678-9012", "1234 5678 9012"):
                with self.subTest(prefix=prefix, number=number):
                    with self.assertRaises(ValueError) as caught:
                        require_safe_egress(prefix + number)
                    self.assertIn("personal_number", str(caught.exception))
                    self.assertNotIn(number, str(caught.exception))

    def test_valid_or_invalid_check_digit_does_not_make_a_number_safe(self):
        # Generate only synthetic candidates, never actual personal records.
        digits = "12345678901"
        total = sum(int(digit) * (index + 2 if index < 6 else index - 4)
                    for index, digit in enumerate(reversed(digits)))
        remainder = total % 11
        check = 0 if remainder <= 1 else 11 - remainder
        for suffix in (check, (check + 1) % 10):
            with self.subTest(check_digit=suffix):
                value = digits + str(suffix)
                self.assertTrue(has_personal_number(value))
                self.assertIn("12桁の番号（個人番号の可能性）", scan_input(value)["confirm"])
                with self.assertRaises(ValueError):
                    require_safe_egress(value)

    def test_identifier_does_not_hide_another_number_or_secret(self):
        for payload in (
            {"task_id": IDENTIFIER, "content": "1234-5678-9012"},
            {"task_id": IDENTIFIER, "attachment_text": "テスト用 123456789012"},
            {"task_id": "123456789012"},
        ):
            with self.subTest(payload=payload):
                self.assertIn("personal_number", findings(payload))
                with self.assertRaises(ValueError):
                    require_safe_egress(payload)
        self.assertIn("secret", findings({"task_id": IDENTIFIER,
                                         "content": "api_key=synthetic_not_a_real_secret"}))

    def test_tool_outputs_keep_ids_but_withhold_number_candidates(self):
        value = {"task_id": IDENTIFIER, "stdout": "Synthetic successful test"}
        clean, labels, changed = sanitize_tool_output(value)
        self.assertEqual(clean, value)
        self.assertEqual(labels, [])
        self.assertFalse(changed)
        clean, labels, changed = sanitize_tool_output(
            {"task_id": IDENTIFIER, "stdout": "テスト用 1234-5678-9012"})
        self.assertTrue(changed)
        self.assertIn("personal_number", labels)
        self.assertEqual(clean["stdout"], BLOCK_MARKER)

    def test_provider_boundary_checks_prompt_and_attachment_text(self):
        ctx = SimpleNamespace(agent_system_instructions="",
                              agent_definition=SimpleNamespace(description=""))
        provider_input(ctx, f"Synthetic task {IDENTIFIER}", "claude")
        for text in ("123456789012", f"{IDENTIFIER}\n参考添付本文：1234-5678-9012"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    provider_input(ctx, text, "claude")

    def test_extracted_attachment_text_is_checked_independently_of_its_id(self):
        attachments = SimpleNamespace(
            select=lambda ids: [{"id": IDENTIFIER, "name": "synthetic.txt"}],
            content=lambda ids, provider: [{"type": "text", "text": "テスト用 1234-5678-9012"}])
        ctx = SimpleNamespace(
            agent_system_instructions="", agent_definition=SimpleNamespace(description=""),
            attachment_ids=[IDENTIFIER], attachments=attachments)
        with self.assertRaises(ValueError) as caught:
            provider_input(ctx, "Synthetic request", "claude")
        self.assertIn("personal_number", str(caught.exception))
        attachments.content = lambda ids, provider: [{"type": "text", "text": "<個人番号>"}]
        provider_input(ctx, "Synthetic request", "claude")

    def test_web_publication_and_document_checks_use_the_same_detection(self):
        import team_artifact_guard
        import team_quality_document
        import team_web_guard
        policy = {"max_length": 1000, "blocked_domains": [], "blocked_terms": [], "allowed_terms": []}
        with patch.object(team_web_guard, "load_policy", return_value=policy), \
                patch.object(team_artifact_guard, "load_policy", return_value=policy), \
                patch.object(team_artifact_guard, "_environment_terms", return_value=[]):
            for value, expected in ((IDENTIFIER, True), ("テスト用 1234-5678-9012", False)):
                with self.subTest(value=value):
                    self.assertEqual(team_web_guard.check_query(value)[0], expected)
                    self.assertEqual(team_artifact_guard.check_text(value)[0], expected)
                    checks = team_quality_document._sensitive(value)
                    self.assertEqual(checks[0]["state"], "passed" if expected else "inconclusive")

    def test_placeholder_is_preferred_and_does_not_need_an_exception(self):
        self.assertEqual(findings("テスト用：<個人番号>"), [])


if __name__ == "__main__":
    unittest.main()
