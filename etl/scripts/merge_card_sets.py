#!/usr/bin/env python3
import argparse
import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path


@dataclass
class CardWrap:
    card: dict
    front_norm: str
    back_norm: str
    score: float


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower()
    t = re.sub(r"[^a-z0-9а-яёąćęłńóśźż ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def confidence(card: dict) -> float:
    attrs = card.get("sourceAttribution") or []
    if not attrs:
        return 0.0
    return max(float(a.get("confidenceScore", 0.0)) for a in attrs)


def diacritics(text: str) -> int:
    return sum(text.count(ch) for ch in "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")


def score(card: dict) -> float:
    back = card.get("backRuTranslation", "")
    return confidence(card) * 100.0 + diacritics(back) * 0.7 + len(back) * 0.02


def is_duplicate(a: CardWrap, b: CardWrap) -> bool:
    if a.front_norm == b.front_norm and a.back_norm == b.back_norm:
        return True

    front_ratio = SequenceMatcher(None, a.front_norm, b.front_norm).ratio()
    back_ratio = SequenceMatcher(None, a.back_norm, b.back_norm).ratio()

    return front_ratio >= 0.94 and back_ratio >= 0.90


def merge(winner: dict, loser: dict) -> dict:
    winner["tags"] = sorted(set(winner.get("tags", [])) | set(loser.get("tags", [])))
    winner["sourceAttribution"] = (winner.get("sourceAttribution") or []) + (loser.get("sourceAttribution") or [])
    return winner


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge two card sets with fuzzy dedupe")
    parser.add_argument("--a", required=True)
    parser.add_argument("--b", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    rows = load_jsonl(Path(args.a)) + load_jsonl(Path(args.b))

    wrapped = [
        CardWrap(
            card=r,
            front_norm=norm(r.get("frontTextPl", "")),
            back_norm=norm(r.get("backRuTranslation", "")),
            score=score(r),
        )
        for r in rows
    ]
    wrapped.sort(key=lambda w: w.score, reverse=True)

    selected: list[CardWrap] = []
    for cand in wrapped:
        dup_idx = None
        for i, cur in enumerate(selected):
            if is_duplicate(cand, cur):
                dup_idx = i
                break

        if dup_idx is None:
            selected.append(cand)
        else:
            existing = selected[dup_idx]
            if cand.score > existing.score:
                selected[dup_idx] = CardWrap(
                    card=merge(cand.card, existing.card),
                    front_norm=cand.front_norm,
                    back_norm=cand.back_norm,
                    score=cand.score,
                )
            else:
                existing.card = merge(existing.card, cand.card)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for item in selected:
            fh.write(json.dumps(item.card, ensure_ascii=False) + "\n")

    print(f"merge_sets_complete read={len(rows)} unique={len(selected)} output={out}")


if __name__ == "__main__":
    main()
