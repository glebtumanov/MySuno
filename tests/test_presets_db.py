"""Тесты словаря пресетов и библиотеки (без GPU и моделей): python -m unittest discover -s tests"""
import tempfile
import unittest
from pathlib import Path

from mysuno import presets
from mysuno.db import Library


class UniqueTitleTests(unittest.TestCase):
    def test_suffixes(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Library(Path(d) / "t.db")
            add = lambda title: lib.add_track(title=lib.unique_title(title), filename="x.flac", fmt="flac")["title"]  # noqa: E731
            self.assertEqual([add("Рок, Джаз") for _ in range(3)], ["Рок, Джаз", "Рок, Джаз 1", "Рок, Джаз 2"])
            self.assertEqual(add("Рок"), "Рок")   # «Рок, Джаз …» не считается занятым «Рок»
            self.assertEqual(add("100%_hit"), "100%_hit")
            self.assertEqual(add("100%_hit"), "100%_hit 1")   # % и _ в названии не работают как шаблон LIKE
            lib._conn.close()


class AutoTitleTests(unittest.TestCase):
    def test_genres_first(self):
        from mysuno.jobs import _auto_title

        self.assertEqual(_auto_title({"genres": ["rock", "jazz"], "prompt": "дождь"}), "Рок, Джаз")
        self.assertEqual(_auto_title({"genres": [], "prompt": "дождь"}), "дождь")
        self.assertEqual(_auto_title({}), "Без названия")


class BuildCaptionTests(unittest.TestCase):
    def test_genres_vocal_description(self):
        caption, neg = presets.build_caption(["rock", "jazz"], ["sad"], "female", "rain in autumn")
        self.assertIn("rock", caption)
        self.assertIn("jazz", caption)
        self.assertIn("sad", caption)
        self.assertIn("female vocals", caption)
        self.assertTrue(caption.endswith("rain in autumn"))
        self.assertEqual(neg, "NO USER INPUT")

    def test_negative_genres_removed_from_positive_and_passed_to_lm(self):
        caption, neg = presets.build_caption(["rock", "metal"], [], "auto", "", ["metal", "punk"])
        self.assertNotIn("heavy metal", caption)
        self.assertIn("rock", caption)
        self.assertEqual(neg, "heavy metal, punk rock")

    def test_unknown_ids_are_ignored(self):
        caption, neg = presets.build_caption(["nope"], ["nope"], "nope", "")
        self.assertEqual(caption, "")
        self.assertEqual(neg, "NO USER INPUT")

    def test_caption_is_limited(self):
        caption, _ = presets.build_caption([g[0] for g in presets.GENRES], [], "auto", "x" * 300)
        self.assertLessEqual(len(caption), presets.CAPTION_LIMIT)

    def test_instrumental_vocal(self):
        self.assertTrue(presets.is_instrumental("none"))
        self.assertFalse(presets.is_instrumental("male"))
        self.assertFalse(presets.is_instrumental("unknown-id"))

    def test_vocal_types_cover_requirements(self):
        ids = {v[0] for v in presets.VOCALS}
        self.assertTrue({"male", "female", "child", "choir"} <= ids)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.lib = Library(Path(self._tmp.name) / "t.db")

    def tearDown(self):
        self.lib._conn.close()
        self._tmp.cleanup()

    def _track(self, title="t", folder_id=None):
        return self.lib.add_track(title=title, filename=f"{title}.flac", fmt="flac", folder_id=folder_id,
                                  duration=10.0, prompt="запрос " + title, caption="rock")

    def test_folder_crud_and_unique_names(self):
        f = self.lib.create_folder("  Рок   баллады ")
        self.assertEqual(f["name"], "Рок баллады")
        with self.assertRaises(ValueError):
            self.lib.create_folder("Рок баллады")
        with self.assertRaises(ValueError):
            self.lib.create_folder("   ")
        self.lib.rename_folder(f["id"], "Джаз")
        self.assertEqual(self.lib.list_folders()["folders"][0]["name"], "Джаз")
        with self.assertRaises(KeyError):
            self.lib.rename_folder(999, "x")

    def test_tracks_filter_by_folder(self):
        f = self.lib.create_folder("A")
        self._track("one", f["id"])
        self._track("two")
        self.assertEqual(len(self.lib.list_tracks("all")), 2)
        self.assertEqual([t["title"] for t in self.lib.list_tracks(f["id"])], ["one"])
        self.assertEqual([t["title"] for t in self.lib.list_tracks("none")], ["two"])
        listing = self.lib.list_folders()
        self.assertEqual((listing["total"], listing["unfiled"], listing["folders"][0]["count"]), (2, 1, 1))

    def test_delete_folder_moves_tracks_to_unfiled(self):
        f = self.lib.create_folder("A")
        t = self._track("one", f["id"])
        self.lib.delete_folder(f["id"])
        self.assertIsNone(self.lib.get_track(t["id"])["folder_id"])

    def test_move_and_rename_track(self):
        f = self.lib.create_folder("A")
        t = self._track("one")
        t = self.lib.update_track(t["id"], folder_id=f["id"])
        self.assertEqual(t["folder_id"], f["id"])
        t = self.lib.update_track(t["id"], title="Новое имя")
        self.assertEqual((t["title"], t["folder_id"]), ("Новое имя", f["id"]))  # папка не сброшена
        t = self.lib.update_track(t["id"], folder_id=None)
        self.assertIsNone(t["folder_id"])
        with self.assertRaises(KeyError):
            self.lib.update_track(t["id"], folder_id=12345)

    def test_search_and_delete(self):
        a = self._track("alpha")
        self._track("beta")
        self.assertEqual([t["title"] for t in self.lib.list_tracks("all", "alp")], ["alpha"])
        self.assertEqual(self.lib.delete_track(a["id"]), "alpha.flac")
        self.assertIsNone(self.lib.get_track(a["id"]))
        with self.assertRaises(KeyError):
            self.lib.delete_track(a["id"])


if __name__ == "__main__":
    unittest.main()
