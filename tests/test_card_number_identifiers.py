"""A SHA-256 digest is not a card number (third end-to-end trial)."""
import unittest

from team_dlp import has_card_number, has_personal_number
from team_egress_guard import findings

CARD = '4111111111111111'
# 64 hex characters with a Luhn-valid 16-digit run between letters, like the trial's command digest.
DIGEST = 'a9f3c1d0' + CARD + 'e' + 'b7' * 19 + 'c'


class CardNumberIdentifierTests(unittest.TestCase):
    def test_the_digest_is_a_64_character_identifier(self):
        self.assertEqual(len(DIGEST), 64)

    def test_a_luhn_valid_run_inside_a_digest_is_not_a_card_number(self):
        self.assertFalse(has_card_number('command_digest: ' + DIGEST))
        self.assertNotIn('possible_card_number', findings({'details': {'command_digest': DIGEST}}))

    def test_the_same_digits_alone_are_still_a_card_number(self):
        self.assertTrue(has_card_number('カード ' + CARD))
        self.assertTrue(has_card_number('4111 1111 1111 1111'))
        self.assertFalse(has_card_number('1234 5678 9012 3456'))

    def test_a_card_number_next_to_a_digest_is_still_found(self):
        self.assertTrue(has_card_number(DIGEST + ' / 4111 1111 1111 1111'))
        self.assertIn('possible_card_number', findings(DIGEST + ' ' + CARD))

    def test_the_personal_number_check_keeps_its_behaviour(self):
        self.assertTrue(has_personal_number('番号 1234 5678 9012'))
        self.assertFalse(has_personal_number('id ' + 'ab' * 4 + '123456789012' + 'cd' * 6))  # 32 characters


if __name__ == '__main__':
    unittest.main()
