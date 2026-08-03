#!/usr/bin/env python3
"""Tests for claude_tabs.

Hermetic: no iTerm2, no AppleScript, no network, and nothing read from or written
to the real ~/.claude or ~/.cache. Anything that would shell out is either
pointed at a temporary directory or replaced.

Run with:  python3 -m unittest -v
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_tabs as ct  # noqa: E402


US, RS = ct.US, ct.RS


def pane_payload(rows):
    """Build the AppleScript payload that iterm_panes() parses.

    Each row is (win_idx, win_id, tab, pane, uid, tty, name, title, path, cmd,
    current_pane_uid, front_win_id).
    """
    return RS.join(US.join(str(f) for f in row) for row in rows)


class TestPureHelpers(unittest.TestCase):
    def test_project_dir_mangling(self):
        self.assertEqual(ct.project_dir_for("/Users/x/Documents/foo"),
                         "-Users-x-Documents-foo")
        # Slashes, dots and underscores all collapse to a dash, which is why
        # /a/b and /a-b and /a.b all land in the same directory.
        self.assertEqual(ct.project_dir_for("/a/b_c.d"), "-a-b-c-d")
        self.assertEqual(ct.project_dir_for("/a/b"), ct.project_dir_for("/a-b"))

    def test_clean_title_strips_spinner_and_process(self):
        self.assertEqual(ct.clean_title("✳ Review PR (claude)"),
                         ("Review PR", "claude"))
        self.assertEqual(ct.clean_title("⠂ Audit tokens (caffeinate)"),
                         ("Audit tokens", "caffeinate"))
        self.assertEqual(ct.clean_title("~/Documents (-zsh)"),
                         ("~/Documents", "-zsh"))
        self.assertEqual(ct.clean_title("proj (tmux)"), ("proj", "tmux"))
        self.assertEqual(ct.clean_title(""), ("", ""))

    def test_clean_title_keeps_inner_parentheses(self):
        title, proc = ct.clean_title("✳ Fix thing (again) (claude)")
        self.assertEqual((title, proc), ("Fix thing (again)", "claude"))

    def test_parse_etime(self):
        self.assertEqual(ct.parse_etime("05:10"), 310)
        self.assertEqual(ct.parse_etime("01:00:00"), 3600)
        self.assertEqual(ct.parse_etime("2-03:00:00"), 2 * 86400 + 3 * 3600)
        self.assertEqual(ct.parse_etime("garbage"), 0)

    def test_parse_color(self):
        self.assertEqual(ct.parse_color("#ff8800"), [65535, 34952, 0])
        self.assertEqual(ct.parse_color("ff8800"), ct.parse_color("#ff8800"))
        for bad in ("", "#fff", "nonsense", None):
            with self.assertRaises(ValueError):
                ct.parse_color(bad)

    def test_message_text_shapes(self):
        self.assertEqual(ct.message_text({"content": "hi"}), "hi")
        self.assertEqual(ct.message_text(
            {"content": [{"type": "text", "text": "a"},
                         {"type": "tool_use", "name": "Bash"},
                         {"type": "text", "text": "b"}]}), "a\nb")
        self.assertEqual(ct.message_text({}), "")
        self.assertEqual(ct.message_text(None), "")

    def test_is_real_prompt_rejects_injected_content(self):
        self.assertTrue(ct.is_real_prompt("check the registrar"))
        for noise in ("<system-reminder>x</system-reminder>",
                      "<command-name>/foo</command-name>",
                      "Caveat: The messages below were generated",
                      "[Request interrupted by user]",
                      "   ", ""):
            self.assertFalse(ct.is_real_prompt(noise), noise)

    def test_tilde_and_trunc_and_ago(self):
        self.assertEqual(ct.tilde(os.path.join(ct.HOME, "Documents")),
                         "~/Documents")
        self.assertEqual(ct.tilde("/opt/thing"), "/opt/thing")
        self.assertEqual(ct.tilde(None), "")
        self.assertEqual(ct.trunc("a  b\n c", 40), "a b c")
        self.assertTrue(ct.trunc("x" * 50, 10).endswith("…"))
        self.assertEqual(len(ct.trunc("x" * 50, 10)), 10)
        now = time.time()
        self.assertEqual(ct.ago(now), "0s")
        self.assertEqual(ct.ago(now - 120), "2m")
        self.assertEqual(ct.ago(now - 7200), "2h00m")
        self.assertEqual(ct.ago(None), "")

    def test_compile_pattern_exits_on_bad_regex(self):
        self.assertTrue(ct.compile_pattern("ab.", "--grep").search("xabc"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                ct.compile_pattern("[unclosed", "--grep")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--grep", err.getvalue())
        self.assertIn("invalid regular expression", err.getvalue())


class TestPaneParsing(unittest.TestCase):
    def setUp(self):
        self._run = ct.run

    def tearDown(self):
        ct.run = self._run

    def _panes(self, rows):
        ct.run = lambda *a, **k: pane_payload(rows)
        return ct.iterm_panes()

    def test_parses_tree_and_marks_selection(self):
        panes = self._panes([
            (1, "100", 1, 1, "AAA", "/dev/ttys001", "✳ Work (claude)", "t",
             "/Users/x/a", "claude", "BBB", "100"),
            (1, "100", 2, 1, "BBB", "/dev/ttys002", "~ (-zsh)", "t",
             "/Users/x/b", "-zsh", "BBB", "100"),
            (2, "200", 1, 1, "CCC", "/dev/ttys003", "~ (-zsh)", "t",
             "/Users/x/c", "-zsh", "", "100"),
        ])
        self.assertEqual(len(panes), 3)
        self.assertEqual(panes[0]["window_id"], "100")
        self.assertEqual(panes[0]["iterm_session_id"], "AAA")
        self.assertTrue(panes[0]["is_front_window"])
        self.assertFalse(panes[2]["is_front_window"])
        # Tab 2 holds the current pane, so tab 2 is the selected tab and tab 1
        # is not, even though tab 1 was listed first.
        self.assertFalse(panes[0]["is_selected_tab"])
        self.assertTrue(panes[1]["is_selected_tab"])
        self.assertTrue(panes[1]["is_selected_pane"])
        # A window whose current pane could not be read has no selected tab.
        self.assertFalse(panes[2]["is_selected_tab"])

    def test_skips_malformed_records(self):
        good = (1, "100", 1, 1, "AAA", "/dev/ttys001", "n", "t", "/p", "c", "AAA", "100")
        short = ("only", "three", "fields")
        bad_ints = ("x", "100", "y", "z", "AAA", "/dev/ttys002", "n", "t", "/p", "c", "AAA", "100")
        panes = self._panes([good, short, bad_ints])
        self.assertEqual(len(panes), 1)

    def test_empty_output_is_not_an_error(self):
        ct.run = lambda *a, **k: ""
        self.assertEqual(ct.iterm_panes(), [])


class TestProcessParsing(unittest.TestCase):
    def setUp(self):
        self._run = ct.run

    def tearDown(self):
        ct.run = self._run

    def test_claude_pids_drops_nested_processes(self):
        procs = {
            1: {"pid": 1, "ppid": 0, "command": "/bin/zsh", "tty": "s1",
                "stat": "Ss", "etime": "01:00"},
            2: {"pid": 2, "ppid": 1, "command": "claude", "tty": "s1",
                "stat": "S+", "etime": "01:00"},
            # A headless `claude -p` spawned by the session above.
            3: {"pid": 3, "ppid": 2, "command": "claude -p summarize",
                "tty": "s1", "stat": "S", "etime": "00:05"},
            4: {"pid": 4, "ppid": 1, "command": "/usr/bin/python3 x.py",
                "tty": "s1", "stat": "S", "etime": "00:05"},
            5: {"pid": 5, "ppid": 0, "command": "/opt/bin/claude", "tty": "s2",
                "stat": "S+", "etime": "10:00"},
        }
        self.assertEqual(ct.claude_pids(procs), {2, 5})

    def test_pane_ids_read_from_environment(self):
        guid = "73A553A5-7153-477D-BE15-B35348BE54F2"
        ct.run = lambda *a, **k: (
            "  57190 claude TERM=xterm ITERM_SESSION_ID=w0t5p0:%s SHELL=/bin/zsh\n"
            "  99999 claude TERM=xterm SHELL=/bin/zsh\n" % guid)
        got = ct.process_pane_ids({57190, 99999})
        # The w0t5p0 prefix is stale once a tab moves, so only the GUID is used,
        # and a process without the variable is simply absent.
        self.assertEqual(got, {57190: guid})

    def test_pane_ids_no_pids_skips_the_call(self):
        ct.run = lambda *a, **k: self.fail("should not shell out")
        self.assertEqual(ct.process_pane_ids(set()), {})

    def test_tty_foreground_picks_the_foreground_group(self):
        procs = {
            1: {"pid": 1, "ppid": 0, "command": "-zsh", "tty": "s1",
                "stat": "Ss", "etime": "1"},
            2: {"pid": 2, "ppid": 1, "command": "claude", "tty": "s1",
                "stat": "S+", "etime": "1"},
            3: {"pid": 3, "ppid": 0, "command": "launchd", "tty": "??",
                "stat": "Ss", "etime": "1"},
        }
        self.assertEqual(ct.tty_foreground(procs), {"s1": "claude"})


class TranscriptFixture(unittest.TestCase):
    """Base class giving each test its own fake ~/.claude/projects."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._projects = ct.PROJECTS_DIR
        ct.PROJECTS_DIR = os.path.join(self.tmp, "projects")
        os.makedirs(ct.PROJECTS_DIR)

    def tearDown(self):
        ct.PROJECTS_DIR = self._projects
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_transcript(self, cwd, session_id, ai_title=None, prompts=(),
                         mtime=None, branch="main", recorded_cwd=None,
                         compact=False):
        pdir = os.path.join(ct.PROJECTS_DIR, ct.project_dir_for(cwd))
        os.makedirs(pdir, exist_ok=True)
        path = os.path.join(pdir, session_id + ".jsonl")
        rows = []
        for text in prompts:
            rows.append({"type": "user", "sessionId": session_id,
                         "cwd": recorded_cwd or cwd, "gitBranch": branch,
                         "version": "2.1.0", "timestamp": "2026-08-01T00:00:00Z",
                         "message": {"role": "user", "content": text}})
            rows.append({"type": "assistant", "sessionId": session_id,
                         "cwd": recorded_cwd or cwd,
                         "message": {"model": "claude-opus-5", "content": "ok"}})
        if ai_title:
            rows.append({"type": "ai-title", "aiTitle": ai_title,
                         "sessionId": session_id})
        seps = (",", ":") if compact else (", ", ": ")
        with open(path, "w") as fh:
            for row in rows:
                fh.write(json.dumps(row, separators=seps) + "\n")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    @staticmethod
    def claude_row(cwd, title, started_at=None, tab=1):
        return {
            "window_index": 1, "window_id": "100", "tab_index": tab,
            "pane_index": 1, "iterm_session_id": "GUID%d" % tab,
            "tty": "/dev/ttys00%d" % tab, "tab_name": title, "title": title,
            "cwd": cwd, "shell_path": cwd, "foreground_command": "claude",
            "foreground_hint": "claude", "pane_title": title,
            "command_line": "claude", "is_selected_tab": False,
            "is_selected_pane": False, "is_front_window": False,
            "claude": {"pid": 1000 + tab, "extra_pids": [], "attached": "direct",
                       "process_tty": "s%d" % tab, "uptime": "10:00",
                       "started_at": started_at or (time.time() - 600),
                       "command": "claude", "match": "none", "session": None},
        }


