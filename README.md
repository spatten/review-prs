# review-prs

Copy PR links, then run:

```sh
review-prs --dry-run
review-prs
```

The command extracts PRs, finds the matching existing checkout under `~/fossa`, and runs `claude --cloud "Review <canonical PR URL>"` from that directory. It records and prints the created cloud session's URL.

**No extra clones, branch switching, routines, API tokens, browser automation, or extensions.** It uses your existing Claude CLI account. Both `~/fossa/sparkle` and `~/fossa/fossa-action` work automatically when their `origin` matches the PR repository.

## Install

Requires macOS, Python 3.10+, Git, and an installed Claude Code CLI supporting `--cloud`. The command is already installed on this machine at `~/bin/review-prs`.

For a fresh installation:

```sh
cd ~/projects/pr-launcher
python3 -m venv .venv
.venv/bin/python -m pip install .
mkdir -p ~/bin
ln -s "$PWD/.venv/bin/review-prs" ~/bin/review-prs
export PATH="$HOME/bin:$PATH"
```

Skip the symlink step if it already exists. Add the PATH line to `~/.zshrc` if needed. After edits, reinstall with `.venv/bin/python -m pip install .`. There are no third-party Python runtime dependencies.

If Claude is not signed in, run `claude auth login` yourself using your Claude account. Your successful manual `claude --cloud` launch already demonstrated the required cloud access.

## Usage

Clipboard input is the default. `--clipboard` remains an optional alias; use `--file` or `--stdin` to select another source.

```sh
review-prs                 # Launch each new PR
review-prs --dry-run       # Read-only preview; no Claude invocation
review-prs --force         # Allow repeats of successful PRs
review-prs --open          # Also open resulting links
review-prs --file reminder.txt --dry-run
review-prs --file reminder.html --dry-run
printf '%s\n' 'https://github.com/fossas/sparkle/pull/1490/files#diff-example' | review-prs --stdin --dry-run
```

Dry runs display PR URLs, checkout paths, current branches and exact intended commands. They do not create history/configuration, make network requests, launch Claude, open a browser, or replace your clipboard.

Each PR gets its own cloud session. Launch requests are sequential; cloud reviews then run independently. Exit codes are 0 for success/empty input, 1 for any failure or uncertain result, and 130 for interruption.

## Repository discovery

For `https://github.com/owner/repo/pull/123`, the default checkout is `~/fossa/repo`. The tool checks the checkout root and verifies `origin` points to `owner/repo` on GitHub. HTTPS and standard GitHub SSH remotes are supported; custom SSH host aliases require a standard origin URL. A matching folder name alone is insufficient.

For a repository somewhere else, save a path override:

```sh
review-prs --configure fossas/sparkle --checkout ~/fossa/sparkle
```

This only writes a mapping; it does not clone anything or launch a session. Configuration is optional and lives at:

```text
~/Library/Application Support/review-prs/config.json
```

```json
{
  "repositories": {
    "fossas/sparkle": "~/fossa/sparkle",
    "fossas/fossa-action": "~/fossa/fossa-action"
  }
}
```

Missing repositories get a clear error and mapping command; other PRs continue. Existing linked worktrees also work. Routine mappings from the earlier version must be replaced with path mappings using `--configure`; old token files are unused and left untouched. No configuration is needed for the two default paths above unless an old override exists.

## Branch and working-file behavior

The launcher uses the existing checkout's **current branch**, including a feature branch. It does not enforce the default branch or require a clean working tree. It never switches branches, resets, stashes, fetches, or edits repository files. Its Git calls only inspect the root, origin, and current branch.

