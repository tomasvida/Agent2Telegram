"""Transcription language, key validation, and handing the key to Hermes.

A three-second Czech voice note came back as the English word "Down": Scribe was never told
the language, so it guessed — and on a short clip it guesses badly.
"""
import types
import unittest

from agent2telegram import stt, wizard


class LanguageIsSent(unittest.TestCase):
    def _capture(self, **kw):
        """Run a transcription against a fake opener and return the request body."""
        telo = {}

        class FakeResp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner): return b'{"text": "ahoj"}'

        class FakeOpener:
            def open(self_inner, req, timeout=None):
                telo["body"] = req.data.decode("utf-8", "replace")
                return FakeResp()

        text = stt.transcribe_elevenlabs(b"audio", api_key="sk_x", opener=FakeOpener(), **kw)
        return text, telo["body"]

    def test_language_appears_in_the_request(self):
        text, body = self._capture(language="cs")
        self.assertEqual(text, "ahoj")
        self.assertIn("language_code", body)
        self.assertIn("cs", body)

    def test_no_language_means_no_field(self):
        """Auto-detect stays available — we must not force a language on everyone."""
        _, body = self._capture()
        self.assertNotIn("language_code", body)

    def test_model_is_always_sent(self):
        _, body = self._capture(language="de")
        self.assertIn("scribe_v1", body)

    def test_dispatcher_passes_the_language_through(self):
        videno = {}
        orig = stt.transcribe_elevenlabs
        stt.transcribe_elevenlabs = lambda audio, **kw: videno.update(kw) or "x"
        try:
            stt.transcribe(b"a", api_key="sk_x", language="cs")
        finally:
            stt.transcribe_elevenlabs = orig
        self.assertEqual(videno.get("language"), "cs")


class KeyShape(unittest.TestCase):
    """The web UI offers "Copy Key ID" but never shows the key again, so pasting the ID is
    the easy mistake. Caught here it costs one retype; caught later it looks like a broken
    bridge answering HTTP 400."""

    def test_key_id_is_rejected(self):
        self.assertFalse(stt.looks_like_api_key("a21b9f0c4e2d4f8b9c1a2b3c4d5e6f70"))

    def test_real_key_is_accepted(self):
        self.assertTrue(stt.looks_like_api_key("sk_abc123"))

    def test_blank_is_rejected(self):
        self.assertFalse(stt.looks_like_api_key(""))


class HandsKeyToHermes(unittest.TestCase):
    """Hermes runs its own gateway with its own transcription: a key set for the bridge does
    nothing for it, and nobody would guess why voice stayed deaf on one of them."""

    def setUp(self):
        self.calls = []
        self.orig_which = wizard.__dict__.get("shutil")

    def _run(self, hermes_path, rc=0):
        import shutil as real_shutil
        import subprocess as real_sub
        orig_which, orig_run = real_shutil.which, real_sub.run
        real_shutil.which = lambda x: hermes_path if x == "hermes" else orig_which(x)
        real_sub.run = lambda args, **kw: (
            self.calls.append(args),
            types.SimpleNamespace(returncode=rc, stdout="", stderr=""))[1]
        try:
            wizard._also_configure_hermes("sk_secret")
        finally:
            real_shutil.which, real_sub.run = orig_which, orig_run

    def test_sets_key_and_restarts_when_hermes_is_present(self):
        """Asserts WHAT was called, not in which position. Pinning the restart to `calls[1]`
        broke the moment another setting was added between the key and the restart, even though
        the behaviour was correct — a test that fails on unrelated additions hides real ones."""
        self._run("/usr/bin/hermes")
        self.assertTrue(any("ELEVENLABS_API_KEY" in a for a in self.calls),
                        "the key was never set")
        self.assertTrue(any("restart" in a for a in self.calls),
                        "the gateway was never restarted")

    def test_does_nothing_when_hermes_is_absent(self):
        self._run(None)
        self.assertEqual(self.calls, [])

    def test_a_failing_hermes_does_not_raise(self):
        self._run("/usr/bin/hermes", rc=1)   # must not blow up the command that succeeded


if __name__ == "__main__":
    unittest.main()
