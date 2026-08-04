#!/usr/bin/env python3
"""Index every iTerm2 window/tab, its working directory, and any Claude Code
session running in it, including what that session is about.

How the pieces are joined:
  * iTerm2 AppleScript gives the window/tab/pane tree, each pane's `unique id`,
    its tty, its title, and its shell cwd (`path` session variable).
  * Every `claude` process inherits ITERM_SESSION_ID from its pane, so
    `ps eww` on the claude pids joins process -> pane exactly.
  * The pane's cwd maps to a ~/.claude/projects/<mangled-path> directory.
  * Inside that directory, the session .jsonl carrying an `ai-title` equal to
    the pane title is that pane's transcript. No claude process holds its
    .jsonl open, so this title match (with an mtime fallback) is the join.

Stdlib only, macOS only.

Almost entirely read-only. Three exceptions: --jump selects a tab and briefly
tints it, --save writes the file you name, and any invocation may repaint a pane
and rewrite the flash-state cache if an earlier --jump was interrupted before it
could restore the colour. Transcripts are only ever read.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

__version__ = "1.2.0"

HOME = os.path.expanduser("~")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")

US = "\x1f"  # field separator in the AppleScript payload
RS = "\x1e"  # record separator

# Claude Code sets the pane title to "<spinner glyph> <ai title> (<process>)".
TITLE_RE = re.compile(r"^[\s✳⚙⠀-⣿●·*]*(.*?)\s*(?:\(([^()]*)\))?$")
ITERM_SESSION_RE = re.compile(r"ITERM_SESSION_ID=(?:[^:\s]*:)?([0-9A-Fa-f-]{36})")

# Per project directory, how many transcripts (newest first) to consider.
DIR_SCAN_LIMIT = 250
TAIL_BYTES = 512 * 1024
HEAD_BYTES = 256 * 1024


def run(cmd, timeout=90):
    """Run a command and return stdout ('' on any failure)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return p.stdout


# ============================================================ iTerm2 enumeration

APPLESCRIPT = """
on j(vals)
  set AppleScript's text item delimiters to (ASCII character 31)
  set s to vals as text
  set AppleScript's text item delimiters to ""
  return s
end j

tell application "iTerm2"
  set accum to {}
  set frontID to ""
  try
    set frontID to (id of current window) as text
  end try
  repeat with wi from 1 to count of windows
    set win to window wi
    -- iTerm2 3.6 rejects `index of current tab of <window>`, so identify the
    -- selected pane by its unique id instead and infer the selected tab.
    set curSessID to ""
    try
      set curSessID to (unique id of current session of win)
    end try
    repeat with ti from 1 to count of tabs of win
      set tb to tab ti of win
      repeat with si from 1 to count of sessions of tb
        set ss to session si of tb
        set thePath to ""
        set theTitle to ""
        set theCmd to ""
        set theTTY to ""
        set uid to ""
        set nm to ""
        tell ss
          try
            set thePath to (get variable named "path")
          end try
          try
            set theTitle to (get variable named "session.name")
          end try
          try
            set theCmd to (get variable named "commandLine")
          end try
          try
            set theTTY to (get tty)
          end try
          try
            set uid to (get unique id)
          end try
          try
            set nm to (get name)
          end try
        end tell
        set accum to accum & {my j({wi as text, (id of win) as text, ti as text, si as text, ¬
          uid, theTTY, nm, theTitle, thePath, theCmd, curSessID, frontID})}
      end repeat
    end repeat
  end repeat
  set AppleScript's text item delimiters to (ASCII character 30)
  set payload to accum as text
  set AppleScript's text item delimiters to ""
  return payload
end tell
"""


def iterm_panes():
    """One dict per iTerm2 split-pane session, in window/tab/pane order."""
    out = run(["osascript", "-e", APPLESCRIPT])
    if not out.strip():
        return []
    panes = []
    selected = {}
    for rec in out.split(RS):
        f = rec.split(US)
        if len(f) < 12:
            continue
        (win_idx, win_id, tab_idx, pane_idx, uid, tty, name, title,
         path, cmd, cur_pane, front) = f[:12]
        try:
            win_i, tab_i, pane_i = int(win_idx), int(tab_idx), int(pane_idx)
        except ValueError:
            continue
        if cur_pane.strip().upper() == uid.strip().upper():
            selected[win_id.strip()] = tab_i
        panes.append({
            "window_index": win_i,
            "window_id": win_id.strip(),
            "tab_index": tab_i,
            "pane_index": pane_i,
            "iterm_session_id": uid.strip().upper(),
            "tty": tty.strip(),
            "tab_name": name.strip(),
            "pane_title": title.strip(),
            "shell_path": path.strip(),
            "command_line": cmd.strip(),
            "is_selected_pane": cur_pane.strip().upper() == uid.strip().upper(),
            "is_front_window": win_id.strip() == front.strip(),
        })
    for pane in panes:
        pane["is_selected_tab"] = selected.get(pane["window_id"]) == pane["tab_index"]
    return panes


