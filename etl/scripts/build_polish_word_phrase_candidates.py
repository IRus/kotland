#!/usr/bin/env python3
"""Build WORD + PHRASE candidate JSONL files from multiple sources.

Approach A (words): Wiktionary/OpenSubtitles Polish frequency list.
Approach B (phrases): Tatoeba Polish sentences, filtered and frequency-ranked.
"""

from __future__ import annotations

import argparse
import bz2
import json
import re
import statistics
import urllib.request
from dataclasses import dataclass
from pathlib import Path


WIKTIONARY_FREQUENCY_URL = (
    "https://en.wiktionary.org/w/index.php?title=Wiktionary:Frequency_lists/Polish_wordlist&action=raw"
)
TATOEBA_POL_SENTENCES_URL = "https://downloads.tatoeba.org/exports/per_language/pol/pol_sentences.tsv.bz2"

WORD_RE = re.compile(r"^[a-ząćęłńóśźż]+(?:-[a-ząćęłńóśźż]+)*$")
TOKEN_RE = re.compile(r"[a-ząćęłńóśźż]+(?:-[a-ząćęłńóśźż]+)?")

# Keep source frequency quality high for learning cards.
STOP_WORD_FOR_PHRASES = {
    "kurwa",
    "cholera",
    "pierdol",
    "spierdalaj",
}


@dataclass(frozen=True)
class FrequencyWord:
    word: str
    rank: int
    count: int


@dataclass(frozen=True)
class PhraseCandidate:
    sentence_id: str
    text: str
    score: float
    avg_rank: float
    known_ratio: float
    token_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Polish WORD/PHRASE ETL candidates")
    parser.add_argument("--raw-dir", default="raw", help="ETL raw directory")
    parser.add_argument("--work-dir", default="work", help="ETL work directory")
    parser.add_argument("--word-count", type=int, default=3500, help="Number of WORD cards")
    parser.add_argument("--phrase-count", type=int, default=1500, help="Number of PHRASE cards")
    parser.add_argument("--deck-code", default="polish-core")
    parser.add_argument("--deck-title", default="Polish Core")
    parser.add_argument("--refresh", action="store_true", help="Re-download remote sources")
    parser.add_argument("--min-phrase-words", type=int, default=2)
    parser.add_argument("--max-phrase-words", type=int, default=6)
    return parser.parse_args()


def download_if_needed(url: str, path: Path, refresh: bool) -> None:
    if path.exists() and not refresh:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "kotland-etl/1.0 (+https://example.local)",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(request) as resp:  # noqa: S310
        data = resp.read()
    path.write_bytes(data)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


def parse_wiktionary_words(path: Path, limit: int) -> list[FrequencyWord]:
    text = path.read_text(encoding="utf-8")
    words: list[FrequencyWord] = []
    seen: set[str] = set()
    rank = 0

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("# ") or "<span lang=\"pl\">" not in line:
            continue

        match = re.search(r"\[\[[^|\]]+\|([^\]]+)\]\]\s*</span>\s*(\d+)\s*$", line)
        if not match:
            continue

        token = normalize_space(match.group(1).lower())
        if token in seen:
            continue
        if not WORD_RE.fullmatch(token):
            continue
        if token in STOP_WORD_FOR_PHRASES:
            continue

        rank += 1
        seen.add(token)
        words.append(FrequencyWord(word=token, rank=rank, count=int(match.group(2))))
        if len(words) >= limit:
            break

    if len(words) < limit:
        raise RuntimeError(f"Parsed only {len(words)} words from {path}, expected at least {limit}")
    return words