Claude's normal cloud path clones the GitHub remote at that branch. If the Claude GitHub App is absent, Claude may instead bundle local repository data, including tracked uncommitted changes. This is the same behavior as invoking `claude --cloud` yourself. A local-only branch or detached HEAD may not work through every cloud launch path. See [Claude's cloud launch documentation](https://code.claude.com/docs/en/claude-code-on-the-web#from-terminal-to-cloud).

The exact prompt is `Review <URL>`. The launcher does not separately publish comments, submit GitHub reviews, or modify PRs. The cloud agent still follows its own permissions, skills and repository instructions; this tool does not restrict the cloud agent's actions. Local Claude permission-rule warnings do not prevent recording a returned session URL.

Model, effort and environment are left to Claude's defaults/settings. The tool exposes no overrides because reliable propagation through this launch interface has not been established. No extra cloud messages are sent.

## Extraction

Reads both plain text and HTML from the macOS clipboard using AppKit through built-in JavaScript for Automation. It never writes to the clipboard. Per clipboard item, plain text is scanned before HTML; first-seen order is retained across representations.

Accepts URLs in plain text, Markdown links, Slack `<url|label>` links and HTML anchors, including href-only URLs. Only exact `github.com` URLs identifying `/owner/repo/pull/<positive integer>` qualify. Case and zero-padded numbers are normalized, and extra paths, queries and fragments removed. Duplicates are discarded. Issues, Slack profiles, lookalike domains, explicit ports and credential-bearing URLs are ignored.

Only canonical PR URLs enter prompts; surrounding titles, usernames and instructions are discarded. Subprocesses receive argument arrays, never shell-interpolated clipboard content. Screenshots, OCR, scheme-less links and RTF-only clipboard content are unsupported.

The four-PR example yields sparkle 1474, 1486, 1490 and 1499; repeating 1490 with `/files#diff-example` adds nothing. The longer supplied example yields fossa-action 341 and sparkle 1474, 1486, 1490 and 1494.

## History and uncertain outcomes

History is stored outside this project in `~/Library/Application Support/review-prs/history.sqlite3`. Each attempt retains the canonical PR URL, UTC timestamp, status and returned session URL. Successful PRs print `SKIP` and the existing session link by default. A process lock prevents overlapping invocations.

A durable pending record is committed before invoking Claude; successes are committed individually. Failed validation or failure to start the CLI can be retried after fixing the cause. Timeouts, interruption, unrecognized output, and CLI exits without a unique session URL are uncertain and block automatic retries—even with `--force`. Failures show a bounded excerpt of Claude’s output, with common bearer/API tokens redacted, and save that excerpt in history. Later invocations display the saved error without retrying. Older attempts cannot recover output that was discarded by previous versions. A captured session URL confirms creation even if the CLI subsequently exits unsuccessfully or times out. It does not confirm review completion.

Check claude.ai/code first. If an uncertain attempt created a session:

```sh
review-prs --resolve https://github.com/fossas/sparkle/pull/1490 \
  --session-url https://claude.ai/code/session_REPLACE_WITH_REAL_ID
```

Only after confirming no session was created:

```sh
review-prs --resolve https://github.com/fossas/sparkle/pull/1490 --not-created
review-prs
```

The launcher prints `LAUNCH` immediately and returns as soon as Claude emits a complete `View:` session-link line, stopping the local CLI without waiting for the cloud review to finish. The CLI is invoked once per attempt inside a pseudo-terminal with captured output and a 120-second timeout. All three standard descriptors are terminals, as required by `--cloud`; the launcher does not type answers to interactive prompts. Local CLI processes are stopped on timeout or interruption. There are no automatic retries. History survives backend changes but cannot detect sessions created manually or on other computers. **Your manually launched PR 1490 session is not automatically in this history.** Do not include that PR in a live batch unless you want another review.

## Development checks

```sh
cd ~/projects/pr-launcher
python3 -m unittest discover -s tests -v
python3 -m review_prs.cli --dry-run
```

Tests mock cloud launches and use temporary Git repositories and state. They cover extraction, deduplication, your demonstrated session output, safe argument passing, unchanged dirty/feature checkouts, remote validation, custom mappings, linked worktrees, partial failures, success persistence, repeat suppression, force, timeouts, interruptions and uncertainty resolution. No actual review sessions are launched during development.
