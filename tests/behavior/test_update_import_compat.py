"""Run with stdlib unittest on the oldest supported Python as well as pytest."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class UpdateImportCompatibility(unittest.TestCase):
    def test_fresh_interpreter_import_and_disabled_startup(self):
        repo = Path(__file__).resolve().parents[2]
        commands = (
            ['-c', 'from scripts.aqg_update import run, rules; '
             'assert rules.policy_problem("") is None'],
            ['-m', 'scripts.aqg_update.run'],
        )
        with tempfile.TemporaryDirectory() as state_dir:
            env = dict(os.environ, AQG_NO_UPDATE_CHECK='1', AQG_STATE_ROOT=state_dir,
                       AQG_ROOT=str(repo), HOME=state_dir, USERPROFILE=state_dir)
            for command in commands:
                with self.subTest(command=command):
                    result = subprocess.run(
                        [sys.executable, '-E', '-s', *command], cwd=repo, env=env,
                        capture_output=True, text=True, timeout=15,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(result.stderr, '')
                    self.assertEqual(list(Path(state_dir).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
