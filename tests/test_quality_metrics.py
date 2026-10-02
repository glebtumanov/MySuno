"""Тесты метрик качества, не требующих моделей: сопоставление текста с расшифровкой и эвристики шума."""
import unittest

import numpy as np

from mysuno import quality

LY = "[Verse 1]\nДождь стучит по крыше\nТихо город спит\n\n[Chorus]\nОсень, осень, осень\nТы со мной одна"


class LyricsMatchTests(unittest.TestCase):
    def test_exact_transcript_scores_high(self):
        r = quality.lyrics_match("Дождь стучит по крыше тихо город спит осень осень осень ты со мной одна", LY)
        self.assertGreater(r["lyrics_score"], 0.95)

    def test_slightly_wrong_words_are_tolerated(self):
        r = quality.lyrics_match("Дождь тучит по крыше тихо город спит", LY)
        self.assertGreater(r["precision"], 0.95)

    def test_garbage_scores_low(self):
        r = quality.lyrics_match("совсем другие слова про машины и самолёты", LY)
        self.assertLess(r["lyrics_score"], 0.25)

    def test_empty_transcript_is_zero(self):
        self.assertEqual(quality.lyrics_match("", LY)["lyrics_score"], 0.0)

    def test_structure_tags_are_not_counted_as_lyrics(self):
        r = quality.lyrics_match("verse chorus", "[Verse]\nпривет\n[Chorus]\nмир")
        self.assertEqual(r["precision"], 0.0)


class NoiseHeuristicsTests(unittest.TestCase):
    sr = 48000

    def test_white_noise_is_flagged_and_tone_is_not(self):
        rng = np.random.default_rng(0)
        noise = rng.normal(0, 0.1, self.sr * 40).astype("float32")
        t = np.arange(self.sr * 40) / self.sr
        tone = (0.3 * np.sin(2 * np.pi * 440 * t) + 0.1 * np.sin(2 * np.pi * 660 * t)).astype("float32")
        n, m = quality.spectral_stats(noise, self.sr), quality.spectral_stats(tone, self.sr)
        self.assertGreater(n["noise_ratio"], 0.9)
        self.assertEqual(m["noise_ratio"], 0.0)
        self.assertGreater(n["flat_median"], 10 * m["flat_median"])

    def test_stationary_drone_has_high_static_share(self):
        t = np.arange(self.sr * 60) / self.sr
        drone = (0.3 * np.sin(2 * np.pi * 220 * t)).astype("float32")
        self.assertGreater(quality.spectral_stats(drone, self.sr)["static"], 0.8)

    def test_overall_penalises_noise(self):
        base = dict(CE=7.5, PQ=8.0, CE_p10=7.2, noise_ratio=0.0, static=0.0)
        self.assertLess(quality.overall(dict(base, noise_ratio=0.5)), quality.overall(base) - 1.5)

    def test_total_score_rewards_intelligible_vocals(self):
        base = dict(CE=7.5, PQ=8.0, CE_p10=7.2, noise_ratio=0.0, static=0.0)
        good = quality.total_score(base, {"lyrics_score": 0.9})
        bad = quality.total_score(base, {"lyrics_score": 0.4})
        self.assertGreater(good - bad, 1.5)


if __name__ == "__main__":
    unittest.main()