# ================================================================ jump flashing

# Where flashing records each pane's real background colour.
#
# The record is sticky: written the first time a pane is ever flashed and reused
# for every restore after that. Restoring normally goes through OSC 111, which
# is exact, but the record still matters for the fallback path, because an
# AppleScript colour write quantizes (writing red 4273 reads back 4272, and
# 4274 reads back 4274). Re-reading the pane on each flash would therefore walk
# it a unit darker every time; writing the same recorded value never does.
#
# It also means a flash killed mid-run is repaired on the next run rather than
# leaving the pane recoloured for good.
FLASH_STATE = os.path.join(HOME, ".cache", "claude-tabs", "flash-state.json")
FLASH_COLOR = "#ff8800"
FLASH_SECONDS = 0.8
FLASH_RECORD_TTL = 30 * 86400


def pane_ref(pane):
    return "session %d of tab %d of window id %s" % (
        pane["pane_index"], pane["tab_index"], pane["window_id"])


def parse_color(text):
    """'#ff8800' or 'ff8800' -> 16-bit AppleScript RGB."""
    s = (text or "").lstrip("#").strip()
    if len(s) != 6:
        raise ValueError("colour must be RRGGBB, got %r" % text)
    return [int(s[i:i + 2], 16) * 257 for i in (0, 2, 4)]


def read_background(pane):
    out = run(["osascript", "-e", 'tell application "iTerm2" to tell %s to '
               'get background color' % pane_ref(pane)], timeout=20)
    parts = [p.strip() for p in out.strip().split(",")]
    if len(parts) != 3:
        return None
    try:
        return [int(p) for p in parts]
    except ValueError:
        return None


def set_background(pane, rgb):
    run(["osascript", "-e",
         'tell application "iTerm2" to tell %s to set background color to {%d, %d, %d}'
         % ((pane_ref(pane),) + tuple(rgb))], timeout=20)


def reset_background(pane):
    """Ask the terminal to drop any background override (OSC 111).

    This is the only exact restore available. An AppleScript write quantizes:
    on this machine red 4273 is simply not reachable, since writing 4273 lands
    on 4272 and 4274 lands on 4274, so writing a recorded colour back always
    risks being a unit off. OSC 111 reverts the pane to its profile colour with
    no value to round.
    """
    tty = pane.get("tty") or ""
    if not tty.startswith("/dev/"):
        return False
    try:
        with open(tty, "w") as fh:
            fh.write("\033]111\007")
        return True
    except OSError:
        return False


def restore_background(pane, recorded, settle=0.15):
    """Put a pane's background back, exactly where that is possible.

    Prefer OSC 111 and confirm it landed on the recorded colour. If it did not,
    this pane had a genuine per-session colour rather than its profile default,
    so put that value back by hand and accept the rounding.
    """
    if reset_background(pane):
        time.sleep(settle)
        if read_background(pane) == recorded:
            return "profile"
    set_background(pane, recorded)
    return "rgb"


def load_flash_state():
    try:
        with open(FLASH_STATE) as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_flash_state(state):
    try:
        os.makedirs(os.path.dirname(FLASH_STATE), exist_ok=True)
        tmp = FLASH_STATE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, FLASH_STATE)
    except OSError:
        pass


def recover_flashes(panes):
    """Repair any flash that died mid-run, and drop records we no longer need."""
    state = load_flash_state()
    if not state:
        return
    live = {p["iterm_session_id"]: p for p in panes}
    now, changed = time.time(), False
    for guid, entry in list(state.items()):
        active = entry.get("active_until", 0)
        if active > now:
            continue  # a flash is still legitimately in flight
        if active and entry.get("color") and guid in live:
            restore_background(live[guid], entry["color"])
            entry["active_until"] = 0
            changed = True
        # The pane is gone, or the record is old enough that the user's own
        # colours have probably moved on since.
        if guid not in live or now - entry.get("seen", now) > FLASH_RECORD_TTL:
            del state[guid]
            changed = True
    if changed:
        save_flash_state(state)


def flash_pane(pane, color=FLASH_COLOR, seconds=FLASH_SECONDS):
    """Briefly tint a pane's background, then put its own colour back."""
    guid = pane["iterm_session_id"]
    try:
        rgb = parse_color(color)
    except ValueError:
        return False
    state = load_flash_state()
    # Reuse the recorded value if we have one. Reading the pane now would return
    # a value a unit off the last restore, or the flash colour itself if an
    # earlier flash was killed before it could restore.
    original = state.get(guid, {}).get("color")
    if not original:
        original = read_background(pane)
        if not original:
            return False
    state[guid] = {"color": original, "active_until": time.time() + seconds + 5,
                   "seen": time.time()}
    save_flash_state(state)
    set_background(pane, rgb)
    try:
        time.sleep(seconds)
    finally:
        # Ctrl-C during the flash must still put the colour back, and must clear
        # the active marker so the next run does not redo a restore that already
        # happened. The state record is the backstop if even this cannot run.
        restore_background(pane, original)
        state = load_flash_state()
        state[guid] = {"color": original, "active_until": 0, "seen": time.time()}
        save_flash_state(state)
    return True