class TestTranscriptScanning(TranscriptFixture):
    def test_scan_tail_extracts_the_session_facts(self):
        path = self.write_transcript("/Users/x/proj", "s1", "Fix the parser",
                                     ["first thing", "second thing"],
                                     branch="feature/x")
        info = ct.scan_tail(path)
        self.assertEqual(info["ai_title"], "Fix the parser")
        self.assertEqual(info["session_id"], "s1")
        self.assertEqual(info["git_branch"], "feature/x")
        self.assertEqual(info["model"], "claude-opus-5")
        self.assertEqual(info["last_prompt"], "second thing")
        self.assertEqual(info["version"], "2.1.0")

    def test_scan_head_adds_first_prompt_and_entry_count(self):
        path = self.write_transcript("/Users/x/proj", "s1", "T",
                                     ["alpha", "beta"])
        info = ct.scan_head(ct.scan_tail(path))
        self.assertEqual(info["first_prompt"], "alpha")
        self.assertEqual(info["entries"], ct.count_lines(path))
        self.assertEqual(info["entries"], 5)  # 2 prompts + 2 replies + title

    def test_ai_title_found_by_grep_when_tail_window_misses_it(self):
        # A long tool-heavy tail can push every ai-title out of the window, so
        # scan_tail falls back to grepping the whole file.
        path = self.write_transcript("/Users/x/proj", "s1", "Buried title",
                                     ["p"])
        self._append_filler(path, "s1")
        compact = self.write_transcript("/Users/x/proj2", "s2", "Compact title",
                                        ["p"], compact=True)
        self._append_filler(compact, "s2")
        info = self._scan_with_small_tail(path)
        info_compact = self._scan_with_small_tail(compact)
        # JSON permits whitespace after the colon; Claude omits it. Both must work.
        self.assertEqual(info["ai_title"], "Buried title")
        self.assertEqual(info_compact["ai_title"], "Compact title")

    @staticmethod
    def _append_filler(path, session_id, lines=200):
        """Pad a transcript so its ai-title falls outside the tail window."""
        with open(path, "a") as fh:
            for _ in range(lines):
                fh.write(json.dumps({"type": "system", "sessionId": session_id,
                                     "filler": "x" * 200}) + "\n")

    def _buried_title_file(self, name, title_fragment):
        """A file whose only ai-title sits outside the tail window."""
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write('{"type":"user","sessionId":"s",'
                     '"message":{"content":"p"}}\n')
            fh.write('{"type":"ai-title",%s,"sessionId":"s"}\n' % title_fragment)
        self._append_filler(path, "s")
        return path

    def _scan_with_small_tail(self, path):
        original = ct.TAIL_BYTES
        try:
            ct.TAIL_BYTES = 1024
            return ct.scan_tail(path)
        finally:
            ct.TAIL_BYTES = original

    def test_ai_title_grep_tolerates_a_tab_after_the_colon(self):
        # JSON permits any whitespace after the colon. Claude uses none, but the
        # fallback must not depend on that.
        path = self._buried_title_file("tabbed.jsonl", '"aiTitle":\t"Tabbed title"')
        self.assertEqual(self._scan_with_small_tail(path)["ai_title"],
                         "Tabbed title")

    def test_title_with_an_escaped_quote_fails_softly(self):
        # Known limit: the fallback pattern's [^"]* stops at the escaped quote,
        # so the title is not recovered. What matters is that it degrades to
        # "no title" and lets the mtime fallback take over, rather than raising.
        path = self._buried_title_file("escaped.jsonl",
                                       '"aiTitle":"He said \\"hi\\" loudly"')
        self.assertIsNone(self._scan_with_small_tail(path)["ai_title"])

    def test_count_lines_and_missing_files(self):
        path = self.write_transcript("/Users/x/proj", "s1", "T", ["a"])
        self.assertEqual(ct.count_lines(path), 3)
        self.assertEqual(ct.count_lines(os.path.join(self.tmp, "nope.jsonl")), 0)
        missing = ct.scan_tail(os.path.join(self.tmp, "nope.jsonl"))
        self.assertIsNone(missing["ai_title"])
        self.assertEqual(missing["mtime"], 0)

    def test_malformed_lines_are_skipped(self):
        path = self.write_transcript("/Users/x/proj", "s1", "Good", ["p"])
        with open(path, "a") as fh:
            fh.write("not json at all\n{\"unterminated\": \n\n")
        self.assertEqual(ct.scan_tail(path)["ai_title"], "Good")


