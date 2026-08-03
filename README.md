# claude-tabs

Indexes every iTerm2 window, tab and split pane: working directory, foreground
process, and for each Claude Code session its title, transcript file, session
id, git branch, idle time and first/last prompt.

Stdlib Python 3 only (tested on the macOS system Python, 3.9.6), macOS only.
Read-only except `--jump`, which activates a tab.

## Requirements

- macOS with iTerm2. Developed against 3.6.11; the code works around two
  AppleScript gaps in that version, noted below.
- Python 3.7 or newer, which the macOS system Python satisfies. No packages to
  install, nothing to build. (3.7 is the floor because of
  `subprocess.run(capture_output=...)`.)
- Claude Code, if you want the session columns. Without it the tool still
  indexes windows, tabs, paths and foreground processes.
- Automation permission for iTerm2, for whichever terminal you run it from.
  macOS prompts for this on the first run.

## Install

```sh
git clone https://github.com/alpersonalwebsite/claude-tabs.git
cd claude-tabs
mkdir -p ~/.local/bin
ln -sfn "$PWD/claude_tabs.py" ~/.local/bin/claude-tabs
```

`~/.local/bin` is **not** on the default PATH that macOS assembles from
`/etc/paths` and `/etc/paths.d`. So if `claude-tabs` comes back as `command not
found`, add it and reload:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && exec zsh
```

Run that once. `>>` appends, so running it again just leaves a duplicate export
line: harmless, but there is nothing to gain from it.

Prefer not to touch your shell config? Symlink into `/usr/local/bin` instead,
which is on the default PATH, though writing there needs `sudo` unless Homebrew
already made it yours.

No `chmod` step is needed. The file is committed executable (mode `100755`) and
stays that way through both `git clone` and GitHub's "Download ZIP". The one
exception is fetching the single file with `curl`, which lands it `644`: either
`chmod +x claude_tabs.py` or run it as `python3 claude_tabs.py`, which needs no
executable bit at all.

If you later move or rename the checkout, re-run the `ln -sfn` line from the new
location and you are done. Nothing else depends on where the code lives. Use
`-sfn` rather than plain `-s`: `ln -s` fails with `File exists` when the link is
already there, so it cannot repoint a stale one.

## Commands

| Command | What it does |
|---|---|
| `claude-tabs` | Every window, tab and pane, grouped by window |
| `claude-tabs --claude-only` | Only the tabs running Claude |
| `claude-tabs --prompts` | Adds each session's first and last prompt |
| `claude-tabs --grep deploy` | Only tabs matching a regex over path, title, prompts, branch or session id |
| `claude-tabs --idle 60` | Only Claude sessions untouched for over 60 minutes |
| `claude-tabs --sort idle` | Stalest session first. Also `--sort path` or `--sort window` (default) |
| `claude-tabs --md` | Prints a Markdown table instead of the tree |
| `claude-tabs --json` | Prints the whole index as JSON |
| `claude-tabs --save f.json` | Writes the JSON index to `f.json` |
| `claude-tabs --jump 'auth refactor'` | Focuses the one tab matching the regex and flashes it orange |
| `claude-tabs --jump X --no-flash` | Jumps without the flash |
| `claude-tabs --jump X --flash-color 00c8ff` | Different flash colour (RRGGBB) |
| `claude-tabs --jump X --flash-seconds 2` | Longer flash (default 0.8s) |
| `claude-tabs --no-color` | Plain text, no ANSI colour |
| `claude-tabs --version` | Prints the version |

Flags combine: `--claude-only --idle 60 --sort idle` is the "what did I abandon"
list, and `--save` works alongside any view.

`--jump` refuses to guess. If the regex matches several tabs it lists them and
changes nothing (exit 2); no match exits 1.

## The jump flash, and how the colour gets put back

`--jump` briefly sets the target pane's background colour so you can see where
it landed, then restores it. Restoring exactly is harder than it looks, because
an AppleScript colour write quantizes: writing red `4273` reads back `4272`, and
`4274` reads back `4274`. The value `4273` is simply not reachable by writing,
yet that is exactly what every untouched pane here sits at. So writing a
recorded colour back is always at risk of landing a unit off, and repeating that
walks the pane darker on every jump.

The restore therefore goes through **OSC 111**, the terminal's own "reset
background to the profile default" escape, written to the pane's tty. There is
no value to round, so it is exact. Measured: four consecutive flashes each
returned the pane to `4273, 5020, 6015`, byte-identical to untouched panes.

The pane's colour is still recorded once, the first time that pane is ever
flashed, in `~/.cache/claude-tabs/flash-state.json`. It is used two ways. After
OSC 111, the restored colour is compared against the record; if they disagree
the pane had a real per-session colour rather than its profile default, so that
value is written back explicitly and the one-unit rounding is accepted. The
record is never refreshed, which is what stops the fallback path from drifting.

The record also makes the flash crash-safe. If the process is killed while the
pane is still tinted, the record stays marked active and the next `claude-tabs`
run of any kind restores the pane and clears it. Verified by leaving a pane
orange with an expired record: recovery returned it to exactly `4273, 5020,
6015`. Records for panes that no longer exist, or older than 30 days, are
dropped automatically.

Two things worth knowing if you extend this. Reading a colour immediately after
setting one returns a stale value; reads are only reliable once the change has
settled, which is why the restore confirmation waits. And `tab color` would be a
less invasive knob than repainting the background, but iTerm2 3.6.11 does not
expose it to AppleScript.

## How the join works

There is no single API that says "this tab is running that Claude session", so
the tool builds the link from four sources:

| Question | Source |
|---|---|
| What windows, tabs and panes exist? | iTerm2 AppleScript: `unique id`, `tty`, `name`, `path` variable |
| Which pane is a claude process in? | `ITERM_SESSION_ID` in the process environment (`ps eww`) equals the pane's `unique id` |
| What directory is it working in? | the process's own cwd via one `lsof -c claude -d cwd` call |
| Which transcript is it writing? | the `.jsonl` in `~/.claude/projects/<mangled-cwd>/` whose current `ai-title` equals the pane title |
| What is the session about? | that transcript's `ai-title` (Claude's own summary), plus first and last user prompt |

`~/.claude/projects` directory names are the cwd with `/`, `.` and `_` all
replaced by `-`.

## Why the transcript needs matching at all

No claude process holds its `.jsonl` open (verified: `lsof -c claude | grep -c
jsonl` returns 0), and the transcript records no pid or tty. So the file has to
be identified by content. Three things make the naive approaches wrong:

- **cwd in the transcript drifts.** It is re-recorded per entry and follows the
  session into subdirectories, so it often disagrees with the pane's cwd. It is
  used only as a tiebreak, never as a filter.
- **A resumed session is copied into several project directories.** One session
  id here existed in four. Claims are tracked by both path and session id so two
  panes cannot take two copies of the same session.
- **Busy directories defeat mtime.** One directory here holds 134 transcripts
  with 20 live sessions. Ranking by mtime alone assigns early tabs arbitrary
  files and every later tab inherits the error. Every exact title match is
  therefore settled first, everywhere, before any mtime guess is allowed.

## Match quality markers

The column after the title flags how confidently the transcript was identified.

| Marker | Meaning |
|---|---|
| (blank) | exact: pane title equals the transcript's current `ai-title` |
| `~` | mtime guess: newest unclaimed transcript for that directory, written since the process started |
| `?` | weak: newest unclaimed transcript, but not written since the process started |
| `!` | no transcript found (normal for a session that has had no prompt yet) |

`nested(ttysNNN)` after the metadata means the claude process is not on the
pane's own tty, which happens inside tmux: `ITERM_SESSION_ID` is inherited from
whichever pane started the tmux server, so the tab attribution is indirect.
Those tabs also lose title matching, because the tab name is tmux's, not
Claude's.

Measured on a 111-tab, 56-session setup: 55 matched by exact title and 1 by
mtime (a tmux tab, which has no Claude title to match), in 2.4s. An independent
cross-check of each process's tty against its pane agreed with the
`ITERM_SESSION_ID` join on all 55 direct sessions.

## Notes

- Window numbers are iTerm2's index, which reorders as windows are focused. The
  `id=` shown next to each window and used by `--jump` is stable.
- iTerm2 3.6.11 rejects `index of current tab of <window>`, so the selected tab
  is found by matching each pane against `current session of <window>` instead.
  That is the second AppleScript gap; `tab color` is the first.
- `--json` and `--save` include prompt text, so treat that output as
  conversation content rather than metadata.
- Requires Automation permission for iTerm2 for whichever terminal runs it. The
  first run raises the macOS prompt.
- A tab is reported as running Claude only while the process is alive. Closed
  sessions live on in `~/.claude/projects` but have no tab.
- A `claude` running inside tmux is attributed to whichever pane started the
  tmux server, and is marked `nested`. See the match quality markers above.

## Licence

MIT. See [LICENSE](LICENSE).