# ========================================================== process enumeration


def all_processes():
    """pid -> {ppid, etime, tty, stat, command} for every visible process."""
    out = run(["ps", "-Ao", "pid=,ppid=,etime=,tty=,stat=,command="])
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        pid, ppid, etime, tty, stat, cmd = parts
        try:
            procs[int(pid)] = {"pid": int(pid), "ppid": int(ppid), "etime": etime,
                               "tty": tty, "stat": stat, "command": cmd}
        except ValueError:
            continue
    return procs


def parse_etime(etime):
    """ps elapsed time ('DD-HH:MM:SS', 'HH:MM:SS', 'MM:SS') -> seconds."""
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            days = 0
    bits = etime.split(":")
    try:
        nums = [int(b) for b in bits]
    except ValueError:
        return 0
    secs = 0
    for n in nums:
        secs = secs * 60 + n
    return days * 86400 + secs


def claude_pids(procs):
    """Live `claude` CLI pids, minus any nested under another claude."""
    pids = set()
    for pid, p in procs.items():
        argv = p["command"].split()
        if argv and os.path.basename(argv[0]) == "claude":
            pids.add(pid)
    top = set()
    for pid in pids:
        cur, depth, nested = procs[pid]["ppid"], 0, False
        while cur in procs and depth < 40:
            if cur in pids:
                nested = True
                break
            cur, depth = procs[cur]["ppid"], depth + 1
        if not nested:
            top.add(pid)
    return top


def process_pane_ids(pids):
    """pid -> iTerm2 pane GUID, from each process's ITERM_SESSION_ID."""
    if not pids:
        return {}
    # BSD-style `ps eww` (unhyphenated keys) is what dumps the environment on
    # macOS; `ps -e` would instead mean "all processes" and ignore -p.
    out = run(["ps", "eww", "-o", "pid=,command=", "-p",
               ",".join(str(p) for p in sorted(pids))])
    result = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s", line)
        if not m or int(m.group(1)) not in pids:
            continue
        g = ITERM_SESSION_RE.search(line)
        if g:
            result[int(m.group(1))] = g.group(1).upper()
    return result


def claude_cwds():
    """pid -> cwd for every claude process, in a single lsof call."""
    out = run(["lsof", "-c", "claude", "-d", "cwd", "-a", "-Fpn"])
    cwds, cur = {}, None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                cur = int(line[1:])
            except ValueError:
                cur = None
        elif line.startswith("n") and cur is not None:
            cwds.setdefault(cur, line[1:])
    return cwds


TMUX_PANE_FMT = US.join(["#{pane_tty}", "#{session_name}", "#{window_index}",
                         "#{pane_index}", "#{pane_active}", "#{window_active}",
                         "#{pane_title}"])
TMUX_CLIENT_FMT = US.join(["#{client_tty}", "#{client_session}"])


def short_tty(tty):
    return (tty or "").replace("/dev/", "")


def is_default_pane_title(title):
    """True when tmux is reporting its own fallback rather than a real title.

    tmux seeds pane_title with the machine's hostname, so a pane whose program
    has not set a title reports that. Measured on a scratch server: a pane
    running `sleep` reports the hostname verbatim. Letting that through would
    replace a tab's real name with the machine name, which is worse than not
    overriding at all.
    """
    host = socket.gethostname()
    return (title or "") in ("", host, host.split(".")[0])


def tmux_panes():
    """short tty -> tmux pane facts, for every pane on every tmux session.

    A claude inside tmux runs on a tmux pty, not on the iTerm pane's tty, so this
    is what connects the two. `pane_title` matters as much as the tty: tmux
    captures the title the program sets, so Claude's own session title is here
    even though the iTerm tab shows tmux's window name instead.
    """
    out = {}
    for line in run(["tmux", "list-panes", "-a", "-F", TMUX_PANE_FMT],
                    timeout=20).splitlines():
        f = line.split(US)
        if len(f) < 7:
            continue
        out[short_tty(f[0])] = {
            "session": f[1],
            "window": f[2],
            "pane": f[3],
            "visible": f[4] == "1" and f[5] == "1",
            "title": f[6],
        }
    return out


def tmux_clients():
    """tmux session name -> short ttys of the terminals attached to it."""
    out = {}
    for line in run(["tmux", "list-clients", "-F", TMUX_CLIENT_FMT],
                    timeout=20).splitlines():
        f = line.split(US)
        if len(f) < 2:
            continue
        out.setdefault(f[1], []).append(short_tty(f[0]))
    return out


# Strongest attribution route first. A claude on the pane's own tty is certain; a
# tmux client tty is nearly so; ITERM_SESSION_ID is a guess, because it is
# inherited and so names whichever pane started the nesting.
ROUTE_RANK = {"direct": 0, "tmux": 1, "env": 2}