class TestStore(TranscriptFixture):
    def test_files_for_is_newest_first_and_bounded_by_since(self):
        now = time.time()
        self.write_transcript("/Users/x/p", "old", "Old", ["a"], mtime=now - 9000)
        self.write_transcript("/Users/x/p", "new", "New", ["a"], mtime=now - 10)
        store = ct.TranscriptStore()
        files = store.files_for("/Users/x/p")
        self.assertEqual([os.path.basename(f) for f in files],
                         ["new.jsonl", "old.jsonl"])
        recent = store.files_for("/Users/x/p", since=now - 100)
        self.assertEqual([os.path.basename(f) for f in recent], ["new.jsonl"])

    def test_titles_for_indexes_current_titles(self):
        self.write_transcript("/Users/x/p", "a", "Title A", ["x"])
        self.write_transcript("/Users/x/p", "b", "Title B", ["x"])
        store = ct.TranscriptStore()
        index = store.titles_for(store.files_for("/Users/x/p"))
        self.assertEqual(sorted(index), ["Title A", "Title B"])

    def test_claim_marks_path_and_session_id_as_taken(self):
        p = self.write_transcript("/Users/x/p", "sid1", "T", ["x"])
        store = ct.TranscriptStore()
        row = self.claude_row("/Users/x/p", "T")
        self.assertTrue(store.is_free(p))
        store.claim(store.summary(p), row, "title")
        self.assertFalse(store.is_free(p))
        self.assertEqual(row["claude"]["match"], "title")
        self.assertEqual(row["claude"]["session"]["session_id"], "sid1")

    def test_pick_prefers_the_matching_project_directory(self):
        # The same resumed session is copied into two project directories.
        here = self.write_transcript("/Users/x/here", "dup", "T", ["x"])
        there = self.write_transcript("/Users/x/there", "dup", "T", ["x"],
                                      mtime=time.time() + 50)
        store = ct.TranscriptStore()
        best = store.pick([there, here], "/Users/x/here")
        self.assertEqual(best["session_file"], here,
                         "should prefer the pane's own project dir over a newer copy")


