"""One-shot container commands must never start services or run bootstrap."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class EntrypointCommandTests(unittest.TestCase):
    def test_explicit_command_preserves_arguments_and_exit_status(self):
        script = Path(__file__).resolve().parents[1] / 'entrypoint.sh'
        with tempfile.TemporaryDirectory() as directory:
            # Any accidental bootstrap invocation fails without reaching a DB.
            for name in ('python', 'supervisord'):
                stub = Path(directory) / name
                stub.write_text('#!/bin/sh\necho UNEXPECTED_BOOTSTRAP >&2\nexit 99\n')
                stub.chmod(0o755)
            result = subprocess.run(
                ['/bin/bash', str(script), '/bin/sh', '-c',
                 'printf "%s" "$1"; exit 23', 'fixture', 'argument with spaces'],
                env={**os.environ, 'PATH': directory}, cwd=directory,
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertEqual(result.stdout, 'argument with spaces')
            self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