def attribute_claudes(panes, procs, cpids, pane_of):
    """iTerm pane unique id -> [(pid, route, tmux facts or None)]."""
    by_tty = {short_tty(p["tty"]): p for p in panes}
    by_guid = {p["iterm_session_id"]: p for p in panes}
    # Consulted only when the direct route misses, so the common case where every
    # claude sits on its own pane's tty spawns no tmux processes at all.
    tpanes = tclients = None
    out = {}
    for pid in sorted(cpids):
        tty = procs[pid]["tty"]
        pane, route, tmux_info = by_tty.get(tty), "direct", None
        if pane is None and tpanes is None:
            tpanes, tclients = tmux_panes(), tmux_clients()
        if pane is None and tty in tpanes:
            facts = tpanes[tty]
            for client_tty in tclients.get(facts["session"], []):
                if client_tty in by_tty:
                    pane, route, tmux_info = by_tty[client_tty], "tmux", facts
                    break
        if pane is None:
            guid = pane_of.get(pid)
            pane, route, tmux_info = by_guid.get(guid or ""), "env", None
        if pane is None:
            continue
        out.setdefault(pane["iterm_session_id"], []).append((pid, route, tmux_info))
    return out


def tty_foreground(procs):
    """short tty -> command of that tty's foreground process."""
    fg = {}
    for p in procs.values():
        if p["tty"] in ("??", "-", "") or "+" not in p["stat"]:
            continue
        fg.setdefault(p["tty"], p["command"])
    return fg


# ======================================================== transcript inspection


def project_dir_for(cwd):
    """A working directory's ~/.claude/projects subdirectory name."""
    return re.sub(r"[/._]", "-", cwd)


def count_lines(path):
    n = 0
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    return n
                n += chunk.count(b"\n")
    except OSError:
        return 0