class TestResolver(TranscriptFixture):
    def test_title_match_wins_over_a_newer_unrelated_transcript(self):
        now = time.time()
        self.write_transcript("/Users/x/p", "right", "The Real Work", ["a"],
                              mtime=now - 500)
        self.write_transcript("/Users/x/p", "newer", "Something Else", ["a"],
                              mtime=now - 1)
        row = self.claude_row("/Users/x/p", "The Real Work",
                              started_at=now - 1000)
        ct.resolve_transcripts([row], use_global=False)
        self.assertEqual(row["claude"]["match"], "title")
        self.assertEqual(row["claude"]["session"]["session_id"], "right")

    def test_busy_directory_gives_every_tab_its_own_transcript(self):
        """The bug this ordering exists to prevent.

        Twenty live sessions share one directory. Resolving tab by tab with an
        mtime guess hands early tabs arbitrary files and every later tab
        inherits the error, so all title matches must settle first.
        """
        now, rows = time.time(), []
        for i in range(20):
            title = "Session %02d" % i
            self.write_transcript("/Users/x/busy", "sid%02d" % i, title, ["a"],
                                  mtime=now - i)
            rows.append(self.claude_row("/Users/x/busy", title,
                                        started_at=now - 5000, tab=i + 1))
        ct.resolve_transcripts(rows, use_global=False)
        for i, row in enumerate(rows):
            self.assertEqual(row["claude"]["match"], "title")
            self.assertEqual(row["claude"]["session"]["session_id"],
                             "sid%02d" % i)
        ids = [r["claude"]["session"]["session_file"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)), "a transcript was claimed twice")

    def test_recorded_cwd_drift_does_not_block_a_match(self):
        # A session that cd'd into a subdirectory records that deeper cwd, which
        # must not disqualify it from matching its own pane.
        now = time.time()
        self.write_transcript("/Users/x/p", "s1", "Drifted", ["a"],
                              mtime=now - 10, recorded_cwd="/Users/x/p/sub/dir")
        row = self.claude_row("/Users/x/p", "Drifted", started_at=now - 100)
        ct.resolve_transcripts([row], use_global=False)
        self.assertEqual(row["claude"]["match"], "title")
        self.assertEqual(row["claude"]["session"]["session_id"], "s1")

    def test_mtime_fallback_when_the_title_cannot_match(self):
        # A tmux tab is titled by tmux, not by Claude, so there is no title to
        # match and the newest transcript is the honest guess.
        now = time.time()
        self.write_transcript("/Users/x/p", "s1", "Real Title", ["a"],
                              mtime=now - 5)
        row = self.claude_row("/Users/x/p", "proj", started_at=now - 100)
        ct.resolve_transcripts([row], use_global=False)
        self.assertEqual(row["claude"]["match"], "mtime")
        self.assertEqual(row["claude"]["session"]["session_id"], "s1")

    def test_weak_marker_when_nothing_was_written_since_launch(self):
        now = time.time()
        self.write_transcript("/Users/x/p", "stale", "Old Title", ["a"],
                              mtime=now - 90000)
        row = self.claude_row("/Users/x/p", "untitled thing", started_at=now - 60)
        ct.resolve_transcripts([row], use_global=False)
        self.assertEqual(row["claude"]["match"], "weak")

    def test_no_transcript_at_all_stays_unmatched(self):
        row = self.claude_row("/Users/x/empty", "Nothing Yet")
        ct.resolve_transcripts([row], use_global=False)
        self.assertEqual(row["claude"]["match"], "none")
        self.assertIsNone(row["claude"]["session"])

    def test_global_pass_finds_a_session_resumed_elsewhere(self):
        # Resuming from a different directory leaves the transcript in the
        # original project directory, so the local scan cannot see it.
        now = time.time()
        self.write_transcript("/Users/x/origin", "moved", "Ported Work", ["a"],
                              mtime=now - 5)
        row = self.claude_row("/Users/x/elsewhere", "Ported Work",
                              started_at=now - 60)
        ct.resolve_transcripts([row], use_global=True)
        self.assertEqual(row["claude"]["match"], "title-global")
        self.assertEqual(row["claude"]["session"]["session_id"], "moved")

    def test_global_pass_can_be_disabled(self):
        now = time.time()
        self.write_transcript("/Users/x/origin", "moved", "Ported Work", ["a"],
                              mtime=now - 5)
        row = self.claude_row("/Users/x/elsewhere", "Ported Work",
                              started_at=now - 60)
        ct.resolve_transcripts([row], use_global=False)
        self.assertEqual(row["claude"]["match"], "none")

    def test_two_panes_cannot_claim_two_copies_of_one_session(self):
        now = time.time()
        for d in ("/Users/x/one", "/Users/x/two"):
            self.write_transcript(d, "sameid", "Shared Title", ["a"],
                                  mtime=now - 5)
        rows = [self.claude_row("/Users/x/one", "Shared Title",
                                started_at=now - 60, tab=1),
                self.claude_row("/Users/x/two", "Shared Title",
                                started_at=now - 60, tab=2)]
        ct.resolve_transcripts(rows, use_global=False)
        claimed = [r["claude"]["session"] for r in rows if r["claude"]["session"]]
        self.assertEqual(len(claimed), 1,
                         "the same session id must not be claimed twice")

    def test_rows_without_claude_are_left_alone(self):
        row = self.claude_row("/Users/x/p", "T")
        row["claude"] = None
        ct.resolve_transcripts([row], use_global=False)
        self.assertIsNone(row["claude"])


