#!/usr/bin/env python3
"""Tests for claude_tabs.

Hermetic: no iTerm2, no AppleScript, no network, and nothing read from or written
to the real ~/.claude or ~/.cache. Anything that would shell out is either
pointed at a temporary directory or replaced.

Run with:  python3 -m unittest -v
"""

import contextlib
import io
import socket
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

    def assertMatch(self, row, expected):
        """Assert the match state, and that it is renderable at all."""
        self.assertEqual(row["claude"]["match"], expected)
        self.assertIn(row["claude"]["match"], ct.MATCH_MARK,
                      "match state has no marker, so it renders as a blank")

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
        self.assertMatch(row, "title")
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
        self.assertMatch(row, "title")
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
            self.assertMatch(row, "title")
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
        self.assertMatch(row, "title")
        self.assertEqual(row["claude"]["session"]["session_id"], "s1")

    def test_mtime_fallback_when_the_title_cannot_match(self):
        # A tmux tab is titled by tmux, not by Claude, so there is no title to
        # match and the newest transcript is the honest guess.
        now = time.time()
        self.write_transcript("/Users/x/p", "s1", "Real Title", ["a"],
                              mtime=now - 5)
        row = self.claude_row("/Users/x/p", "proj", started_at=now - 100)
        ct.resolve_transcripts([row], use_global=False)
        self.assertMatch(row, "mtime")
        self.assertEqual(row["claude"]["session"]["session_id"], "s1")

    def test_weak_marker_when_nothing_was_written_since_launch(self):
        now = time.time()
        self.write_transcript("/Users/x/p", "stale", "Old Title", ["a"],
                              mtime=now - 90000)
        row = self.claude_row("/Users/x/p", "untitled thing", started_at=now - 60)
        ct.resolve_transcripts([row], use_global=False)
        self.assertMatch(row, "weak")

    def test_no_transcript_at_all_stays_unmatched(self):
        row = self.claude_row("/Users/x/empty", "Nothing Yet")
        ct.resolve_transcripts([row], use_global=False)
        self.assertMatch(row, "none")
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
        self.assertMatch(row, "title-global")
        self.assertEqual(row["claude"]["session"]["session_id"], "moved")

    def test_global_pass_can_be_disabled(self):
        now = time.time()
        self.write_transcript("/Users/x/origin", "moved", "Ported Work", ["a"],
                              mtime=now - 5)
        row = self.claude_row("/Users/x/elsewhere", "Ported Work",
                              started_at=now - 60)
        ct.resolve_transcripts([row], use_global=False)
        self.assertMatch(row, "none")

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
    def test_tree_marks_a_global_title_match_distinctly(self):
        # Resolved from another project directory, so it must not render as a
        # plain local exact match.
        now = time.time()
        self.write_transcript("/Users/x/origin", "moved", "Ported Work", ["a"],
                              mtime=now - 5)
        row = self.claude_row("/Users/x/elsewhere", "Ported Work",
                              started_at=now - 60)
        ct.resolve_transcripts([row], use_global=True)
        self.assertMatch(row, "title-global")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ct.print_tree([row], ct.Paint(False))
        out = buf.getvalue()
        self.assertIn(ct.MATCH_MARK["title-global"], out)
        self.assertIn("Ported Work", out)

    def test_markdown_omits_the_marker_column(self):
        # The marker is a tree-view affordance; --md has no column for it.
        self.write_transcript("/Users/x/p", "s1", "Local Work", ["a"])
        row = self.claude_row("/Users/x/p", "Local Work")
        ct.resolve_transcripts([row], use_global=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ct.print_markdown([row])
        header = buf.getvalue().splitlines()[0]
        self.assertNotIn("Marker", header)
        self.assertIn("Claude session", header)

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


TMUX_PANES_OUT = "\n".join([
    US.join(["/dev/ttys095", "work", "1", "1", "1", "1",
             "\u2733 Tmux Session Work (claude)"]),
    US.join(["/dev/ttys096", "work", "2", "1", "1", "0",
             "\u2733 Hidden Window (claude)"]),
    US.join(["/dev/ttys097", "other", "1", "1", "1", "1", "~ (-zsh)"]),
])
# Real-shaped GUIDs: ITERM_SESSION_RE requires 36 characters, so short
# stand-ins would make the env route silently unreachable in these fixtures.
GUID_A = "AAAAAAAA-1111-4111-8111-AAAAAAAAAAAA"
GUID_B = "BBBBBBBB-2222-4222-8222-BBBBBBBBBBBB"

TMUX_CLIENTS_OUT = "\n".join([
    US.join(["/dev/ttys006", "work"]),
    US.join(["/dev/ttys009", "other"]),
])


class TestTmuxIntrospection(unittest.TestCase):
    def setUp(self):
        self._run = ct.run

    def tearDown(self):
        ct.run = self._run

    def test_panes_parse_with_visibility(self):
        ct.run = lambda *a, **k: TMUX_PANES_OUT
        panes = ct.tmux_panes()
        self.assertEqual(sorted(panes), ["ttys095", "ttys096", "ttys097"])
        self.assertEqual(panes["ttys095"]["session"], "work")
        self.assertEqual(panes["ttys095"]["title"],
                         "\u2733 Tmux Session Work (claude)")
        # Visible means active pane of the active window, so both flags.
        self.assertTrue(panes["ttys095"]["visible"])
        self.assertFalse(panes["ttys096"]["visible"])


    def test_default_hostname_title_is_recognised(self):
        import socket
        host = socket.gethostname()
        for default in ("", host, host.split(".")[0]):
            self.assertTrue(ct.is_default_pane_title(default), repr(default))
        for real in ("Address CodeRabbit review comments", "proj", "vim"):
            self.assertFalse(ct.is_default_pane_title(real), real)

    def test_clients_group_by_session(self):
        ct.run = lambda *a, **k: TMUX_CLIENTS_OUT
        self.assertEqual(ct.tmux_clients(),
                         {"work": ["ttys006"], "other": ["ttys009"]})

    def test_no_tmux_server_is_not_an_error(self):
        ct.run = lambda *a, **k: ""
        self.assertEqual(ct.tmux_panes(), {})
        self.assertEqual(ct.tmux_clients(), {})


class TestClaudeAttribution(unittest.TestCase):
    """Which iTerm pane is displaying a given claude process."""

    def setUp(self):
        self._run = ct.run
        ct.run = self._fake

    def tearDown(self):
        ct.run = self._run

    @staticmethod
    def _fake(cmd, **k):
        if cmd[0] == "tmux":
            return TMUX_PANES_OUT if cmd[1] == "list-panes" else TMUX_CLIENTS_OUT
        return ""

    PANES = [
        {"iterm_session_id": "AAA", "tty": "/dev/ttys001"},
        {"iterm_session_id": "BBB", "tty": "/dev/ttys006"},
    ]

    @staticmethod
    def procs(entries):
        return {pid: {"pid": pid, "ppid": 1, "tty": tty, "stat": "S+",
                      "etime": "01:00", "command": "claude"}
                for pid, tty in entries}

    def test_own_tty_wins(self):
        procs = self.procs([(300, "ttys001")])
        got = ct.attribute_claudes(self.PANES, procs, {300}, {300: "BBB"})
        # The env variable points at the wrong pane; the tty is authoritative.
        self.assertEqual(got, {"AAA": [(300, "direct", None)]})


    def test_tmux_is_not_consulted_when_every_route_is_direct(self):
        calls = []
        ct.run = lambda cmd, **k: calls.append(cmd[0]) or ""
        procs = self.procs([(300, "ttys001")])
        ct.attribute_claudes(self.PANES, procs, {300}, {})
        self.assertNotIn("tmux", calls,
                         "tmux was queried even though the tty already matched")

    def test_no_claude_processes_means_no_subprocesses(self):
        calls = []
        ct.run = lambda cmd, **k: calls.append(cmd[0]) or ""
        self.assertEqual(ct.attribute_claudes(self.PANES, {}, set(), {}), {})
        self.assertEqual(calls, [])

    def test_tmux_pane_resolves_through_its_client(self):
        procs = self.procs([(201, "ttys095")])
        # ITERM_SESSION_ID says AAA, because that is where the tmux server was
        # started. The client for session "work" is on ttys006, which is BBB.
        got = ct.attribute_claudes(self.PANES, procs, {201}, {201: "AAA"})
        self.assertEqual(list(got), ["BBB"])
        pid, route, facts = got["BBB"][0]
        self.assertEqual((pid, route), (201, "tmux"))
        self.assertEqual(facts["session"], "work")
        self.assertTrue(facts["visible"])

    def test_env_is_the_last_resort(self):
        procs = self.procs([(400, "ttys123")])  # neither a pane nor a tmux pty
        got = ct.attribute_claudes(self.PANES, procs, {400}, {400: "AAA"})
        self.assertEqual(got, {"AAA": [(400, "env", None)]})

    def test_unreachable_process_is_dropped(self):
        procs = self.procs([(500, "??")])
        self.assertEqual(ct.attribute_claudes(self.PANES, procs, {500}, {}), {})

    def test_tmux_client_on_no_known_pane_falls_back_to_env(self):
        # session "other" has a client on ttys009, which is not an iTerm pane.
        procs = self.procs([(600, "ttys097")])
        got = ct.attribute_claudes(self.PANES, procs, {600}, {600: "AAA"})
        self.assertEqual(got, {"AAA": [(600, "env", None)]})


class TestBuildIndexWithTmux(TranscriptFixture):
    """build_index end to end with the whole world stubbed.

    ct.run is the single choke point for osascript, ps, lsof and tmux, so the
    entire pipeline can run without iTerm2, tmux, or the real filesystem.
    """

    def setUp(self):
        super().setUp()
        self._run, self._state = ct.run, ct.FLASH_STATE
        ct.FLASH_STATE = os.path.join(self.tmp, "cache", "flash-state.json")
        ct.run = self._fake
        # The tmux pane's own transcript, titled the way Claude titled it.
        self.write_transcript("/Users/x/tmuxproj", "tsid", "Tmux Session Work",
                              ["do the tmux thing"])
        # A decoy: newer, so an mtime guess would prefer it.
        self.write_transcript("/Users/x/tmuxproj", "decoy", "Unrelated Thing",
                              ["nope"], mtime=time.time() + 100)
        self.write_transcript("/Users/x/direct", "dsid", "Direct Work", ["go"])

    def tearDown(self):
        ct.run, ct.FLASH_STATE = self._run, self._state
        super().tearDown()

    PS = ("  100     1 01:00:00 ttys006 Ss+ tmux attach -t work\n"
          "  200     1 00:30:00 ttys095 Ss  -zsh\n"
          "  201   200 00:29:00 ttys095 S+  claude\n"
          "  300     1 00:10:00 ttys001 S+  claude\n")
    # Both claims point at AAA: the tmux server was started from that pane, so
    # the env variable is wrong for pid 201.
    ENV = ("  201 claude ITERM_SESSION_ID=w0t0p0:%s\n"
           "  300 claude ITERM_SESSION_ID=w0t0p0:%s\n" % (GUID_A, GUID_A))
    LSOF = "p201\nn/Users/x/tmuxproj\np300\nn/Users/x/direct\n"
    TPANES = TMUX_PANES_OUT
    TCLIENTS = TMUX_CLIENTS_OUT

    def _fake(self, cmd, **k):
        if cmd[0] == "osascript":
            return pane_payload([
                (1, "100", 1, 1, GUID_A, "/dev/ttys001",
                 "\u2733 Direct Work (claude)", "t", "/Users/x/direct",
                 "claude", GUID_A, "100"),
                (1, "100", 2, 1, GUID_B, "/dev/ttys006", "work (tmux)", "t",
                 "/Users/x/somewhere", "tmux", GUID_A, "100"),
            ])
        if cmd[0] == "ps" and cmd[1] == "-Ao":
            return self.PS
        if cmd[0] == "ps" and cmd[1] == "eww":
            return self.ENV
        if cmd[0] == "lsof":
            return self.LSOF
        if cmd[0] == "tmux":
            return self.TPANES if cmd[1] == "list-panes" else self.TCLIENTS
        return ""

    def test_tmux_session_is_attributed_and_matched_by_title(self):
        rows = ct.build_index()
        by_tab = {r["tab_index"]: r for r in rows}
        self.assertEqual(sorted(by_tab), [1, 2])

        tmux_row = by_tab[2]
        cl = tmux_row["claude"]
        self.assertEqual(cl["pid"], 201)
        self.assertEqual(cl["attached"], "tmux",
                         "must resolve through the tmux client, not the env var")
        self.assertEqual(cl["tmux"]["session"], "work")
        # The iTerm tab is named by tmux; the title comes from the tmux pane.
        self.assertEqual(tmux_row["tab_name"], "work (tmux)")
        self.assertEqual(tmux_row["title"], "Tmux Session Work")
        # And because the title is real, the transcript is an exact match rather
        # than the newest-file guess that would have picked the decoy.
        self.assertMatch(tmux_row, "title")
        self.assertEqual(cl["session"]["session_id"], "tsid")
        self.assertEqual(cl["session"]["ai_title"], "Tmux Session Work")

    def test_direct_pane_is_unaffected(self):
        row = {r["tab_index"]: r for r in ct.build_index()}[1]
        self.assertEqual(row["claude"]["attached"], "direct")
        self.assertEqual(row["claude"]["pid"], 300)
        self.assertMatch(row, "title")
        self.assertEqual(row["claude"]["session"]["session_id"], "dsid")

    def test_the_tmux_row_renders_its_coordinates(self):
        rows = ct.build_index()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ct.print_tree(rows, ct.Paint(False))
        out = buf.getvalue()
        self.assertIn("tmux work:1.1", out)
        self.assertIn("Tmux Session Work", out)


class TestAttributionRanking(TestBuildIndexWithTmux):
    """When one iTerm pane hosts several claude processes, which one wins."""

    # pid 999 has no recognisable tty, so only ITERM_SESSION_ID places it, and it
    # names BBB. pid 201 is on a tmux pane whose client is BBB as well.
    PS = (TestBuildIndexWithTmux.PS +
          "  999     1 00:05:00 ttys200 S+  claude\n")
    ENV = (TestBuildIndexWithTmux.ENV +
           "  999 claude ITERM_SESSION_ID=w0t0p0:%s\n" % GUID_B)
    LSOF = TestBuildIndexWithTmux.LSOF + "p999\nn/Users/x/envonly\n"

    def test_tmux_beats_an_env_only_claim_on_the_same_pane(self):
        row = {r["tab_index"]: r for r in ct.build_index()}[2]
        cl = row["claude"]
        self.assertEqual(cl["attached"], "tmux",
                         "a resolved tmux client beats an inherited env var")
        self.assertEqual(cl["pid"], 201)
        self.assertIn(999, cl["extra_pids"])


class TestVisibleTmuxPaneWins(TestBuildIndexWithTmux):
    """Two claude sessions in one tmux session, only one on screen."""

    # ttys096 is window 2, which is not the active window, so it is hidden.
    PS = (TestBuildIndexWithTmux.PS +
          "  202   200 00:20:00 ttys096 S+  claude\n")
    ENV = (TestBuildIndexWithTmux.ENV +
           "  202 claude ITERM_SESSION_ID=w0t0p0:%s\n" % GUID_A)
    LSOF = TestBuildIndexWithTmux.LSOF + "p202\nn/Users/x/hidden\n"

    def test_the_visible_pane_is_the_one_reported(self):
        row = {r["tab_index"]: r for r in ct.build_index()}[2]
        cl = row["claude"]
        self.assertEqual(cl["pid"], 201, "the tab shows the active tmux pane")
        self.assertTrue(cl["tmux"]["visible"])
        self.assertEqual(cl["tmux"]["window"], "1")
        self.assertIn(202, cl["extra_pids"])




class TestHiddenOnlyTmuxPane(TestBuildIndexWithTmux):
    """The tab's only claude sits in a tmux window that is not on screen."""

    # 201 is gone, so 202 on ttys096 (window 2, not the active window) is the
    # only candidate. Nothing is hand-edited: the row is selected and rendered
    # by the real path.
    PS = ("  100     1 01:00:00 ttys006 Ss+ tmux attach -t work\n"
          "  200     1 00:30:00 ttys095 Ss  -zsh\n"
          "  202   200 00:20:00 ttys096 S+  claude\n"
          "  300     1 00:10:00 ttys001 S+  claude\n")
    ENV = ("  202 claude ITERM_SESSION_ID=w0t0p0:%s\n"
           "  300 claude ITERM_SESSION_ID=w0t0p0:%s\n" % (GUID_A, GUID_A))
    LSOF = "p202\nn/Users/x/hidden\np300\nn/Users/x/direct\n"

    def test_hidden_pane_is_selected_and_labelled(self):
        rows = ct.build_index()
        row = {r["tab_index"]: r for r in rows}[2]
        cl = row["claude"]
        self.assertEqual(cl["pid"], 202)
        self.assertEqual(cl["attached"], "tmux")
        self.assertFalse(cl["tmux"]["visible"])
        self.assertEqual(row["title"], "Hidden Window")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ct.print_tree(rows, ct.Paint(False))
        self.assertIn("(hidden)", buf.getvalue())

    def test_tmux_session_is_attributed_and_matched_by_title(self):
        self.skipTest("inherited fixture assumes pid 201 is present")

    def test_the_tmux_row_renders_its_coordinates(self):
        self.skipTest("inherited fixture assumes pid 201 is present")


class TestTmuxDefaultTitleDoesNotOverride(TestBuildIndexWithTmux):
    """A tmux pane that has set no title reports the hostname."""

    TPANES = US.join(["/dev/ttys095", "work", "1", "1", "1", "1",
                      socket.gethostname()])

    def test_hostname_title_leaves_the_tab_name_alone(self):
        row = {r["tab_index"]: r for r in ct.build_index()}[2]
        self.assertEqual(row["claude"]["attached"], "tmux",
                         "attribution still works without a title")
        # tmux's fallback title must not replace the tab's own name.
        self.assertNotIn(socket.gethostname(), row["title"])
        self.assertEqual(row["title"], "work")

    def test_the_tmux_row_renders_its_coordinates(self):
        self.skipTest("this fixture has no Claude-set title")

    def test_tmux_session_is_attributed_and_matched_by_title(self):
        self.skipTest("this fixture has no Claude-set title")


class TestMatchMarkers(TranscriptFixture):
    def test_markers_are_distinct(self):
        """Guards deletion and collision, but not addition: see below."""
        self.assertEqual(len(set(ct.MATCH_MARK.values())), len(ct.MATCH_MARK),
                         "two states share a marker")

    @unittest.skipIf(not __debug__,
                     "the claim() guard is an assert, stripped under -O")
    def test_claim_refuses_a_state_with_no_marker(self):
        """Guards addition, which is how the title-global gap arose.

        A hardcoded list of states in a test only knows what the test knows, so
        renaming or adding a resolver label slips past it. claim() is the single
        funnel every non-none state passes through, so the invariant lives there
        and cannot drift from the resolver.
        """
        path = self.write_transcript("/Users/x/p", "s1", "T", ["a"])
        store = ct.TranscriptStore()
        row = self.claude_row("/Users/x/p", "T")
        with self.assertRaises(AssertionError):
            store.claim(store.summary(path), row, "invented-state")
        # A real state is accepted.
        store.claim(store.summary(path), row, "title")
        self.assertMatch(row, "title")


class TestViewSelection(TranscriptFixture):
    """--idle and --sort, extracted from main() so they can be tested."""

    def rows(self, now):
        claude_old = self.claude_row("/Users/x/zzz-old", "Old Work", tab=1)
        claude_old["claude"]["session"] = {"mtime": now - 7200, "ai_title": "Old Work"}
        claude_new = self.claude_row("/Users/x/aaa-new", "New Work", tab=2)
        claude_new["claude"]["session"] = {"mtime": now - 60, "ai_title": "New Work"}
        shell = self.claude_row("/Users/x/mmm-shell", "~ (-zsh)", tab=3)
        shell["claude"] = None
        return [claude_old, claude_new, shell]

    def test_claude_only_drops_plain_shells(self):
        now = time.time()
        view = ct.select_rows(self.rows(now), claude_only=True, now=now)
        self.assertEqual([r["tab_index"] for r in view], [1, 2])

    def test_idle_keeps_only_sessions_past_the_cutoff(self):
        now = time.time()
        view = ct.select_rows(self.rows(now), idle_minutes=60, now=now)
        self.assertEqual([r["tab_index"] for r in view], [1])
        # A shell tab has no session to age, so --idle excludes it outright.
        self.assertTrue(all(r["claude"] for r in view))

    def test_idle_zero_keeps_every_session_but_no_shells(self):
        now = time.time()
        view = ct.select_rows(self.rows(now), idle_minutes=0, now=now)
        self.assertEqual([r["tab_index"] for r in view], [1, 2])

    def test_sort_idle_puts_the_stalest_first(self):
        # Feed it newest-first, so the sort has to do real work. With the fixture
        # order the input is already stalest-first and deleting the sort passes.
        now = time.time()
        given = list(reversed(self.rows(now)))
        view = ct.select_rows(given, claude_only=True, sort="idle", now=now)
        self.assertEqual([r["tab_index"] for r in view], [1, 2])

    def test_sort_path_is_alphabetical_by_directory(self):
        now = time.time()
        view = ct.select_rows(self.rows(now), sort="path", now=now)
        self.assertEqual([r["cwd"] for r in view],
                         ["/Users/x/aaa-new", "/Users/x/mmm-shell",
                          "/Users/x/zzz-old"])

    def test_sort_window_is_the_default_and_preserves_order(self):
        now = time.time()
        given = self.rows(now)
        self.assertEqual([r["tab_index"] for r in ct.select_rows(given, now=now)],
                         [1, 2, 3])

    def test_grep_filters_and_combines_with_the_others(self):
        now = time.time()
        rx = ct.compile_pattern("work", "--grep")
        view = ct.select_rows(self.rows(now), grep=rx, now=now)
        self.assertEqual([r["tab_index"] for r in view], [1, 2])
        view = ct.select_rows(self.rows(now), grep=rx, idle_minutes=60,
                              sort="idle", now=now)
        self.assertEqual([r["tab_index"] for r in view], [1])

    def test_the_callers_list_is_never_reordered(self):
        # `view = rows` followed by view.sort() used to reorder the input.
        now = time.time()
        given = self.rows(now)
        before = list(given)
        ct.select_rows(given, sort="path", now=now)
        self.assertEqual(given, before)


class TestIndexPayload(TranscriptFixture):
    def test_shape_and_json_round_trip(self):
        self.write_transcript("/Users/x/p", "s1", "Titled", ["go"])
        row = self.claude_row("/Users/x/p", "Titled")
        ct.resolve_transcripts([row], use_global=False)
        payload = ct.index_payload([row], now=1234.5)
        self.assertEqual(sorted(payload), ["generated_at", "tabs"])
        self.assertEqual(payload["generated_at"], 1234.5)
        self.assertEqual(len(payload["tabs"]), 1)

        # Everything the resolver attaches has to survive json.dumps, including
        # the session dict, or --save and --json break on real data.
        restored = json.loads(json.dumps(payload))
        tab = restored["tabs"][0]
        for key in ("window_id", "tab_index", "cwd", "title", "claude"):
            self.assertIn(key, tab)
        for key in ("session_id", "ai_title", "first_prompt", "mtime",
                    "entries", "git_branch", "session_file"):
            self.assertIn(key, tab["claude"]["session"])
        self.assertEqual(tab["claude"]["match"], "title")

    def test_generated_at_defaults_to_now(self):
        payload = ct.index_payload([])
        self.assertAlmostEqual(payload["generated_at"], time.time(), delta=5)
        self.assertEqual(payload["tabs"], [])


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
