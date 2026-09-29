#!/usr/bin/env python3
import argparse
import json
import re
import unicodedata
from pathlib import Path


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower()
    t = re.sub(r"[^a-z0-9а-яёąćęłńóśźż ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def diacritics_score(text: str) -> int:
    return sum(text.count(ch) for ch in "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")


def confidence(card: dict) -> float:
    attrs = card.get("sourceAttribution") or []
    if not attrs:
        return 0.0
    return max(float(a.get("confidenceScore", 0.0)) for a in attrs)


def card_score(card: dict) -> float:
    back = card.get("backRuTranslation", "")
    return confidence(card) * 100.0 + diacritics_score(back) * 0.7 + len(back) * 0.01


def main() -> None:
    parser = argparse.ArgumentParser(description="Dedupe cards by normalized front/back")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    inp = Path(args.input)
    out = Path(args.output)

    best = {}
    rows = 0

    for line in inp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows += 1
        card = json.loads(line)
        fp = f"{norm(card.get('frontTextPl',''))}|{norm(card.get('backRuTranslation',''))}"
        if fp in best:
            existing = best[fp]
            if card_score(card) > card_score(existing):
                winner, loser = card, existing
            else:
                winner, loser = existing, card

            winner["tags"] = sorted(set(winner.get("tags", [])) | set(loser.get("tags", [])))
            winner["sourceAttribution"] = (winner.get("sourceAttribution") or []) + (loser.get("sourceAttribution") or [])
            best[fp] = winner
        else:
            best[fp] = card

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for card in best.values():
            fh.write(json.dumps(card, ensure_ascii=False) + "\n")

    print(f"dedupe_complete read={rows} unique={len(best)} output={out}")


if __name__ == "__main__":
    main()