class TestFiltering(TranscriptFixture):
    def _row_with_session(self):
        self.write_transcript("/Users/x/p", "s1", "Audit the tokens",
                              ["find stale keys"])
        row = self.claude_row("/Users/x/p", "Audit the tokens")
        ct.resolve_transcripts([row], use_global=False)
        return row

    def test_matches_searches_path_title_prompt_and_branch(self):
        row = self._row_with_session()
        for pattern in ("audit", "tokens", "stale keys", "main", "s1",
                        "Users/x/p"):
            self.assertTrue(ct.matches(row, ct.compile_pattern(pattern, "--grep")),
                            pattern)
        self.assertFalse(ct.matches(row, ct.compile_pattern("zzz", "--grep")))

    def test_matches_on_a_plain_shell_tab(self):
        row = self.claude_row("/Users/x/shellonly", "~ (-zsh)")
        row["claude"] = None
        self.assertTrue(ct.matches(row, ct.compile_pattern("shellonly", "--grep")))


class TestJump(TranscriptFixture):
    """--jump is the only code path that changes anything, so pin its contract.

    jump() reaches the outside world only through ct.run and ct.flash_pane, both
    module globals, so both are replaced here and no tab is ever activated.
    """

    def setUp(self):
        super().setUp()
        self.activated = []
        self.flashed = []
        self._run, self._flash = ct.run, ct.flash_pane
        ct.run = lambda cmd, **k: self.activated.append(cmd) or ""
        ct.flash_pane = lambda pane, color, seconds: (
            self.flashed.append((pane["tab_index"], color, seconds)) or True)
        self.err = io.StringIO()
        self.out = io.StringIO()

    def tearDown(self):
        ct.run, ct.flash_pane = self._run, self._flash
        super().tearDown()

    def _jump(self, rows, pattern, **kw):
        with contextlib.redirect_stderr(self.err), contextlib.redirect_stdout(self.out):
            return ct.jump(rows, pattern, **kw)

    def shell_row(self, cwd, title, tab):
        row = self.claude_row(cwd, title, tab=tab)
        row["claude"] = None
        return row

    def test_no_match_exits_1_and_changes_nothing(self):
        rows = [self.claude_row("/Users/x/p", "Some Work")]
        self.assertEqual(self._jump(rows, "nothing-like-this"), 1)
        self.assertEqual(self.activated, [], "must not activate a tab")
        self.assertEqual(self.flashed, [])
        self.assertIn("no tab matches", self.err.getvalue())

    def test_ambiguous_exits_2_and_changes_nothing(self):
        rows = [self.claude_row("/Users/x/one", "Deploy the thing", tab=1),
                self.claude_row("/Users/x/two", "Deploy the other", tab=2)]
        self.assertEqual(self._jump(rows, "deploy"), 2)
        self.assertEqual(self.activated, [], "must not activate a tab")
        self.assertEqual(self.flashed, [])
        err = self.err.getvalue()
        self.assertIn("ambiguous: 2 matches", err)
        # Both candidates are listed so the user can narrow the pattern.
        self.assertIn("Deploy the thing", err)
        self.assertIn("Deploy the other", err)

    def test_single_claude_tab_wins_over_matching_shell_tabs(self):
        # The disambiguation rule: if exactly one match is running Claude, it is
        # the intended target and the plain shells are noise.
        rows = [self.shell_row("/Users/x/deploy-notes", "~ (-zsh)", tab=1),
                self.claude_row("/Users/x/deploy", "Deploy the thing", tab=2),
                self.shell_row("/Users/x/deploy-old", "~ (-zsh)", tab=3)]
        self.assertEqual(self._jump(rows, "deploy"), 0)
        self.assertEqual(len(self.activated), 1)
        script = self.activated[0][-1]
        self.assertIn("tell tab 2", script, "activated the wrong tab")
        self.assertIn("window id 100", script)
        self.assertIn("Deploy the thing", self.out.getvalue())

    def test_rule_does_not_fire_when_two_matches_run_claude(self):
        rows = [self.shell_row("/Users/x/deploy-notes", "~ (-zsh)", tab=1),
                self.claude_row("/Users/x/deploy", "Deploy one", tab=2),
                self.claude_row("/Users/x/deploy2", "Deploy two", tab=3)]
        self.assertEqual(self._jump(rows, "deploy"), 2)
        self.assertEqual(self.activated, [])

    def test_rule_does_not_fire_when_no_match_runs_claude(self):
        rows = [self.shell_row("/Users/x/deploy-a", "~ (-zsh)", tab=1),
                self.shell_row("/Users/x/deploy-b", "~ (-zsh)", tab=2)]
        self.assertEqual(self._jump(rows, "deploy"), 2)
        self.assertEqual(self.activated, [])

    def test_single_shell_tab_still_jumps(self):
        rows = [self.shell_row("/Users/x/plain", "~ (-zsh)", tab=4)]
        self.assertEqual(self._jump(rows, "plain"), 0)
        self.assertIn("tell tab 4", self.activated[0][-1])

    def test_flash_is_requested_by_default_and_suppressible(self):
        rows = [self.claude_row("/Users/x/p", "Work", tab=7)]
        self.assertEqual(self._jump(rows, "Work"), 0)
        self.assertEqual(self.flashed, [(7, ct.FLASH_COLOR, ct.FLASH_SECONDS)])
        self.flashed = []
        self.assertEqual(self._jump(rows, "Work", flash=False), 0)
        self.assertEqual(self.flashed, [])
        # A zero or negative duration also means no flash.
        self.assertEqual(self._jump(rows, "Work", seconds=0), 0)
        self.assertEqual(self.flashed, [])

    def test_failed_flash_still_reports_a_successful_jump(self):
        ct.flash_pane = lambda *a: False
        rows = [self.claude_row("/Users/x/p", "Work", tab=1)]
        self.assertEqual(self._jump(rows, "Work"), 0, "the jump itself worked")
        self.assertIn("could not flash", self.err.getvalue())

    def test_bad_regex_exits_2_before_touching_anything(self):
        rows = [self.claude_row("/Users/x/p", "Work")]
        with self.assertRaises(SystemExit) as cm:
            self._jump(rows, "*bad")
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.activated, [])

    def test_matches_on_session_id_and_prompt_text(self):
        self.write_transcript("/Users/x/p", "abc123", "Titled Work",
                              ["look at the invoice job"])
        row = self.claude_row("/Users/x/p", "Titled Work")
        ct.resolve_transcripts([row], use_global=False)
        self.assertEqual(self._jump([row], "abc123"), 0)
        self.activated = []
        self.assertEqual(self._jump([row], "invoice job"), 0)


