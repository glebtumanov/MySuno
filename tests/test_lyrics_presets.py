"""Тесты нормализации текста песни и проверки конфликтов тегов."""
import unittest

from mysuno import lyrics, presets


class LyricsTests(unittest.TestCase):
    def test_russian_section_tags_become_english(self):
        text, _ = lyrics.normalize("[Куплет 1]\nСтрока раз\nСтрока два\n\n[Припев - громкий]\nЛа-ла-ла\n\n[Бридж]\nТихо\n")
        self.assertIn("[Verse 1]", text)
        self.assertIn("[Chorus - громкий]", text)
        self.assertIn("[Bridge]", text)
        self.assertNotIn("Куплет", text)

    def test_unknown_tags_are_untouched(self):
        text, _ = lyrics.normalize("[raspy vocal]\nПривет мир")
        self.assertIn("[raspy vocal]", text)

    def test_blank_line_before_each_tag(self):
        text, _ = lyrics.normalize("[Verse]\nодин\n[Chorus]\nдва")
        self.assertIn("один\n\n[Chorus]", text)

    def test_auto_structure_marks_repeated_block_as_chorus(self):
        raw = "Дождь стучит по крыше\nТихо город спит\n\nЯ тебя услышу\nСердце говорит\n\nДождь стучит по крыше\nТихо город спит"
        text, warns = lyrics.normalize(raw)
        self.assertTrue(text.startswith("[Verse 1]"))
        self.assertIn("[Verse 2]", text)
        self.assertEqual(text.count("[Chorus]"), 1)
        self.assertTrue(any("автоматически" in w for w in warns))

    def test_words_are_never_changed(self):
        raw = "[Куплет]\nНа небе луна\nСветит как огонь"
        text, _ = lyrics.normalize(raw)
        self.assertIn("На небе луна\nСветит как огонь", text)

    def test_long_line_warning(self):
        _, warns = lyrics.normalize("[Verse]\nЯ иду по длинной-длинной дороге в далёкие незнакомые страны мечты")
        self.assertTrue(any("слишком длинная" in w for w in warns))

    def test_syllable_count(self):
        self.assertEqual(lyrics.count_syllables("Дождь стучит по крыше"), 6)   # дождь·стучит(2)·по·крыше(2)


class ConflictTests(unittest.TestCase):
    def test_conflicting_moods_and_genres_detected(self):
        w = presets.check_conflicts(["ambient", "metal"], ["calm", "energetic"])
        self.assertEqual(len(w), 2)

    def test_too_many_genres(self):
        w = presets.check_conflicts(["pop", "rock", "jazz", "folk"], [])
        self.assertTrue(any("жанров" in x for x in w))

    def test_clean_selection_has_no_warnings(self):
        self.assertEqual(presets.check_conflicts(["pop", "ballad"], ["sad", "romantic"]), [])

    def test_conflict_tables_use_known_ids(self):
        genres = {g[0] for g in presets.GENRES}
        moods = {m[0] for m in presets.MOODS}
        for a, b in presets.GENRE_CONFLICTS:
            self.assertIn(a, genres), self.assertIn(b, genres)
        for a, b in presets.MOOD_CONFLICTS:
            self.assertIn(a, moods), self.assertIn(b, moods)


if __name__ == "__main__":
    unittest.main()
