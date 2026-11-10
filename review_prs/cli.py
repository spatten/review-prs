"""Small, dependency-free PR launcher. No clipboard text becomes executable code."""
import argparse
import fcntl
import html
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
from .terminal import run_terminal
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

STATE = Path.home() / 'Library/Application Support/review-prs'
CHECKOUTS = Path.home() / 'fossa'
URL = re.compile(r'https?://[^\s<>"\'`|\\()\[\]{}]+', re.I)
REPO = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+\Z')
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


def inbox():
    try:
        p = run(['gh', 'api', '-X', 'GET', 'search/issues', '-f', f'q={INBOX_QUERY}',
                 '-f', 'per_page=100', '--jq', '.items[].html_url'])
    except FileNotFoundError:
        raise Error('--inbox requires the GitHub CLI (gh); install it and run gh auth login')
    if p.returncode:
        raise Error('Cannot read GitHub inbox (try gh auth login): ' + p.stderr.strip())
    return [p.stdout]


def config(state):
    path = state / 'config.json'
    data = json.loads(path.read_text()) if path.exists() else {'repositories': {}}
    if not isinstance(data, dict) or not isinstance(data.get('repositories'), dict):
        raise Error('config.json must contain a repositories object')
    if any(not isinstance(k, str) or (not REPO.fullmatch(k) or k.split('/')[1] in ('.', '..')) for k in data['repositories']):
        raise Error('repository keys must be owner/repo')
    return {k.lower(): v for k, v in data['repositories'].items()}


def git(path, *args):
    result = run(['git', '-C', str(path), *args])
    if result.returncode:
        raise Error('not a usable Git checkout: ' + str(path))
    return result.stdout.strip()


def validate(repo, entry=None):
    if entry is not None and not isinstance(entry, str):
        raise Error(f'old routine configuration; replace with: review-prs --configure {repo} --checkout <path>')
    path = Path(entry).expanduser().resolve() if entry is not None else (CHECKOUTS / repo.split('/')[1]).resolve()
    if not path.is_dir():
        raise Error(f'no checkout at {path}; use: review-prs --configure {repo} --checkout <path>')
    root = Path(git(path, 'rev-parse', '--show-toplevel')).resolve()
    if not root.samefile(path):
        raise Error('mapping must point to the root of a Git checkout')
    path = root
    origin = git(path, 'remote', 'get-url', 'origin')
    remote = re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?', origin, re.I)
    if not remote or remote[1].lower() != repo:
        raise Error(f'origin does not match {repo}; use: review-prs --configure {repo} --checkout <path>')
    branch = git(path, 'rev-parse', '--abbrev-ref', 'HEAD')
    return path, branch if branch != 'HEAD' else 'detached HEAD'


def private_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def configure(state, repo, checkout):
    repo = repo.lower()
    if not REPO.fullmatch(repo) or repo.split('/')[1] in ('.', '..'):
        raise Error('expected owner/repo')
    path, branch = validate(repo, str(checkout))
    repos = config(state)
    repos[repo] = str(path)
    private_write(state / 'config.json', json.dumps({'repositories': repos}, indent=2) + '\n')
    print(f'Mapped {repo} → {path} [{branch}]')


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


def process(prs, repos, history, dry=False, force=False, opener=False, state=None):
    state = state or STATE
    failures = 0
    opened = []
    for pr in prs:
        records = history.records(pr)
        uncertain = next((r for r in records if r['status'] in ('pending', 'uncertain')), None)
        if uncertain:
            print(f'UNCERTAIN {pr} — check claude.ai/code, then use --resolve; --force does not override this')
            if uncertain['detail']:
                print('  Previous attempt: ' + uncertain['detail'])
            failures += 1
            continue
        success = next((r for r in records if r['status'] == 'success'), None)
        if success and not force:
            print(f'SKIP {pr} → {success["session"]}')
            opened.append((pr, success['session']))
            continue
        repo = '/'.join(urlsplit(pr).path.split('/')[1:3])
        try:
            path, branch = validate(repo, repos.get(repo))
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
            print(f'{status.upper()} {pr} — {detail}' + ('; check claude.ai/code before --resolve' if status == 'uncertain' else ''))
    if opener and opened and not dry:
        open_window(opened)
    return 1 if failures else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group()
    source.add_argument('--clipboard', action='store_true', help='read macOS clipboard (default)')
    source.add_argument('--file', type=Path, help='UTF-8 text or HTML file')
    source.add_argument('--stdin', action='store_true')
    source.add_argument('--inbox', action='store_true', help='PRs that request your review by name, via gh')
    source.add_argument('--configure', metavar='OWNER/REPO', help='map a repository outside ~/fossa; never launches')
    source.add_argument('--resolve', metavar='PR_URL', help='resolve uncertain attempts after checking the cloud UI')
    p.add_argument('--checkout', type=Path, help='existing checkout path for --configure')
    resolution = p.add_mutually_exclusive_group()
    resolution.add_argument('--session-url')
    resolution.add_argument('--not-created', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--force', action='store_true')
    p.add_argument('--open', action='store_true', dest='opener', help='open each PR and its review in one new Firefox window')
    args = p.parse_args(argv)
    if (args.configure or args.resolve) and (args.dry_run or args.force or args.opener):
        p.error('setup/resolve cannot be combined with launch flags')
    if bool(args.resolve) != bool(args.session_url or args.not_created):
        p.error('--resolve requires --session-url or --not-created, and vice versa')
    if bool(args.configure) != bool(args.checkout):
        p.error('--configure requires --checkout, and vice versa')
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
        if args.configure:
            configure(state, args.configure, args.checkout)
            return 0
        history = History(state, readonly=args.dry_run)
        if args.resolve:
            pr = canonical(args.resolve)
            url = session_url(args.session_url or '')
            if not pr or (args.session_url and not url):
                raise Error('provide a valid PR URL and Claude session URL')
            records = [r for r in history.records(pr) if r['status'] in ('pending', 'uncertain')]
            if not records:
                raise Error('no uncertain attempt found for this PR')
            for r in records:
                history.finish(r['id'], 'success' if url else 'failed', url, 'Manually resolved')
            print(f'Resolved {pr}: {url or "not created; safe to retry"}')
            return 0
        texts = ([args.file.read_text()] if args.file else [sys.stdin.read()] if args.stdin
                 else inbox() if args.inbox else clipboard())
        prs = extract(*texts)
        if not prs:
            print('No PRs are waiting for your review.' if args.inbox else
                  'The copied content contains no usable PR links. Copy the links or linked text again.')
            return 0
        return process(prs, config(state), history, args.dry_run, args.force, args.opener, state=state)
    except (Error, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError, EOFError) as e:
        print(f'Error: {e}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nInterrupted. Any pending launch is uncertain; check claude.ai/code before retrying.', file=sys.stderr)
        return 130
    finally:
        if history:
            history.close()
        if lock:
            lock.close()


if __name__ == '__main__':
    sys.exit(main())