class TestFlashState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._state = ct.FLASH_STATE
        ct.FLASH_STATE = os.path.join(self.tmp, "cache", "flash-state.json")
        self.restored = []
        self._restore = ct.restore_background
        ct.restore_background = lambda pane, rgb, **k: self.restored.append(
            (pane["iterm_session_id"], list(rgb))) or "profile"

    def tearDown(self):
        ct.FLASH_STATE = self._state
        ct.restore_background = self._restore
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def pane(guid="G1"):
        return {"iterm_session_id": guid, "window_id": "1", "tab_index": 1,
                "pane_index": 1, "tty": "/dev/ttys001"}

    def test_state_round_trip_creates_missing_directories(self):
        self.assertEqual(ct.load_flash_state(), {})
        ct.save_flash_state({"G1": {"color": [1, 2, 3], "active_until": 0,
                                    "seen": 5}})
        self.assertEqual(ct.load_flash_state()["G1"]["color"], [1, 2, 3])

    def test_corrupt_state_file_is_treated_as_empty(self):
        os.makedirs(os.path.dirname(ct.FLASH_STATE), exist_ok=True)
        with open(ct.FLASH_STATE, "w") as fh:
            fh.write("{not json")
        self.assertEqual(ct.load_flash_state(), {})

    def test_recover_restores_a_flash_that_never_finished(self):
        now = time.time()
        ct.save_flash_state({"G1": {"color": [10, 20, 30],
                                    "active_until": now - 1, "seen": now}})
        ct.recover_flashes([self.pane("G1")])
        self.assertEqual(self.restored, [("G1", [10, 20, 30])])
        # The record survives, now marked inactive, because it is the pane's
        # pristine colour and must not be re-read later.
        entry = ct.load_flash_state()["G1"]
        self.assertEqual(entry["active_until"], 0)
        self.assertEqual(entry["color"], [10, 20, 30])

    def test_recover_leaves_a_flash_still_in_flight(self):
        now = time.time()
        ct.save_flash_state({"G1": {"color": [1, 2, 3],
                                    "active_until": now + 60, "seen": now}})
        ct.recover_flashes([self.pane("G1")])
        self.assertEqual(self.restored, [])
        self.assertGreater(ct.load_flash_state()["G1"]["active_until"], now)

    def test_recover_drops_records_for_panes_that_are_gone(self):
        now = time.time()
        ct.save_flash_state({"GONE": {"color": [1, 2, 3], "active_until": 0,
                                       "seen": now}})
        ct.recover_flashes([self.pane("G1")])
        self.assertEqual(self.restored, [])
        self.assertEqual(ct.load_flash_state(), {})

    def test_recover_expires_stale_records(self):
        old = time.time() - ct.FLASH_RECORD_TTL - 10
        ct.save_flash_state({"G1": {"color": [1, 2, 3], "active_until": 0,
                                    "seen": old}})
        ct.recover_flashes([self.pane("G1")])
        self.assertEqual(ct.load_flash_state(), {})

    def test_recover_with_no_state_does_nothing(self):
        ct.recover_flashes([self.pane("G1")])
        self.assertEqual(self.restored, [])

    def test_flash_reuses_the_recorded_colour_and_never_re_reads(self):
        """Re-reading would walk the pane darker on every flash."""
        now = time.time()
        ct.save_flash_state({"G1": {"color": [4273, 5020, 6015],
                                    "active_until": 0, "seen": now}})
        calls = {"read": 0, "set": []}
        real_read, real_set, real_sleep = (ct.read_background, ct.set_background,
                                          time.sleep)
        ct.read_background = lambda pane: calls.__setitem__(
            "read", calls["read"] + 1) or [1, 1, 1]
        ct.set_background = lambda pane, rgb: calls["set"].append(list(rgb))
        time.sleep = lambda s: None
        try:
            self.assertTrue(ct.flash_pane(self.pane("G1"), "#ff8800", 0.01))
        finally:
            ct.read_background, ct.set_background = real_read, real_set
            time.sleep = real_sleep
        self.assertEqual(calls["read"], 0, "must not re-read a recorded colour")
        self.assertEqual(calls["set"], [[65535, 34952, 0]])
        self.assertEqual(self.restored, [("G1", [4273, 5020, 6015])])
        self.assertEqual(ct.load_flash_state()["G1"]["color"],
                         [4273, 5020, 6015])

    def test_flash_rejects_a_bad_colour_without_touching_the_pane(self):
        real_set = ct.set_background
        ct.set_background = lambda *a: self.fail("must not repaint the pane")
        try:
            self.assertFalse(ct.flash_pane(self.pane("G1"), "nonsense", 0.01))
        finally:
            ct.set_background = real_set


