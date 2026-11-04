#!/usr/bin/env python3
"""Add bundled Polish translations without replacing Russian answers or review data."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


DEFAULT_TRANSLATIONS = Path(__file__).resolve().parents[1] / "translations/history_answers.pl.json"


def translated_answers(card: dict, translations: dict[str, str]) -> tuple[str, str] | None:
    if card["type"] != "HISTORY_FACT" or (card.get("historyAnswerPl") or "").strip():
        return None
    russian = (card.get("historyAnswerRu") or "").strip() or card["backRuTranslation"]
    polish = translations.get(russian)
    if not isinstance(polish, str) or not polish.strip():
        raise ValueError(f"No Polish translation for answer: {russian!r}")
    return polish, russian


def translate_manifest(source: Path, output: Path, translations: dict[str, str], deck_code: str) -> int:
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    changed = 0
    for card in rows:
        if card["deckCode"] != deck_code:
            continue
        answers = translated_answers(card, translations)
        if answers:
            card["historyAnswerPl"], card["historyAnswerRu"] = answers
            changed += 1
    # Validate every row before writing; leave the source available for rollback.
    if source.resolve() == output.resolve():
        raise ValueError("Use a separate output manifest")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return changed


def translate_database(path: Path, translations: dict[str, str], deck_code: str, apply: bool = False) -> int:
    # mode=rw prevents accidentally creating a database at a misspelled path.
    with sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        rows = db.execute(
            """SELECT c.id, c.type, c.back_ru_translation AS backRuTranslation,
                      c.history_answer_pl AS historyAnswerPl, c.history_answer_ru AS historyAnswerRu
               FROM cards c JOIN decks d ON d.id = c.deck_id
               WHERE d.code = ? AND c.type = 'HISTORY_FACT'""", (deck_code,)
        ).fetchall()
        updates = []
        for row in rows:
            answers = translated_answers(dict(row), translations)
            if answers:
                updates.append((*answers, row["id"]))
        if apply and updates:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = path.with_name(f"{path.name}.before-polish-{timestamp}.sqlite")
            # A separate reader can back up the pre-update state while this connection
            # holds the write reservation (including databases using WAL).
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as reader:
                with sqlite3.connect(backup) as destination:
                    reader.backup(destination)
            print(f"backup={backup}")
            db.executemany(
                """UPDATE cards SET history_answer_pl = ?, history_answer_ru = ?,
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""", updates
            )
        return len(updates)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Source JSONL manifest")
    source.add_argument("--db", type=Path, help="Existing SQLite database (preview unless --apply)")
    parser.add_argument("--output", type=Path, help="Separate output JSONL manifest")
    parser.add_argument("--deck-code", default="history")
    parser.add_argument("--translations", type=Path, default=DEFAULT_TRANSLATIONS)
    parser.add_argument("--apply", action="store_true", help="Back up and update the database")
    args = parser.parse_args()
    translations = json.loads(args.translations.read_text(encoding="utf-8"))
    if args.input:
        if not args.output or args.apply:
            parser.error("--input requires --output and does not use --apply")
        count = translate_manifest(args.input, args.output, translations, args.deck_code)
    else:
        if args.output:
            parser.error("--output requires --input")
        count = translate_database(args.db, translations, args.deck_code, args.apply)
    print(f"cards_translated={count} mode={'manifest' if args.input else 'apply' if args.apply else 'preview'}")


if __name__ == "__main__":
    main()
