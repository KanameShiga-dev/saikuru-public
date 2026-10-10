"""Static checks of the screen scripts (no Node.js on this PC, so they are read as text)."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).with_name('web')


class ButtonHandlerTest(unittest.TestCase):
    def test_button_handlers_are_not_wrapped_in_action_twice(self):
        # button() already runs its handler through action(); an inner action() returns at once because of the
        # double-click guard, so the button silently does nothing (2026-10-09: Claude login button).
        found = []
        for path in WEB.glob('*.js'):
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if re.search(r'\bbutton\([^;]*?\(\)\s*=>\s*action\(', line):
                    found.append(f'{path.name}:{number}')
        self.assertFalse(found)


if __name__ == '__main__':
    unittest.main()
