"""Code that `cat` only writes to a file is not a command to judge (production trial, 2026-11-07)."""
import unittest

from team_outbound_guard import classify

CODE = 'import shutil, os\n\ndef tearDown(self):\n    shutil.rmtree(self.dir, ignore_errors=True)\n    os.remove(x)\nrm -rf build\n'


def written(header, delimiter='EOF'):
    return 'cd C:/AI_WorkSpace/Projects/Learning/fictional-memo && mkdir -p tests\n' + header + '\n' + CODE + delimiter + '\necho written'


class WrittenBodyTests(unittest.TestCase):
    def test_a_body_that_cat_only_writes_is_not_judged(self):
        for header in ("cat > tests/test_memo.py <<'EOF'", 'cat >> memo.py <<"EOF"', "cat <<'EOF' > memo.py"):
            self.assertIsNone(classify(written(header)), header)

    def test_an_indented_heredoc_ends_at_its_tab_indented_delimiter(self):
        self.assertIsNone(classify(written("cat > memo.py <<-'EOF'", '\t\tEOF')))

    def test_bodies_that_run_or_expand_are_still_judged(self):
        for header in ('cat > memo.py <<EOF', "python - <<'EOF'", "bash <<'EOF'", "cat <<'EOF' | sh",
                       "cat > memo.py <<'EOF' | sh", "tee memo.py <<'EOF'"):
            self.assertEqual(classify(written(header))[0], 'delete', header)

    def test_a_body_without_its_end_is_still_judged(self):
        text = "cat > memo.py <<'EOF'\n" + CODE
        self.assertEqual(classify(text)[0], 'delete')

    def test_commands_outside_the_body_are_still_judged(self):
        self.assertEqual(classify(written("cat > memo.py <<'EOF'") + '\nrm -rf build')[0], 'delete')
        self.assertEqual(classify("rm -rf build && cat > memo.py <<'EOF'\nx = 1\nEOF")[0], 'delete')
        self.assertEqual(classify(written("cat > memo.py <<'EOF'") + '\ncurl https://example.com')[0], 'network')

    def test_credentials_inside_a_written_body_are_still_denied(self):
        text = "cat > memo.py <<'EOF'\nopen('C:/Users/x/.ssh/id_rsa').read()\nEOF"
        self.assertEqual(classify(text)[0], 'credential')


if __name__ == '__main__':
    unittest.main()