def message_text(message):
    """Flatten a transcript message's content to plain text."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(blk.get("text") or "")
            elif isinstance(blk, str):
                parts.append(blk)
        return "\n".join(parts)
    return ""


NOISE_PREFIXES = ("<system-reminder", "<command-name>", "<command-message>",
                  "<local-command-stdout>", "Caveat: The messages below",
                  "[Request interrupted", "<user-prompt-submit-hook>")


def is_real_prompt(text):
    """True for something the human actually typed."""
    t = (text or "").strip()
    return bool(t) and not t.startswith(NOISE_PREFIXES)


def iter_json_lines(blob):
    for raw in blob.split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def scan_tail(path):
    """Cheap per-file facts: title, ids, latest activity. Tail read only."""
    info = {"session_file": path, "session_id": None, "ai_title": None,
            "cwd": None, "git_branch": None, "version": None, "model": None,
            "permission_mode": None, "last_prompt": None, "first_prompt": None,
            "last_activity": None, "mtime": 0, "size_bytes": 0, "entries": None}
    try:
        st = os.stat(path)
    except OSError:
        return info
    info["mtime"], info["size_bytes"] = st.st_mtime, st.st_size

    def absorb(obj):
        t = obj.get("type")
        for key, field in (("sessionId", "session_id"), ("cwd", "cwd"),
                           ("gitBranch", "git_branch"), ("version", "version"),
                           ("timestamp", "last_activity")):
            if obj.get(key):
                info[field] = obj[key]
        if t == "ai-title" and obj.get("aiTitle"):
            info["ai_title"] = obj["aiTitle"]
        elif t == "permission-mode" and obj.get("permissionMode"):
            info["permission_mode"] = obj["permissionMode"]
        elif t == "assistant":
            model = (obj.get("message") or {}).get("model")
            if model:
                info["model"] = model
        elif t == "user":
            text = message_text(obj.get("message") or {})
            if is_real_prompt(text):
                info["last_prompt"] = text.strip()

    try:
        with open(path, "rb") as fh:
            if st.st_size > TAIL_BYTES:
                fh.seek(-TAIL_BYTES, os.SEEK_END)
                blob = fh.read()
                blob = blob.split(b"\n", 1)[1] if b"\n" in blob else b""
            else:
                blob = fh.read()
            for obj in iter_json_lines(blob):
                absorb(obj)
    except OSError:
        return info

    # ai-title recurs throughout a transcript, but a long tool-heavy tail can
    # push every occurrence out of the window. Transcripts reach hundreds of
    # megabytes, so scan the whole file with grep rather than in Python.
    if info["ai_title"] is None and st.st_size > TAIL_BYTES:
        # Claude writes compact JSON, but JSON allows whitespace after the
        # colon, so do not depend on its absence. Space and tab are the whole
        # surface: a newline cannot occur mid-record in JSONL.
        out = run(["grep", "-o", "-a", '"aiTitle":[[:space:]]*"[^"]*"', path],
                  timeout=120)
        lines = out.splitlines()
        if lines:
            try:
                info["ai_title"] = json.loads("{" + lines[-1] + "}")["aiTitle"]
            except (ValueError, KeyError):
                pass
    return info


def scan_head(info):
    """Fill in first_prompt and entry count. Called only for claimed files."""
    if info.get("entries") is not None:
        return info
    path = info["session_file"]
    info["entries"] = count_lines(path)
    try:
        with open(path, "rb") as fh:
            blob = fh.read(HEAD_BYTES)
    except OSError:
        return info
    for obj in iter_json_lines(blob):
        if obj.get("type") == "user":
            text = message_text(obj.get("message") or {})
            if is_real_prompt(text):
                info["first_prompt"] = text.strip()
                break
    return info


class TranscriptStore:
    """Lazily scans ~/.claude/projects, one directory at a time."""

    def __init__(self):
        self._dirs = {}
        self._files = {}
        self.claimed = set()
        self.claimed_ids = set()

    def listing(self, pdir):
        """[(mtime, path)] for one project directory, newest first, cached."""
        if pdir not in self._dirs:
            entries = []
            try:
                for entry in os.scandir(pdir):
                    if entry.name.endswith(".jsonl"):
                        try:
                            entries.append((entry.stat().st_mtime, entry.path))
                        except OSError:
                            pass
            except OSError:
                pass
            entries.sort(reverse=True)
            self._dirs[pdir] = entries
        return self._dirs[pdir]

    def files_for(self, cwd, since=0):
        pdir = os.path.join(PROJECTS_DIR, project_dir_for(cwd))
        return [p for mt, p in self.listing(pdir)
                if mt >= since][:DIR_SCAN_LIMIT]

    def all_files_since(self, since):
        """Every transcript under ~/.claude/projects touched since `since`."""
        out = []
        try:
            dirs = [e.path for e in os.scandir(PROJECTS_DIR) if e.is_dir()]
        except OSError:
            return []
        for pdir in dirs:
            out += [(mt, p) for mt, p in self.listing(pdir) if mt >= since]
        out.sort(reverse=True)
        return [p for _, p in out]

    def summary(self, path):
        if path not in self._files:
            self._files[path] = scan_tail(path)
        return self._files[path]

    def is_free(self, path):
        if path in self.claimed:
            return False
        sid = self.summary(path).get("session_id")
        return not (sid and sid in self.claimed_ids)

    def candidates(self, cwd):
        """Unclaimed transcripts for cwd, newest first.

        Deliberately no cwd filtering: a transcript's recorded cwd drifts as
        the session cd's into subdirectories, and a resumed session is copied
        into the project directory of every cwd it has been resumed from, so
        the recorded cwd routinely disagrees with the pane's. The pane title
        is the trustworthy key; cwd only breaks ties.
        """
        for path in self.files_for(cwd):
            if self.is_free(path):
                yield self.summary(path)

    def claim(self, info, row, match):
        # Every reachable state must be renderable. A state with no MATCH_MARK
        # entry falls through to a blank, which reads as an exact local match and
        # hides the difference, so enforce it at the one funnel all states pass
        # through rather than restating the list somewhere it can drift.
        assert match in MATCH_MARK, "match state %r has no MATCH_MARK entry" % match
        self.claimed.add(info["session_file"])
        if info.get("session_id"):
            self.claimed_ids.add(info["session_id"])
        row["claude"]["session"] = info
        row["claude"]["match"] = match

    def titles_for(self, files):
        """title -> [paths] for these transcripts' current titles.

        A session's title is regenerated as the conversation moves on, so only
        the last ai-title in a transcript is what its pane shows now. Tail
        reads are capped, which is what keeps this cheap: grepping the same
        directory means reading every byte of transcripts that run to hundreds
        of megabytes.
        """
        index = {}
        for path in files:
            title = self.summary(path).get("ai_title")
            if title:
                index.setdefault(title, []).append(path)
        return index

    def pick(self, paths, pane_cwd):
        """Best unclaimed transcript among paths: same project dir, then newest."""
        want_dir = os.path.join(PROJECTS_DIR, project_dir_for(pane_cwd))
        best, best_rank = None, None
        for path in paths:
            if not self.is_free(path):
                continue
            info = self.summary(path)
            rank = (0 if os.path.dirname(path) == want_dir else 1,
                    0 if info.get("cwd") == pane_cwd else 1,
                    -info["mtime"])
            if best_rank is None or rank < best_rank:
                best, best_rank = info, rank
        return best




# ==================================================================== assembly


def clean_title(tab_name):
    """'* Review PR (claude)' -> ('Review PR', 'claude')."""
    m = TITLE_RE.match(tab_name or "")
    if not m:
        return (tab_name or "").strip(), ""
    return (m.group(1) or "").strip(), (m.group(2) or "").strip()


def tilde(path):
    return "~" + path[len(HOME):] if path and path.startswith(HOME) else (path or "")


def build_index():
    procs = all_processes()
    cpids = claude_pids(procs)
    pane_of = process_pane_ids(cpids)
    cwd_of = claude_cwds()
    fg = tty_foreground(procs)
    panes = iterm_panes()
    recover_flashes(panes)

    by_pane = attribute_claudes(panes, procs, cpids, pane_of)

    rows = []
    for pane in panes:
        title, hint = clean_title(pane["tab_name"])
        row = dict(pane)
        row["title"] = title
        row["foreground_hint"] = hint
        row["foreground_command"] = fg.get(short_tty(pane["tty"]), "")
        row["claude"] = None
        found = by_pane.get(pane["iterm_session_id"], [])
        if found:
            # One iTerm pane can host many tmux panes but shows one at a time,
            # so the visible one is the session this tab is actually displaying.
            found.sort(key=lambda r: (ROUTE_RANK[r[1]],
                                      0 if (r[2] or {}).get("visible") else 1,
                                      r[0]))
            pid, route, tmux_info = found[0]
            row["cwd"] = cwd_of.get(pid) or pane["shell_path"]
            if tmux_info and not is_default_pane_title(tmux_info.get("title")):
                # The iTerm tab name is tmux's window name, which says nothing
                # about the session. tmux kept the title Claude set, so use it
                # and title matching works inside tmux too.
                row["title"] = clean_title(tmux_info["title"])[0] or row["title"]
            row["claude"] = {
                "pid": pid,
                "extra_pids": [r[0] for r in found[1:]],
                "attached": route,
                "process_tty": procs[pid]["tty"],
                "tmux": tmux_info,
                "uptime": procs[pid]["etime"],
                "started_at": time.time() - parse_etime(procs[pid]["etime"]),
                "command": procs[pid]["command"],
                "match": "none",
                "session": None,
            }
        else:
            row["cwd"] = pane["shell_path"]
        rows.append(row)

    resolve_transcripts(rows)
    return rows


def resolve_transcripts(rows, use_global=True, slack=300):
    """Attach a transcript to each Claude pane.

    Three phases, in this order on purpose. Every exact-title match anywhere is
    settled before any mtime guess may claim a file, otherwise one directory
    holding 20 live sessions hands its early tabs arbitrary transcripts and
    every later tab inherits the mistake.

      1. title, local   - pane title == the current ai-title of a transcript in
                          the pane cwd's project directory
      2. title, global  - same, across every recently touched project
                          directory, for sessions resumed from elsewhere
      3. mtime          - newest unclaimed transcript for that cwd

    Scanning is bounded by process start: a running session writes its
    transcript on the first prompt, so a file untouched since the claude process
    started cannot be that session's.
    """
    store = TranscriptStore()
    claude_rows = [r for r in rows if r["claude"]]
    if not claude_rows:
        return
    groups = {}
    for row in claude_rows:
        groups.setdefault(row["cwd"], []).append(row)

    def assign(rows_todo, index, label):
        for row in rows_todo:
            if row["claude"]["session"] or not row["title"]:
                continue
            best = store.pick(index.get(row["title"], []), row["cwd"])
            if best is not None:
                store.claim(best, row, label)

    for cwd, group in groups.items():
        since = min(r["claude"]["started_at"] for r in group) - slack
        assign(group, store.titles_for(store.files_for(cwd, since)), "title")

    pending = [r for r in claude_rows
               if not r["claude"]["session"] and r["title"]]
    if use_global and pending:
        since = min(r["claude"]["started_at"] for r in pending) - slack
        assign(pending, store.titles_for(store.all_files_since(since)),
               "title-global")

    for row in claude_rows:
        if row["claude"]["session"]:
            continue
        # Prefer a transcript written since the process started; a pane whose
        # session has had no prompt yet legitimately has no transcript at all.
        started = row["claude"]["started_at"]
        best, best_rank = None, None
        for info in store.candidates(row["cwd"]):
            rank = (0 if info["mtime"] >= started - slack else 1, -info["mtime"])
            if best_rank is None or rank < best_rank:
                best, best_rank = info, rank
        if best is not None:
            store.claim(best, row, "mtime" if best_rank[0] == 0 else "weak")

    for row in claude_rows:
        if row["claude"]["session"]:
            scan_head(row["claude"]["session"])


# ================================================================ presentation


def ago(ts):
    if not ts:
        return ""
    d = max(0, int(time.time() - ts))
    if d < 60:
        return "%ds" % d
    if d < 3600:
        return "%dm" % (d // 60)
    if d < 86400:
        return "%dh%02dm" % (d // 3600, (d % 3600) // 60)
    return "%dd%02dh" % (d // 86400, (d % 86400) // 3600)


class Paint:
    def __init__(self, on):
        self.on = on

    def __call__(self, code, text):
        return "\033[%sm%s\033[0m" % (code, text) if self.on else text


def term_width(default=170):
    try:
        import shutil
        return shutil.get_terminal_size((default, 40)).columns
    except Exception:
        return default


def trunc(text, width):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= width else text[:max(1, width - 1)] + "…"


# Every match state resolve_transcripts can set needs an entry here. A state
# missing from this map renders as a blank, which reads as an exact local match
# and hides the difference.
MATCH_MARK = {"title": " ", "title-global": "*", "mtime": "~", "weak": "?",
              "none": "!"}


def print_tree(rows, paint, show_prompts=False):
    width = term_width()
    path_w = 38 if width < 150 else 44
    title_w = max(24, width - path_w - 46)

    windows = {}
    for r in rows:
        windows.setdefault((r["window_index"], r["window_id"]), []).append(r)
    tabs = len({(r["window_index"], r["tab_index"]) for r in rows})
    n_claude = sum(1 for r in rows if r["claude"])
    print(paint("1", "%d windows · %d tabs · %d Claude sessions" %
                (len(windows), tabs, n_claude)))

    for (wi, wid), items in sorted(windows.items()):
        front = paint("32", "  ← front") if items[0]["is_front_window"] else ""
        print()
        print(paint("1;36", "Window %d" % wi) +
              paint("90", "  id=%s  %d tabs  %d claude" %
                    (wid, len(items), sum(1 for r in items if r["claude"]))) + front)
        for r in sorted(items, key=lambda x: (x["tab_index"], x["pane_index"])):
            label = "%3d" % r["tab_index"]
            if r["pane_index"] > 1:
                label += ".%d" % r["pane_index"]
            cursor = paint("32", "▸") if r["is_selected_tab"] else " "
            path = trunc(tilde(r["cwd"]), path_w).ljust(path_w)
            cl = r["claude"]
            if not cl:
                cmd = trunc(r["foreground_command"] or r["foreground_hint"], title_w)
                print(" %s %s %s %s" % (cursor, paint("90", label),
                                        paint("90", path), paint("90", cmd)))
                continue
            sess = cl["session"]
            title = trunc((sess or {}).get("ai_title") or r["title"] or "(untitled)", title_w)
            if sess:
                meta = "idle %-6s %5d entries  %s" % (
                    ago(sess["mtime"]), sess["entries"] or 0,
                    (sess["session_id"] or "?")[:8])
                if sess.get("git_branch") and sess["git_branch"] != "HEAD":
                    meta += " " + trunc(sess["git_branch"], 28)
            else:
                meta = "no transcript on disk yet"
            if cl["attached"] == "tmux" and cl["tmux"]:
                t = cl["tmux"]
                meta += "  tmux %s:%s.%s%s" % (
                    trunc(t["session"], 18), t["window"], t["pane"],
                    "" if t["visible"] else " (hidden)")
            elif cl["attached"] == "env":
                meta += "  env-linked(%s)" % cl["process_tty"]
            print(" %s %s %s %s%s %s" % (
                cursor, paint("33", label), paint("36", path),
                paint("1", title.ljust(title_w)),
                paint("31", MATCH_MARK.get(cl["match"], " ")), paint("90", meta)))
            if show_prompts and sess:
                for tag, key in (("first", "first_prompt"), (" last", "last_prompt")):
                    if sess.get(key):
                        print("       %s %s" % (paint("90", tag + ":"),
                                               trunc(sess[key], width - 15)))


def print_markdown(rows):
    print("| Win | Tab | Directory | Claude session | Idle | Entries | Branch | Session |")
    print("|----:|----:|---|---|---|----:|---|---|")
    for r in sorted(rows, key=lambda x: (x["window_index"], x["tab_index"], x["pane_index"])):
        cl = r["claude"]
        sess = (cl or {}).get("session") or {}
        if cl:
            what = sess.get("ai_title") or r["title"] or "(untitled)"
        else:
            cmd = r["foreground_command"].split()[0] if r["foreground_command"] else "shell"
            # A login shell shows up as "-zsh"; the dash is not part of its name.
            what = "_%s_" % os.path.basename(cmd).lstrip("-")
        branch = sess.get("git_branch") or ""
        print("| %d | %d | `%s` | %s | %s | %s | %s | `%s` |" % (
            r["window_index"], r["tab_index"], tilde(r["cwd"]) or "?", what,
            ago(sess.get("mtime")) if sess else "", sess.get("entries") or "",
            "" if branch == "HEAD" else branch, (sess.get("session_id") or "")[:8]))


def compile_pattern(pattern, flag):
    """Compile a user-supplied regex, or exit with a readable message."""
    try:
        return re.compile(pattern, re.I)
    except re.error as exc:
        print("%s: invalid regular expression %r: %s" % (flag, pattern, exc),
              file=sys.stderr)
        raise SystemExit(2)


def index_payload(rows, now=None):
    """The JSON structure written by --json and --save."""
    return {"generated_at": time.time() if now is None else now, "tabs": rows}


def session_mtime(row):
    return ((row["claude"] or {}).get("session") or {}).get("mtime") or 0


def select_rows(rows, claude_only=False, grep=None, idle_minutes=None,
                sort="window", now=None):
    """Apply the view filters and ordering.

    Returns a new list; the caller's is never reordered. Kept separate from
    main() so the filtering and sort rules can be tested directly.
    """
    view = list(rows)
    if claude_only:
        view = [r for r in view if r["claude"]]
    if grep is not None:
        view = [r for r in view if matches(r, grep)]
    if idle_minutes is not None:
        cutoff = (time.time() if now is None else now) - idle_minutes * 60
        # Idle only means anything for a session with a transcript to age.
        view = [r for r in view
                if r["claude"] and session_mtime(r) < cutoff]
    if sort == "idle":
        view.sort(key=session_mtime)
    elif sort == "path":
        view.sort(key=lambda r: (r["cwd"] or "", r["window_index"],
                                 r["tab_index"]))
    return view


def matches(row, rx):
    sess = (row["claude"] or {}).get("session") or {}
    hay = (row["cwd"], row["tab_name"], row["title"], sess.get("ai_title"),
           sess.get("first_prompt"), sess.get("last_prompt"),
           sess.get("session_id"), sess.get("git_branch"))
    return any(h and rx.search(h) for h in hay)


def jump(rows, pattern, flash=True, color=FLASH_COLOR, seconds=FLASH_SECONDS):
    rx = compile_pattern(pattern, "--jump")
    hits = [r for r in rows if matches(r, rx)]
    if not hits:
        print("no tab matches %r" % pattern, file=sys.stderr)
        return 1
    if len(hits) > 1 and len([r for r in hits if r["claude"]]) == 1:
        hits = [r for r in hits if r["claude"]]
    if len(hits) > 1:
        print("ambiguous: %d matches" % len(hits), file=sys.stderr)
        for r in hits[:20]:
            sess = (r["claude"] or {}).get("session") or {}
            print("  w%d t%d  %-40s  %s" % (
                r["window_index"], r["tab_index"], tilde(r["cwd"]),
                sess.get("ai_title") or r["title"]), file=sys.stderr)
        return 2
    r = hits[0]
    run(["osascript", "-e",
         'tell application "iTerm2"\n'
         '  tell window id %s\n    select\n'
         '    tell tab %d\n      select\n'
         '      tell session %d to select\n    end tell\n'
         '  end tell\n  activate\nend tell' %
         (r["window_id"], r["tab_index"], r["pane_index"])])
    sess = (r["claude"] or {}).get("session") or {}
    print("window %d tab %d  %s  %s" % (r["window_index"], r["tab_index"],
                                        tilde(r["cwd"]),
                                        sess.get("ai_title") or r["title"] or ""))
    if flash and seconds > 0 and not flash_pane(r, color, seconds):
        print("(could not flash the pane; the jump itself worked)", file=sys.stderr)
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Index iTerm2 windows, tabs, working directories and "
                    "Claude Code sessions.")
    ap.add_argument("--json", action="store_true", help="emit the index as JSON")
    ap.add_argument("--md", action="store_true", help="emit a Markdown table")
    ap.add_argument("--claude-only", action="store_true", help="only tabs running Claude")
    ap.add_argument("--prompts", action="store_true",
                    help="show each session's first and last user prompt")
    ap.add_argument("--grep", metavar="RE",
                    help="filter by regex over path, title, prompts, branch, id")
    ap.add_argument("--idle", type=float, metavar="MIN",
                    help="only Claude sessions idle longer than MIN minutes")
    ap.add_argument("--sort", choices=("window", "idle", "path"), default="window")
    ap.add_argument("--jump", metavar="RE", help="activate the one tab matching RE")
    ap.add_argument("--no-flash", action="store_true",
                    help="with --jump, do not tint the target pane")
    ap.add_argument("--flash-color", metavar="RRGGBB", default=FLASH_COLOR,
                    help="flash colour (default %s)" % FLASH_COLOR)
    ap.add_argument("--flash-seconds", type=float, default=FLASH_SECONDS,
                    metavar="S", help="flash duration (default %s)" % FLASH_SECONDS)
    ap.add_argument("--save", metavar="PATH", help="write the full JSON index to PATH")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--version", action="version",
                    version="%(prog)s " + __version__)
    args = ap.parse_args()

    rows = build_index()
    if not rows:
        print("No iTerm2 panes found. Is iTerm2 running, and does this terminal "
              "have Automation permission for it?", file=sys.stderr)
        return 1

    if args.jump:
        return jump(rows, args.jump, flash=not args.no_flash,
                    color=args.flash_color, seconds=args.flash_seconds)

    # Deliberately the whole index, not the filtered view: --save is for handing
    # the complete picture to another tool, so a filter on the terminal output
    # does not silently truncate the file. --json does follow the filters.
    if args.save:
        with open(args.save, "w") as fh:
            json.dump(index_payload(rows), fh, indent=2)

    view = select_rows(rows, claude_only=args.claude_only,
                       grep=compile_pattern(args.grep, "--grep") if args.grep else None,
                       idle_minutes=args.idle, sort=args.sort)

    if args.json:
        json.dump(index_payload(view), sys.stdout, indent=2)
        print()
    elif args.md:
        print_markdown(view)
    else:
        print_tree(view, Paint(sys.stdout.isatty() and not args.no_color),
                   show_prompts=args.prompts)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
