import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from review_prs import cli as c

PR = 'https://github.com/fossas/sparkle/pull/1490'
SESSION = 'https://claude.ai/code/session_01KT8urKHBAjfZSV8YgvbbWz'


class Launch(unittest.TestCase):
    def test_demonstrated_output_and_exact_arguments(self):
        result = subprocess.CompletedProcess([], 0, 'Created cloud session: sparkle pull request 1490\nView: '+SESSION+'?from=cli&m=0\nResume with: claude --teleport session_01KT8urKHBAjfZSV8YgvbbWz\n', 'Permission allow rule warning\n')
        with patch.object(c, 'run', return_value=result) as run:
            self.assertEqual(c.launch(Path('/existing repo'), PR), ('success', SESSION, None))
            run.assert_called_once_with(['claude', '--cloud', 'Review '+PR], cwd=Path('/existing repo'), timeout=120)

    def test_timeout_without_url_is_uncertain(self):
        with patch.object(c, 'run', side_effect=subprocess.TimeoutExpired([], 120)):
            self.assertEqual(c.launch('/tmp', PR)[0], 'uncertain')

    def test_timeout_with_url_saves_success(self):
        with patch.object(c, 'run', side_effect=subprocess.TimeoutExpired([], 120, output=('View: '+SESSION+'\n').encode())):
            self.assertEqual(c.launch('/tmp', PR)[:2], ('success', SESSION))

    def test_nonzero_exit_with_url_saves_success(self):
        with patch.object(c, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'View: '+SESSION)):
            self.assertEqual(c.launch('/tmp', PR)[:2], ('success', SESSION))

    def test_missing_executable_retryable(self):
        with patch.object(c, 'run', side_effect=FileNotFoundError):
            self.assertEqual(c.launch('/tmp', PR)[0], 'failed')

    def test_missing_url_never_retried(self):
        for code in (0, 1):
            with patch.object(c, 'run', return_value=subprocess.CompletedProcess([], code, '', 'error')) as run:
                self.assertEqual(c.launch('/tmp', PR)[0], 'uncertain')
                run.assert_called_once()

    def test_session_urls_and_ambiguity(self):
        self.assertEqual(c.session_url('\x1b[32mView: '+SESSION+'\x1b[0m'), SESSION)
        self.assertIsNone(c.session_url('https://claude.ai.evil/code/session_wrong'))
        self.assertIsNone(c.session_url(SESSION+' https://claude.ai/code/session_other'))


class Checkout(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'sparkle'
        self.repo.mkdir()
        self.git('init', '-b', 'feature')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '--allow-empty', '-m', 'initial')
        self.git('remote', 'add', 'origin', 'git@github.com:fossas/sparkle.git')

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.PIPE, text=True).strip()

    def test_auto_discovery_accepts_dirty_feature_branch_unchanged(self):
        (self.repo / 'work.txt').write_text('uncommitted work')
        before = self.git('status', '--porcelain')
        with patch.object(c, 'CHECKOUTS', self.root):
            self.assertEqual(c.validate('fossas/sparkle'), (self.repo, 'feature'))
        self.assertEqual(self.git('status', '--porcelain'), before)
        self.assertEqual(self.git('branch', '--show-current'), 'feature')
        self.assertEqual((self.repo / 'work.txt').read_text(), 'uncommitted work')

    def test_wrong_owner_not_accepted_by_folder_name(self):
        with patch.object(c, 'CHECKOUTS', self.root), self.assertRaisesRegex(c.Error, 'origin does not match'):
            c.validate('other/sparkle')

    def test_explicit_mapping_and_configure(self):
        state = self.root/'state'
        with contextlib.redirect_stdout(io.StringIO()):
            c.configure(state, 'Fossas/Sparkle', self.repo)
        self.assertEqual(c.config(state)['fossas/sparkle'], str(self.repo))

    def test_linked_worktree_supported(self):
        worktree = self.root/'linked'
        self.git('worktree', 'add', '-b', 'another-feature', str(worktree))
        self.assertEqual(c.validate('fossas/sparkle', str(worktree)), (worktree, 'another-feature'))

    def test_lookalike_remote_rejected(self):
        self.git('remote', 'set-url', 'origin', 'https://github.com.evil/fossas/sparkle.git')
        with self.assertRaisesRegex(c.Error, 'origin does not match'):
            c.validate('fossas/sparkle', str(self.repo))

    def test_missing_directory_and_old_routine_config(self):
        for entry in (str(self.root/'missing'), {'routine_url': 'unused'}):
            with self.assertRaises(c.Error): c.validate('fossas/sparkle', entry)

    def test_dry_run_exercises_real_git_but_never_claude(self):
        state = self.root/'absent'
        with patch.object(c, 'STATE', state), patch.object(c, 'CHECKOUTS', self.root), patch.object(c.sys, 'stdin', io.StringIO(PR)), patch.object(c, 'launch') as launch, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(c.main(['--stdin', '--dry-run']), 0)
            launch.assert_not_called()
        self.assertIn('[feature]', out.getvalue())
        self.assertIn('claude --cloud', out.getvalue())
        self.assertFalse(state.exists())
