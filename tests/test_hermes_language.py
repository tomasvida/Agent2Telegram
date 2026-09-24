"""Hermes must get the transcription LANGUAGE, not just the key.

Live case 2026-09-03: `agent2telegram set-elevenlabs` was run twice on a fresh server — once
with `cs`, once with auto-detect — and Hermes transcribed Czech speech into English both times.
It had been handed the key but never the language, so it kept its own default. That reads as a
broken transcriber, not as a missing setting.

Each test names the mutation that turns it red.
"""
import subprocess
import unittest
from unittest import mock

from agent2telegram import wizard


class _Beh:
    """Zaznamenává volání `hermes` a předstírá zvolenou generaci CLI."""

    def __init__(self, force_ok: bool):
        self.force_ok = force_ok
        self.volani: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.volani.append(list(argv))
        if "--force" in argv and not self.force_ok:
            return subprocess.CompletedProcess(argv, 2, "", "hermes: error: unrecognized arguments: --force")
        if "--force" not in argv and self.force_ok:
            return subprocess.CompletedProcess(argv, 2, "", "hermes: error: value already set")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    def nastavene(self) -> dict:
        out = {}
        for v in self.volani:
            if v[1:3] == ["config", "set"]:
                out[v[-2]] = v[-1]
        return out


class HermesGetsTheLanguageToo(unittest.TestCase):
    def _spust(self, force_ok: bool, language: str) -> _Beh:
        beh = _Beh(force_ok)
        with mock.patch("shutil.which", return_value="/usr/bin/hermes"), \
             mock.patch("subprocess.run", side_effect=beh):
            wizard._also_configure_hermes("sk_test", language)
        return beh

    def test_language_reaches_hermes(self):
        """Mutation: drop the `stt.elevenlabs.language_code` call → Czech is transcribed as English."""
        nastaveno = self._spust(force_ok=False, language="cs").nastavene()
        self.assertEqual(nastaveno.get("stt.elevenlabs.language_code"), "cs")
        self.assertEqual(nastaveno.get("ELEVENLABS_API_KEY"), "sk_test")

    def test_the_provider_is_switched_to_elevenlabs(self):
        """Hermes' STT provider defaults to "local" (faster-whisper). Without switching it, the
        key we just handed over is never used at all — which is exactly how Czech speech kept
        coming back as English.

        Mutation: drop the `stt.provider` call → the key stays unused and nothing changes."""
        nastaveno = self._spust(force_ok=False, language="cs").nastavene()
        self.assertEqual(nastaveno.get("stt.provider"), "elevenlabs")

    def test_the_local_transcriber_gets_the_language_too(self):
        """Hermes' local transcriber defaults to "en", not to auto-detect, so a build that
        ignores the provider switch keeps turning Czech into English.

        Mutation: drop the `stt.local.language` call → that fallback stays English."""
        nastaveno = self._spust(force_ok=False, language="cs").nastavene()
        self.assertEqual(nastaveno.get("stt.local.language"), "cs")

    def test_auto_detect_sets_no_language(self):
        """A blank language means auto-detect — writing "" would pin Scribe to nothing.

        Mutation: call the setter unconditionally → an empty language_code lands in the config."""
        nastaveno = self._spust(force_ok=False, language="").nastavene()
        self.assertNotIn("stt.elevenlabs.language_code", nastaveno)
        self.assertEqual(nastaveno.get("ELEVENLABS_API_KEY"), "sk_test")
        # …but the LOCAL transcriber must still be told, because its default is "en", not
        # auto-detect. Leaving it alone would turn "auto-detect" into "English".
        # Mutation: skip the local setter on a blank language → auto-detect means English.
        self.assertEqual(nastaveno.get("stt.local.language"), "")

    def test_works_on_a_build_that_rejects_force(self):
        """The older CLI answers `--force` with "unrecognized arguments" and exit 2.

        Mutation: keep only the `--force` form → nothing is set on that build."""
        beh = self._spust(force_ok=False, language="cs")
        self.assertEqual(beh.nastavene().get("stt.elevenlabs.language_code"), "cs")

    def test_works_on_a_build_that_requires_force(self):
        """Mutation: keep only the plain form → nothing is set on the newer build."""
        beh = self._spust(force_ok=True, language="cs")
        self.assertEqual(beh.nastavene().get("stt.elevenlabs.language_code"), "cs")
        self.assertTrue(any("--force" in v for v in beh.volani))

    def test_a_zero_exit_with_an_argparse_error_is_not_success(self):
        """Some builds print "unrecognized arguments" and still exit 0. Trusting the code alone
        reported success while nothing was set.

        Mutation: check only `returncode == 0` → the fallback never runs."""
        volani = []

        def beh(argv, **kw):
            volani.append(list(argv))
            if "--force" not in argv:
                return subprocess.CompletedProcess(argv, 0, "", "hermes: error: unrecognized arguments: -x")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with mock.patch("shutil.which", return_value="/usr/bin/hermes"), \
             mock.patch("subprocess.run", side_effect=beh):
            self.assertTrue(wizard._hermes_set("/usr/bin/hermes", "k", "v"))
        self.assertTrue(any("--force" in v for v in volani), "the fallback was never tried")


if __name__ == "__main__":
    unittest.main()
