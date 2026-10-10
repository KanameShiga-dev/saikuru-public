import tempfile
import unittest

from team_skill_import import SKILLS, script_check

ROOT = str(SKILLS).replace('\\', '/')


class InboxCheckTests(unittest.TestCase):
    def test_text_mentioning_inbox_is_not_a_skill_reference(self):
        command = "cat > _build.py <<'EOF'\nslide('采来での取り込み：_inbox → 確認 → 適用先の選択')\nEOF\npython _build.py"
        self.assertIsNone(script_check(command, set()))

    def test_inbox_paths_are_refused(self):
        for command in ('python skills/_inbox/new/run.py', 'python "' + ROOT + '/_inbox/new/run.py"',
                        'type C:\\AI_Work\\operation\\saikuru\\skills\\_inbox\\x\\SKILL.md'):
            with self.subTest(command=command):
                self.assertFalse(script_check(command, set())[0])

    def test_writes_into_skill_folders_are_refused(self):
        for command in ('echo x > "' + ROOT + '/yomiyasu/SKILL.md"', 'rm -rf ' + ROOT + '/yomiyasu',
                        'Set-Content -Path ' + ROOT + '/yomiyasu/SKILL.md -Value x'):
            with self.subTest(command=command):
                verdict = script_check(command, {'yomiyasu'})
                self.assertFalse(verdict[0])
                self.assertIn('変更できません', verdict[1])

    def test_unrelated_commands(self):
        self.assertIsNone(script_check('python build.py > out.md', set()))


class SkillDirsTests(unittest.TestCase):
    def test_enabled_skills_are_added_to_the_cli(self):
        from team_config import default_config
        from team_engine import Context, Engine
        from team_store import Store
        temp = tempfile.TemporaryDirectory()
        store = Store(temp.name)
        config = default_config()
        config['approved_roots'] = [temp.name]
        engine = Engine(store, config, {'codex': ['fixture'], 'claude': ['fixture']}, 'http://127.0.0.1:1')
        try:
            engine.handoff.active_skill_names = lambda project: {'yomiyasu', '_inbox', 'missing-skill'}
            job = engine.create_job('t', 'g', temp.name, False)
            ctx = Context(engine, engine.new_task(job, 'r', 'r', 'researcher'))
            expected = [str(SKILLS / 'yomiyasu')] if (SKILLS / 'yomiyasu').is_dir() else []
            self.assertEqual(ctx.skill_dirs, expected)
        finally:
            engine.shutdown.set()
            engine.handoff.close()
            store.db.close()
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
