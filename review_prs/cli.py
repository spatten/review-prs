"""Start a Claude cloud review for each GitHub PR link in the clipboard or another source."""
import argparse
import fcntl
import html
import json
from pathlib import Path
import re
import shlex
from .terminal import run_terminal
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

STATE = Path.home() / 'Library/Application Support/review-prs'
CHECKOUTS = Path.home() / 'fossa'
URL = re.compile(r'https?://[^\s<>"\'`|\\()\[\]{}]+', re.I)
PR_PATH = re.compile(r'/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+)/pull/([0-9]+)(?=/|$)')
SESSION = re.compile(r'https://claude\.ai/code/((?:session_|cse_)[A-Za-z0-9_-]+)(?=[?\s\x1b"<>\')]|$)')
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


class Error(Exception):
    pass


def canonical(value):
    try:
        p = urlsplit(value.rstrip('.,;:!?)]}'))
        if p.scheme.lower() not in ('http', 'https') or p.netloc.lower() != 'github.com':
            return None
        m = PR_PATH.match(p.path)
        if not m or not int(m[3]) or m[2] in ('.', '..'):
            return None
        return f'https://github.com/{m[1].lower()}/{m[2].lower()}/pull/{int(m[3])}'
    except ValueError:
        return None


def extract(*representations):
    # Scan source order, including href values; entities are decoded for HTML.
    # Never interpret surrounding text as a prompt or an instruction.
    found = {}
    for text in representations:
        for m in URL.finditer(html.unescape(text)):
            pr = canonical(m[0])
            if pr:
                found.setdefault(pr, None)
    return list(found)


def run(argv, cwd=None, timeout=30):
    return subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)


def clipboard():
    if sys.platform != 'darwin':
        raise Error('--clipboard requires macOS; use --file or --stdin elsewhere')
    p = run(['/usr/bin/osascript', '-l', 'JavaScript', str(Path(__file__).with_name('clipboard.js'))])
    if p.returncode:
        raise Error('Cannot read clipboard: ' + p.stderr.strip())
    return json.loads(p.stdout)


# Matches the "Needs your review" section of github.com/pulls/inbox: PRs that
# request your review by name. review-requested:@me would add team requests too.
INBOX_QUERY = 'is:pr is:open archived:false user-review-requested:@me'


def github():
    try:
        p = run(['gh', 'api', '-X', 'GET', 'search/issues', '-f', f'q={INBOX_QUERY}',
                 '-f', 'per_page=100', '--jq', '.items[].html_url'])
    except FileNotFoundError:
        raise Error('--github requires the GitHub CLI (gh); install it and run gh auth login')
    if p.returncode:
        raise Error('Cannot read GitHub inbox (try gh auth login): ' + p.stderr.strip())
    return [p.stdout]




def git(path, *args):
    result = run(['git', '-C', str(path), *args])
    if result.returncode:
        raise Error('not a usable Git checkout: ' + str(path))
    return result.stdout.strip()


def validate(repo, path):
    path = path.resolve()
    if not path.is_dir():
        raise Error(f'no checkout at {path}; clone {repo} there')
    root = Path(git(path, 'rev-parse', '--show-toplevel')).resolve()
    if not root.samefile(path):
        raise Error(f'{path} is not the root of a Git checkout')
    path = root
    origin = git(path, 'remote', 'get-url', 'origin')
    remote = re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?', origin, re.I)
    if not remote or remote[1].lower() != repo:
        raise Error(f'origin of {path} does not match {repo}')
    branch = git(path, 'rev-parse', '--abbrev-ref', 'HEAD')
    return path, branch if branch != 'HEAD' else 'detached HEAD'






class History:
    def __init__(self, state, readonly=False):
        path = state / 'history.sqlite3'
        self.db = None
        if readonly and not path.exists():
            return
        self.db = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) if readonly else sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        if not readonly:
            self.db.execute('CREATE TABLE IF NOT EXISTS launches (id INTEGER PRIMARY KEY, pr TEXT NOT NULL, status TEXT NOT NULL, session TEXT, timestamp TEXT NOT NULL, detail TEXT)')
            self.db.commit()

    def records(self, pr):
        return list(self.db.execute('SELECT * FROM launches WHERE pr=? ORDER BY id DESC', (pr,))) if self.db else []

    def start(self, pr):
        with self.db:
            return self.db.execute('INSERT INTO launches(pr,status,timestamp) VALUES (?, ?, ?)',
                                   (pr, 'pending', datetime.now(timezone.utc).isoformat())).lastrowid

    def finish(self, id, status, session=None, detail=None):
        with self.db:
            self.db.execute('UPDATE launches SET status=?,session=?,detail=? WHERE id=?', (status, session, detail, id))

    def close(self):
        if self.db:
            self.db.close()


def session_url(output):
    matches = list(dict.fromkeys('https://claude.ai/code/' + m[1] for m in SESSION.finditer(ANSI.sub('', output))))
    return matches[0] if len(matches) == 1 else None


def creation_announced(output):
    # A complete View line avoids mistaking a partially read ID for a URL.
    clean = ANSI.sub('', output).replace('\r\n', '\n')
    return any(session_url(line) for line in re.findall(r'^View: ([^\r\n]*)[\r\n]', clean, re.M))


