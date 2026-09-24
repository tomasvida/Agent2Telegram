"""Adopting a transcript must never replay its history as the current answer (2026-09-04).

Sol's bridge followed a sibling agent's rollout for ten minutes (both agents run from the same
cwd), jumped back to Sol's own rollout when the owner wrote "jsi tam?", started at byte 0 and
forwarded 22 old messages as the reply. Two guards below: the session's OWN rollout wins over
anything cwd/mtime can suggest, and a freshly adopted transcript is read only from the current
turn onwards.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent2telegram.attach import AttachBridge
from agent2telegram.config import Config
from agent2telegram.readers import CodexReader


def _codex_bridge(tmpdir):
    b = object.__new__(AttachBridge)
    b.cfg = Config(agent="codex", token="1:2", allowed_user_ids=[7], tmux_session="a2t")
    b.cfg.transcript_path = "auto"
    b._owner_chat = 7
    b._marker = "[TG]"
    b._transcript = None
    b._reader = CodexReader()
    b._turn_active = threading.Event()
    b._turn_from_tg = True
    b._sent_keys = set()
    b._last_resolve = 0.0
    b._tpos = 0
    b._turn_tpos = 0
    return b


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".000Z"


def _assistant(text, epoch):
    return json.dumps({"timestamp": _iso(epoch), "type": "response_item",
                       "payload": {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": text}]}})


def _user(text, epoch):
    return json.dumps({"timestamp": _iso(epoch), "type": "event_msg",
                       "payload": {"type": "user_message", "message": text}})


class SwitchingTranscript(unittest.TestCase):
    def _switch_to(self, b, path):
        b._resolve_transcript = lambda: path
        b._session_cwd = lambda: str(path.parent)
        b._maybe_reresolve()
        self.assertEqual(b._transcript, path, "the switch itself did not happen")

    def test_outside_a_turn_the_cursor_starts_at_the_end(self):
        """Mutation: ``self._tpos = 0`` after the switch → the whole history is re-read."""
        with tempfile.TemporaryDirectory() as d:
            b = _codex_bridge(d)
            p = Path(d) / "rollout-x.jsonl"
            now = time.time()
            p.write_text("\n".join([_user("[TG] old", now - 900), _assistant("old answer", now - 890)]) + "\n", "utf-8")
            self._switch_to(b, p)
            self.assertEqual(b._tpos, p.stat().st_size)
            self.assertEqual(b._turn_tpos, p.stat().st_size)

    def test_mid_turn_only_records_stamped_after_the_turn_began_are_read(self):
        """The replayed history (old timestamps) is skipped; the live prompt and its answer are not.

        Mutation: drop the timestamp scan (``pos = size`` only) → the answer written before the
        switch is never read; or start at 0 → ``old answer`` is forwarded as the reply."""
        with tempfile.TemporaryDirectory() as d:
            b = _codex_bridge(d)
            p = Path(d) / "rollout-x.jsonl"
            now = time.time()
            lines = [_user("[TG] old", now - 900), _assistant("old answer", now - 890),
                     _user("[TG] jsi tam?", now - 1), _assistant("Ahoj, jsem tady.", now)]
            p.write_text("\n".join(lines) + "\n", "utf-8")
            b._turn_active.set()
            b._turn_started_wall = now - 1.5
            self._switch_to(b, p)
            expected = sum(len(l.encode("utf-8")) + 1 for l in lines[:2])
            self.assertEqual(b._tpos, expected, "cursor is not at the first record of this turn")
            self.assertEqual(b._turn_tpos, expected)
            with open(p, "rb") as f:
                f.seek(b._tpos)
                rest = f.read().decode("utf-8")
            self.assertNotIn("old answer", rest)
            self.assertIn("Ahoj, jsem tady.", rest)

    def test_mid_turn_with_nothing_new_yet_starts_at_the_end(self):
        with tempfile.TemporaryDirectory() as d:
            b = _codex_bridge(d)
            p = Path(d) / "rollout-x.jsonl"
            now = time.time()
            p.write_text(_assistant("old answer", now - 900) + "\n", "utf-8")
            b._turn_active.set()
            b._turn_started_wall = now
            self._switch_to(b, p)
            self.assertEqual(b._tpos, p.stat().st_size)


class OwnRolloutWins(unittest.TestCase):
    def test_the_sessions_own_rollout_beats_a_newer_sibling_in_the_same_cwd(self):
        """Mutation: drop the ``sid`` branch in ``_newest_rollout`` → the newer sibling wins."""
        with tempfile.TemporaryDirectory() as d:
            b = _codex_bridge(d)
            base = Path(d) / "sessions"
            own_id = "019f8f51-f896-7011-b4a9-78afd78d0f23"
            sibling_id = "019e31f8-40d3-7021-a8d8-16928d5022c5"
            own = base / "2026" / "07" / f"rollout-2026-07-23T16-12-21-{own_id}.jsonl"
            sib = base / "2026" / "05" / f"rollout-2026-05-16T20-06-53-{sibling_id}.jsonl"
            for p, mtime in ((own, 1000.0), (sib, 5000.0)):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": d}}) + "\n", "utf-8")
                os.utime(p, (mtime, mtime))
            b._codex_sessions_dir = lambda: base
            b._session_cwd = lambda: d
            b._session_uuid = lambda: own_id
            self.assertEqual(b._newest_rollout(), own)
            b._session_uuid = lambda: None          # a fresh session with no id yet → cwd/mtime rule
            self.assertEqual(b._newest_rollout(), sib)


if __name__ == "__main__":
    unittest.main()
