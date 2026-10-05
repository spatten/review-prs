import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from review_prs import cli
from review_prs.terminal import run_terminal


class Terminal(unittest.TestCase):
    def test_real_child_has_three_tty_descriptors_and_safe_arguments(self):
        argument = 'Review https://github.com/fossas/fossa-cli/pull/1798'
        result = run_terminal([sys.executable, '-c', 'import os,sys,json; print(json.dumps([os.isatty(0), os.isatty(1), os.isatty(2), sys.argv[1]]))', argument], timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), [True, True, True, argument])

    def test_mock_cloud_executable_through_actual_terminal_transport(self):
        url = 'https://claude.ai/code/session_fakeForTesting'
        script = 'import os,sys; assert all(os.isatty(i) for i in (0,1,2)); assert sys.argv[1:] == ["--cloud", "Review https://github.com/fossas/fossa-cli/pull/1798"]; print("View: '+url+'?from=cli&m=0")'
        def mock_executable(argv, **kwargs):
            return run_terminal([sys.executable, '-c', script, *argv[1:]], **kwargs)
        with patch.object(cli, 'run_terminal', side_effect=mock_executable):
            self.assertEqual(cli.launch('/tmp', 'https://github.com/fossas/fossa-cli/pull/1798'), ('success', url, None))

    def test_returns_on_complete_creation_line_without_waiting_for_exit(self):
        url = 'https://claude.ai/code/session_liveMock'
        script = 'import time; print("View: '+url+'?from=cli", flush=True); time.sleep(60)'
        start = time.monotonic()
        result = run_terminal([sys.executable, '-c', script], timeout=5, stop_when=cli.creation_announced)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(cli.session_url(result.stdout), url)

    def test_incomplete_or_unrelated_urls_do_not_stop_launch(self):
        self.assertFalse(cli.creation_announced('View: https://claude.ai/code/session_part'))
        self.assertFalse(cli.creation_announced('Old session: https://claude.ai/code/session_old\n'))
        self.assertTrue(cli.creation_announced('View: https://claude.ai/code/session_done?from=cli\r\n'))

    def test_timeout_preserves_output_and_reaps_child(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory)/'pid'
            script = 'import os,time,pathlib; pathlib.Path('+repr(str(pid_file))+').write_text(str(os.getpid())); print("partial output", flush=True); time.sleep(60)'
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                run_terminal([sys.executable, '-c', script], timeout=1)
            self.assertIn(b'partial output', caught.exception.output)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)

    def test_missing_executable(self):
        with self.assertRaises(FileNotFoundError):
            run_terminal(['/nonexistent-review-prs-test-executable'], timeout=1)

    def test_terminal_rejection_is_known_prelaunch_failure(self):
        result = subprocess.CompletedProcess([], 1, '', 'Error: --cloud requires an interactive terminal.\n')
        with patch.object(cli, 'run_terminal', return_value=result):
            self.assertEqual(cli.launch('/tmp', 'https://github.com/fossas/fossa-cli/pull/1798')[0], 'failed')