def diagnostics(output):
    """Keep bounded, readable failure context; never retain raw terminal output."""
    text = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', output)
    text = ANSI.sub('', text)
    text = re.sub(r'sk-ant-[A-Za-z0-9_-]+', '[redacted]', text)
    text = re.sub(r'(?i)(Bearer\s+)\S+', r'\1[redacted]', text)
    text = ''.join(ch for ch in text if ch in '\n\t' or ord(ch) >= 32)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    errors = [line for line in lines if re.search(r'\b(error|failed|failure)\b', line, re.I)]
    selected = errors[-6:] + [line for line in lines[-8:] if line not in errors[-6:]]
    return '\n'.join(line[:800] for line in selected)[:4000]


def prompt(pr):
    return (f'Review {pr}. Report your findings only in this session. '
            'Never post comments or reviews to the PR, and do not offer to post them.')


def launch(path, pr):
    """Invoke the demonstrated CLI command once, with no shell or branch edits."""
    try:
        result = run_terminal(['claude', '--cloud', prompt(pr)], cwd=path, timeout=120, stop_when=creation_announced)
        output = result.stdout + '\n' + result.stderr
        detail = f'CLI exited {result.returncode} without a unique session URL'
    except subprocess.TimeoutExpired as e:
        def decode(value):
            return value.decode(errors='replace') if isinstance(value, bytes) else (value or '')
        output = decode(e.stdout) + '\n' + decode(e.stderr)
        detail = 'CLI timed out; creation may have succeeded'
    except OSError:
        return 'failed', None, 'could not start Claude; check it is installed and on PATH'
    except subprocess.SubprocessError:
        return 'uncertain', None, 'terminal capture failed; creation may have succeeded'
    url = session_url(output)
    if url:
        return 'success', url, None
    context = diagnostics(output)
    if context:
        detail += '\n  Claude output:\n    ' + context.replace('\n', '\n    ')
    rejected = 'Error: --cloud requires an interactive terminal.' in ANSI.sub('', output)
    return ('failed' if rejected else 'uncertain'), None, detail


def open_window(pairs):
    """Open one new Firefox window with tabs ordered PR, session, PR, session, ...

    open -n starts a second Firefox process, which hands the URLs to the running
    browser and exits; plain open -a drops --args when Firefox is already running.
    Firefox opens several bare URLs together in one new window. Adding --new-window
    splits them: it applies only to the first URL, giving two windows.
    """
    urls = [url for pair in pairs for url in pair]
    try:
        result = run(['/usr/bin/open', '-na', 'Firefox', '--args', *urls])
        if result.returncode:
            print('Could not open Firefox; use the printed links. ' + result.stderr.strip())
    except (OSError, subprocess.SubprocessError):
        print('Could not open Firefox; use the printed links.')


def process(prs, history, dry=False, force=False, opener=False, state=None):
    state = state or STATE
    failures = 0
    opened = []
    for pr in prs:
        records = history.records(pr)
        success = next((r for r in records if r['status'] == 'success'), None)
        if success and not force:
            print(f'SKIP {pr} → {success["session"]}')
            opened.append((pr, success['session']))
            continue
        repo = '/'.join(urlsplit(pr).path.split('/')[1:3])
        try:
            path, branch = validate(repo, CHECKOUTS / repo.split('/')[1])
            if dry:
                print(f'DRY {pr}\n  {repo} → {path} [{branch}]\n  {shlex.join(["claude", "--cloud", prompt(pr)])}')
                continue
        except (Error, OSError, ValueError, subprocess.SubprocessError) as e:
            print(f'FAIL {pr} — {e}')
            failures += 1
            continue
        id = history.start(pr)  # Commit before any cloud side effect, survives interruption.
        print(f'LAUNCH {pr} — waiting for Claude (up to 120 seconds)…', flush=True)
        status, url, detail = launch(path, pr)
        history.finish(id, status, url, detail)
        if status == 'success':
            print(f'OK {pr} → {url}', flush=True)
            opened.append((pr, url))
        else:
            failures += 1
            print(f'{status.upper()} {pr} — {detail}' + ('; the next run will launch it again' if status == 'uncertain' else ''))
    if opener and opened and not dry:
        open_window(opened)
    return 1 if failures else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, epilog='Each review starts with this prompt: ' + prompt('<PR URL>'))
    source = p.add_mutually_exclusive_group()
    source.add_argument('--clipboard', action='store_true', help='read macOS clipboard (default)')
    source.add_argument('--file', type=Path, help='UTF-8 text or HTML file')
    source.add_argument('--stdin', action='store_true', help='read PR links from standard input')
    source.add_argument('--github', action='store_true', help='PRs that request your review by name, via gh')
    p.add_argument('--dry-run', action='store_true', help='show what would be launched without launching or recording anything')
    p.add_argument('--force', action='store_true', help='launch PRs again even if an earlier launch succeeded')
    p.add_argument('--open', action='store_true', dest='opener', help='open each PR and its review in one new Firefox window')
    args = p.parse_args(argv)
    state = STATE.resolve()
    lock = history = None
    try:
        if not args.dry_run:
            state.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = (state / 'lock').open('a')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Error('another review-prs command is running')
        history = History(state, readonly=args.dry_run)
        texts = ([args.file.read_text()] if args.file else [sys.stdin.read()] if args.stdin
                 else github() if args.github else clipboard())
        prs = extract(*texts)
        if not prs:
            print('No PRs are waiting for your review.' if args.github else
                  'The copied content contains no usable PR links. Copy the links or linked text again.')
            return 0
        return process(prs, history, args.dry_run, args.force, args.opener, state=state)
    except (Error, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError, EOFError) as e:
        print(f'Error: {e}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nInterrupted. The next run will launch any PR that did not finish.', file=sys.stderr)
        return 130
    finally:
        if history:
            history.close()
        if lock:
            lock.close()


if __name__ == '__main__':
    sys.exit(main())