class TestRendering(TranscriptFixture):
    def test_markdown_and_tree_render_both_kinds_of_row(self):
        self.write_transcript("/Users/x/p", "s1", "Audit the tokens", ["go"])
        claude = self.claude_row("/Users/x/p", "Audit the tokens")
        ct.resolve_transcripts([claude], use_global=False)
        shell = self.claude_row("/Users/x/plain", "~ (-zsh)", tab=2)
        shell["claude"] = None
        shell["foreground_command"] = "-zsh"
        rows = [claude, shell]

        for render in (lambda: ct.print_markdown(rows),
                       lambda: ct.print_tree(rows, ct.Paint(False)),
                       lambda: ct.print_tree(rows, ct.Paint(False),
                                             show_prompts=True)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                render()
            out = buf.getvalue()
            self.assertIn("Audit the tokens", out)
            self.assertIn("/Users/x/p", out)
        # A login shell's leading dash is not part of its name.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ct.print_markdown(rows)
        self.assertIn("_zsh_", buf.getvalue())


class TestCommandLine(unittest.TestCase):
    """The only tests that run the script as a program.

    Both flags exit inside argparse, before anything talks to iTerm2.
    """

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "claude_tabs.py")

    def test_version(self):
        out = subprocess.run([sys.executable, self.script, "--version"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertIn(ct.__version__, out.stdout + out.stderr)

    def test_help_lists_every_flag(self):
        out = subprocess.run([sys.executable, self.script, "--help"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        for flag in ("--claude-only", "--prompts", "--grep", "--idle", "--sort",
                     "--jump", "--md", "--json", "--save", "--no-flash",
                     "--flash-color", "--flash-seconds", "--no-color",
                     "--version"):
            self.assertIn(flag, out.stdout, flag)


if __name__ == "__main__":
    unittest.main(verbosity=2)
