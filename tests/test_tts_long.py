"""Long narrations (2026-09-05): a long spoken reply is synthesized in sentence-sized pieces.

Mutation that must turn this red: in tts.split_for_tts drop the `len(parts[-1]) + 1 + len(sentence)
<= max_chars` guard (always merge) → a chunk longer than max_chars → test_split_respects_limit fails.
"""
import unittest
from unittest import mock

from agent2telegram import tts


class SplitForTtsTests(unittest.TestCase):
    def test_split_respects_limit_and_sentence_boundaries(self):
        text = "První věta je krátká. Druhá věta je o něco delší než ta první! Třetí? Čtvrtá věta končí tečkou."
        parts = tts.split_for_tts(text, max_chars=45)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 45)
            self.assertRegex(part, r"[.!?…]$")          # cut only at sentence ends
        self.assertEqual(" ".join(parts), text)         # nothing lost, nothing reordered

    def test_single_overlong_sentence_is_cut_at_a_space(self):
        text = "slovo " * 40
        parts = tts.split_for_tts(text.strip(), max_chars=50)
        self.assertTrue(all(len(p) <= 50 for p in parts))
        self.assertTrue(all(" " not in (p[-1], p[0]) for p in parts))
        self.assertEqual(" ".join(parts).split(), text.split())

    def test_empty_and_bad_limit(self):
        self.assertEqual(tts.split_for_tts("   "), [])
        with self.assertRaises(ValueError):
            tts.split_for_tts("x", max_chars=0)


class SynthesizeLongTests(unittest.TestCase):
    def test_one_request_per_piece_in_order(self):
        calls = []

        def fake_synthesize(piece, **kw):
            calls.append(piece)
            return piece.encode("utf-8")

        text = "Věta jedna. Věta dva. Věta tři."
        with mock.patch.object(tts, "synthesize", side_effect=fake_synthesize):
            segments = tts.synthesize_long(text, api_key="k", voice_id="v", max_chars=12)
        self.assertEqual(calls, tts.split_for_tts(text, max_chars=12))
        self.assertEqual(segments, [c.encode("utf-8") for c in calls])
        self.assertGreater(len(segments), 1)

    def test_short_text_is_a_single_segment(self):
        with mock.patch.object(tts, "synthesize", return_value=b"mp3") as syn:
            self.assertEqual(tts.synthesize_long("Krátká věta.", api_key="k", voice_id="v"), [b"mp3"])
        self.assertEqual(syn.call_count, 1)

    def test_nothing_to_speak(self):
        with self.assertRaises(tts.TTSError):
            tts.synthesize_long("", api_key="k", voice_id="v")


if __name__ == "__main__":
    unittest.main()