def tokenize_polish(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def is_phrase_text_ok(text: str, min_words: int, max_words: int) -> bool:
    normalized = normalize_space(text)
    if len(normalized) < 4 or len(normalized) > 80:
        return False
    if re.search(r"\d", normalized):
        return False
    if normalized.count(",") > 1 or normalized.count(";") > 0:
        return False
    if re.search(r"[<>\\[\\]{}|]", normalized):
        return False

    tokens = tokenize_polish(normalized)
    if not (min_words <= len(tokens) <= max_words):
        return False
    if len(tokens) < 2:
        return False
    if any(token in STOP_WORD_FOR_PHRASES for token in tokens):
        return False
    return True


def parse_tatoeba_phrases(
    path: Path,
    word_rank: dict[str, int],
    max_count: int,
    min_words: int,
    max_words: int,
) -> list[PhraseCandidate]:
    scored: list[tuple[PhraseCandidate, tuple[str, str]]] = []
    seen: set[str] = set()

    with bz2.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            sentence_id, lang, text = parts
            if lang != "pol":
                continue

            normalized = normalize_space(text)
            normalized_key = normalized.lower()
            if normalized_key in seen:
                continue
            if not is_phrase_text_ok(normalized, min_words, max_words):
                continue

            tokens = tokenize_polish(normalized)
            known_ranks = [word_rank[tok] for tok in tokens if tok in word_rank]
            known_ratio = (len(known_ranks) / len(tokens)) if tokens else 0.0
            if known_ratio < 0.4:
                continue

            # Lower score is better:
            # - common words (low avg rank)
            # - high known word ratio
            # - moderate phrase length (2-5 words)
            avg_rank = statistics.mean(known_ranks) if known_ranks else 999999.0
            score = (
                avg_rank
                + ((1.0 - known_ratio) * 4500.0)
                + (abs(len(tokens) - 4) * 40.0)
            )

            phrase = PhraseCandidate(
                sentence_id=sentence_id,
                text=normalized,
                score=score,
                avg_rank=avg_rank,
                known_ratio=known_ratio,
                token_count=len(tokens),
            )
            prefix = (tokens[0], tokens[1] if len(tokens) > 1 else "")
            scored.append((phrase, prefix))
            seen.add(normalized_key)

    scored.sort(key=lambda item: (item[0].score, item[0].avg_rank, -item[0].known_ratio, item[0].text))

    selected: list[PhraseCandidate] = []
    prefix_counter: dict[tuple[str, str], int] = {}
    prefix_cap = 24
    for phrase, prefix in scored:
        if len(selected) >= max_count:
            break
        if prefix_counter.get(prefix, 0) >= prefix_cap:
            continue
        prefix_counter[prefix] = prefix_counter.get(prefix, 0) + 1
        selected.append(phrase)

    return selected


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    args = parse_args()

    raw_dir = Path(args.raw_dir)
    work_dir = Path(args.work_dir)
    words_output = work_dir / "polish_core_words.jsonl"
    phrases_output = work_dir / "polish_core_phrases.jsonl"
    report_output = work_dir / "polish_core_candidates.report.json"

    wiktionary_raw = raw_dir / "frequency" / "wiktionary_polish_wordlist.raw.txt"
    tatoeba_pol_bz2 = raw_dir / "tatoeba" / "pol_sentences.tsv.bz2"

    download_if_needed(WIKTIONARY_FREQUENCY_URL, wiktionary_raw, args.refresh)
    download_if_needed(TATOEBA_POL_SENTENCES_URL, tatoeba_pol_bz2, args.refresh)

    words = parse_wiktionary_words(wiktionary_raw, args.word_count)
    word_rank = {item.word: item.rank for item in words}

    phrase_candidates = parse_tatoeba_phrases(
        path=tatoeba_pol_bz2,
        word_rank=word_rank,
        max_count=args.phrase_count,
        min_words=args.min_phrase_words,
        max_words=args.max_phrase_words,
    )

    word_rows: list[dict] = []
    for item in words:
        word_rows.append(
            {
                "deckCode": args.deck_code,
                "deckTitle": args.deck_title,
                "type": "WORD",
                "frontTextPl": item.word,
                "frontTranscript": None,
                "backRuTranslation": "",
                "backRuPronHint": None,
                "dateIso": None,
                "imagePath": None,
                "tags": ["core", "approach_a_frequency"],
                "audioVariants": [],
                "sourceAttribution": [
                    {
                        "sourceName": "wiktionary-frequency",
                        "sourceRef": f"rank={item.rank};count={item.count}",
                        "confidenceScore": 0.97,
                    }
                ],
                "metaRank": item.rank,
            }
        )

    phrase_rows: list[dict] = []
    for idx, item in enumerate(phrase_candidates, start=1):
        phrase_rows.append(
            {
                "deckCode": args.deck_code,
                "deckTitle": args.deck_title,
                "type": "PHRASE",
                "frontTextPl": item.text,
                "frontTranscript": None,
                "backRuTranslation": "",
                "backRuPronHint": None,
                "dateIso": None,
                "imagePath": None,
                "tags": ["core", "approach_b_tatoeba_phrase"],
                "audioVariants": [],
                "sourceAttribution": [
                    {
                        "sourceName": "tatoeba-polish-sentences",
                        "sourceRef": f"sentence_id={item.sentence_id};rank={idx}",
                        "confidenceScore": round(min(0.99, 0.6 + (item.known_ratio * 0.39)), 3),
                    }
                ],
                "metaPhraseScore": {
                    "score": round(item.score, 3),
                    "avgRank": round(item.avg_rank, 3),
                    "knownRatio": round(item.known_ratio, 3),
                    "tokenCount": item.token_count,
                },
            }
        )

    write_jsonl(words_output, word_rows)
    write_jsonl(phrases_output, phrase_rows)

    report = {
        "wordsWritten": len(word_rows),
        "phrasesWritten": len(phrase_rows),
        "wordOutput": str(words_output),
        "phraseOutput": str(phrases_output),
        "wiktionaryRaw": str(wiktionary_raw),
        "tatoebaRaw": str(tatoeba_pol_bz2),
        "wordCountTarget": args.word_count,
        "phraseCountTarget": args.phrase_count,
    }
    report_output.parent.mkdir(parents=True, exist_ok=True)
    report_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"build_candidates_complete words={len(word_rows)} phrases={len(phrase_rows)} "
        f"word_output={words_output} phrase_output={phrases_output} report={report_output}"
    )


if __name__ == "__main__":
    main()
