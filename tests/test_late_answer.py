"""A Telegram turn's answer must arrive even when it lands late or after a harness record.

Live case, 2026-09-02 22:18–22:26 (Genius bridge, `run.log`):
  * 22:19 — the turn ended (Stop hook) before any text was written; the backstop picked the
    PREVIOUS turn's answer as "the final answer", the dedup ledger dropped it, and the log said
    the turn was answered. Nothing reached the user.
  * 22:23 — 90 s of transcript silence (the model was thinking) force-ended the next turn; the
    compaction summary (a ``user`` record with ``isCompactSummary``) reset "turn from Telegram";
    the real answer written at 22:25 was then dropped with no log line at all.

Each test names the mutation that makes it red (rodný list červenosti).
"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent2telegram import attach as attach_mod
from agent2telegram.attach import AttachBridge
from agent2telegram.config import Config
from agent2telegram.readers import ClaudeCodeReader, Ev


class _FakeClient:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))

    def delete_message(self, chat_id, message_id):
        pass

    def send_chat_action(self, chat_id, action):
        pass

    def send_plain_id(self, chat_id, text, parse_mode=None):
        return 55

    def edit_plain(self, chat_id, message_id, text, parse_mode=None):
        pass


def _bridge(tmpdir):
    b = object.__new__(AttachBridge)
    b.cfg = Config(agent="claude-code", token="1:2", allowed_user_ids=[7], tmux_session="a2t")
    b.tg = _FakeClient()
    b._owner_chat = 7
    b._marker = "[TG]"
    b._signal = Path(tmpdir) / "answer.txt"
    b._turn_end = None
    b._transcript = Path(tmpdir) / "transcript.jsonl"
    b._reader = ClaudeCodeReader()
    b._turn_active = threading.Event()
    b._turn_from_tg = True
    b._turn_text_sent = False
    b._pending_turn_end = False
    b._turn_started = time.monotonic() - 1.0
    b._typing_count = 1
    b._max_gap = 0.0
    b._status = {"mid": None, "shown": ""}
    b._status_path = None
    b._seen_tools = set()
    b._stop = threading.Event()
    b._sent_keys = set()
    b._pending_send = []
    b._queue_path = None
    b._use_durable_outbox = False
    b._allowed = {7}
    b._last_activity = 0.0
    b._tui_seen = set()
    b._turn_is_reaction = False
    b._pending_files = []
    b._sent_path = Path(tmpdir) / "sent_uuids"
    b._tpos = 0
    return b


def _assistant(uuid, text):
    return json.dumps({"type": "assistant", "uuid": uuid,
                       "message": {"content": [{"type": "text", "text": text}]}})


class HarnessRecordsAreNotPrompts(unittest.TestCase):
    """Mutation: drop ``_is_harness_record`` from ``parse``/``user_text`` → the summary record
    yields a ``user`` event without the origin prefix."""

    def test_compaction_summary_yields_no_user_event(self):
        rec = {"type": "user", "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
               "message": {"content": "This session is being continued from a previous conversation"}}
        self.assertEqual(list(ClaudeCodeReader().parse(rec)), [])
        self.assertIsNone(ClaudeCodeReader().user_text(rec))

    def test_meta_record_yields_no_user_event(self):
        rec = {"type": "user", "isMeta": True, "message": {"content": "<local-command-caveat>"}}
        self.assertEqual(list(ClaudeCodeReader().parse(rec)), [])

    def test_a_real_prompt_still_yields_a_user_event(self):
        rec = {"type": "user", "message": {"content": "[TG] ?"}}
        evs = list(ClaudeCodeReader().parse(rec))
        self.assertEqual([(e.kind, e.text) for e in evs], [("user", "[TG] ?")])


class MarkedTextAlwaysRoutesToTelegram(unittest.TestCase):
    """Mutation: restore ``if not self._turn_from_tg ...: return`` before the marker check."""

    def test_marked_text_is_forwarded_even_when_turn_is_not_from_telegram(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_from_tg = False                  # flag was reset by a stray record
            b._handle_event(Ev("text", text="[tg] late answer", key="k1"))
            self.assertEqual(b.tg.sent, [(7, "late answer")])

    def test_unmarked_text_outside_a_telegram_turn_stays_local(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_from_tg = False
            with self.assertLogs(attach_mod.log, level="INFO") as cm:
                b._handle_event(Ev("text", text="terminal-only reply", key="k2"))
            self.assertEqual(b.tg.sent, [])
            self.assertTrue(any("NOT forwarded" in m for m in cm.output),
                            "a dropped text must leave a trace in the log")

    def test_an_empty_marker_forwards_nothing(self):
        """An empty ``progress_marker`` means "no marker configured", as it does for
        ``file_marker``. Without the guard every text starts with "" and a terminal turn's whole
        output would be pushed to the user's phone.

        Mutation: drop the ``if not marker: return False`` guard → local text is forwarded."""
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._marker = ""
            b._turn_from_tg = False
            b._handle_event(Ev("text", text="internal terminal work", key="k1"))
            self.assertEqual(b.tg.sent, [], "an empty marker leaked a local answer to Telegram")

    def test_marker_is_only_recognised_at_the_start(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            self.assertTrue(b._has_marker("\n  [tg] hi"))
            self.assertFalse(b._has_marker("hi [tg]"))
            self.assertFalse(b._has_marker(""))


class BackstopNeverRecyclesAnOldAnswer(unittest.TestCase):
    """Mutations: (a) drop the ``ev.key in sent`` skip → the old answer is returned again;
    (b) drop ``_turn_tpos`` from the seek → text before the turn start is returned."""

    def setUp(self):
        self._retry_delay = attach_mod.BACKSTOP_RETRY_DELAY
        attach_mod.BACKSTOP_RETRY_DELAY = 0.0

    def tearDown(self):
        attach_mod.BACKSTOP_RETRY_DELAY = self._retry_delay

    def test_already_sent_text_is_not_this_turns_answer(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._transcript.write_text(_assistant("old", "[tg] the 21:39 answer") + "\n", "utf-8")
            b._sent_keys.add("old")
            b._turn_active.set()
            self.assertIsNone(b._last_assistant_text())
            with self.assertLogs(attach_mod.log, level="ERROR"):
                b._finish_turn()
            self.assertEqual(b.tg.sent, [], "the backstop re-sent an answer from an earlier turn")
            self.assertFalse(b._turn_text_sent, "an unanswered turn must not be marked answered")

    def test_text_written_before_the_turn_began_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            first = _assistant("t1", "answer of an earlier terminal turn") + "\n"
            b._transcript.write_text(first, "utf-8")
            b._tpos = len(first.encode("utf-8"))
            b._begin_turn()                          # records where this turn starts
            self.assertIsNone(b._last_assistant_text())
            b._transcript.write_text(first + _assistant("t2", "[tg] this turn's answer") + "\n", "utf-8")
            self.assertEqual(b._last_assistant_text(), "[tg] this turn's answer")
            self.assertEqual(b._last_backstop_key, "t2")


class TurnStartOffsetSurvivesANewTranscript(unittest.TestCase):
    """The turn-start offset is a position in ONE file. Claude Code opens a new transcript when
    the session changes, and ``_switch_transcript`` does that mid-turn whenever the cwd is known.
    The offset then points past the end of a smaller new file, the backstop seeks past EOF, reads
    nothing — and the answer is dropped. That is the very failure this commit set out to fix,
    reintroduced in a narrower case.

    Mutation: drop the ``start > size`` clamp in ``_last_assistant_text`` → the answer is lost."""

    def test_answer_in_a_shorter_new_transcript_is_still_found(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_tpos = 900_000                   # offset from the previous, much larger file
            b._transcript.write_text(_assistant("t9", "[tg] the answer") + "\n", "utf-8")
            b._turn_active.set()
            self.assertEqual(b._last_assistant_text(), "[tg] the answer")


    def test_switching_transcript_clears_the_turn_start_offset(self):
        """A new transcript can also be LONGER than the old one, where no clamp helps: the stale
        offset would then skip the beginning of the new file. Switching must clear it.

        Mutation: drop ``self._turn_tpos = 0`` from ``_maybe_reresolve`` → the offset survives."""
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_tpos = 12_345
            b.cfg.transcript_path = "auto"
            b._last_resolve = 0.0
            novy = Path(d) / "novy.jsonl"
            novy.write_text(_assistant("t1", "x") + "\n", "utf-8")
            b._resolve_transcript = lambda: novy
            b._session_cwd = lambda: str(Path(d))
            b._maybe_reresolve()
            self.assertEqual(b._transcript, novy, "the switch itself did not happen")
            self.assertNotEqual(b._turn_tpos, 12_345, "a stale offset from the previous transcript survived")
            self.assertLessEqual(b._turn_tpos, novy.stat().st_size, "the offset points past the new file")


class IdleWindowDependsOnTheHook(unittest.TestCase):
    """Mutation: make ``_idle_limit`` return ``IDLE_DONE`` unconditionally."""

    def test_hooked_bridge_waits_longer_than_a_thinking_pause(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            self.assertEqual(b._idle_limit(), attach_mod.IDLE_DONE)
            b._turn_end = Path(d) / "turn_end"
            self.assertEqual(b._idle_limit(), attach_mod.IDLE_DONE_HOOKED)
            self.assertGreaterEqual(attach_mod.IDLE_DONE_HOOKED, 300.0,
                                    "a long-thinking model pauses for minutes, not seconds")


if __name__ == "__main__":
    unittest.main()
