import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from add_history_answer_translations import translate_database, translate_manifest


class HistoryTranslationsTest(unittest.TestCase):
    def test_database_preserves_original_answers_existing_translations_and_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.sqlite"
            with sqlite3.connect(path) as db:
                db.executescript("""
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE decks(id INTEGER PRIMARY KEY, code TEXT);
                    CREATE TABLE cards(id INTEGER PRIMARY KEY, deck_id INTEGER, type TEXT,
                        back_ru_translation TEXT, history_answer_pl TEXT,
                        history_answer_ru TEXT, updated_at TEXT);
                    CREATE TABLE review_state(card_id INTEGER, repetition INTEGER, due_at TEXT);
                    INSERT INTO decks VALUES (1, 'history'), (2, 'personal');
                    INSERT INTO cards VALUES (1, 1, 'HISTORY_FACT', 'Писатель.', NULL, NULL, 'old');
                    INSERT INTO cards VALUES (2, 1, 'HISTORY_FACT', 'Ответ.', 'Gotowe.', 'Ответ.', 'old');
                    INSERT INTO cards VALUES (3, 2, 'HISTORY_FACT', 'Личное.', NULL, NULL, 'old');
                    INSERT INTO cards VALUES (4, 1, 'WORD', 'Слово.', NULL, NULL, 'old');
                    INSERT INTO review_state VALUES (1, 5, '2030-01-01');
                """)
            translations = {'Писатель.': 'Pisarz.'}
            self.assertEqual(1, translate_database(path, translations, 'history'))
            with sqlite3.connect(path) as db:
                self.assertIsNone(db.execute('SELECT history_answer_pl FROM cards WHERE id=1').fetchone()[0])
            self.assertEqual(1, translate_database(path, translations, 'history', apply=True))
            self.assertEqual(0, translate_database(path, translations, 'history', apply=True))
            with sqlite3.connect(path) as db:
                self.assertEqual([
                    (1, 'Писатель.', 'Pisarz.', 'Писатель.'),
                    (2, 'Ответ.', 'Gotowe.', 'Ответ.'),
                    (3, 'Личное.', None, None),
                    (4, 'Слово.', None, None),
                ], db.execute('SELECT id, back_ru_translation, history_answer_pl, history_answer_ru FROM cards').fetchall())
                self.assertEqual([(1, 5, '2030-01-01')], db.execute('SELECT * FROM review_state').fetchall())
            backups = list(Path(directory).glob('*.before-polish-*.sqlite'))
            self.assertEqual(1, len(backups))
            with sqlite3.connect(backups[0]) as backup:
                self.assertIsNone(backup.execute('SELECT history_answer_pl FROM cards WHERE id=1').fetchone()[0])

    def test_manifest_is_complete_before_writing_and_preserves_keys_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.jsonl'
            output = Path(directory) / 'translated.jsonl'
            card = dict(type='HISTORY_FACT', deckCode='history', canonicalKey='stable-key',
                        frontTextPl='Kto to?', backRuTranslation='Писатель.', tags=['pdf'],
                        audioVariants=[], imagePath='photo.jpg')
            source.write_text(json.dumps(card), encoding='utf-8')
            with self.assertRaises(ValueError):
                translate_manifest(source, output, {}, 'history')
            self.assertFalse(output.exists())
            self.assertEqual(1, translate_manifest(source, output, {'Писатель.': 'Pisarz.'}, 'history'))
            translated = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual('Pisarz.', translated.pop('historyAnswerPl'))
            self.assertEqual('Писатель.', translated.pop('historyAnswerRu'))
            self.assertEqual(card, translated)
            self.assertEqual(card, json.loads(source.read_text(encoding='utf-8')))


if __name__ == '__main__':
    unittest.main()
