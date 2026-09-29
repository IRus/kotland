#!/usr/bin/env python3
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def canonical_key(row: dict) -> str:
    if row.get("canonicalKey"):
        return row["canonicalKey"]
    return f"{row.get('type', 'WORD')}|{norm(row.get('frontTextPl', ''))}|{norm(row.get('backRuTranslation', ''))}"


def merge_audio(existing: list, incoming: list) -> list:
    by_key = {(a.get("source"), a.get("path")): a for a in existing}
    for audio in incoming:
        key = (audio.get("source"), audio.get("path"))
        if key in by_key:
            merged = dict(by_key[key])
            merged.update({k: v for k, v in audio.items() if v not in (None, "")})
            by_key[key] = merged
        else:
            by_key[key] = dict(audio)
    return list(by_key.values())


def merge_attr(existing: list, incoming: list) -> list:
    by_key = {(a.get("sourceName"), a.get("sourceRef")): a for a in existing}
    for item in incoming:
        key = (item.get("sourceName"), item.get("sourceRef"))
        if key in by_key:
            merged = dict(by_key[key])
            merged.update({k: v for k, v in item.items() if v not in (None, "")})
            by_key[key] = merged
        else:
            by_key[key] = dict(item)
    return list(by_key.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge candidate JSONL files into one deduped JSONL")
    parser.add_argument("--inputs", nargs="+", required=True, help="Input JSONL files")
    parser.add_argument("--output", required=True, help="Output JSONL file")
    args = parser.parse_args()

    merged = {}
    counts = defaultdict(int)

    for input_path in args.inputs:
        path = Path(input_path)
        if not path.exists():
            raise FileNotFoundError(f"Missing input file: {path}")

        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                key = canonical_key(row)
                counts["read"] += 1

                if key not in merged:
                    merged[key] = {
                        "canonicalKey": key,
                        "deckCode": row.get("deckCode", "polish-core"),
                        "deckTitle": row.get("deckTitle"),
                        "type": row.get("type", "WORD"),
                        "frontTextPl": row.get("frontTextPl", ""),
                        "frontTranscript": row.get("frontTranscript"),
                        "backRuTranslation": row.get("backRuTranslation", ""),
                        "backRuPronHint": row.get("backRuPronHint"),
                        "dateIso": row.get("dateIso"),
                        "imagePath": row.get("imagePath"),
                        "tags": sorted(set(row.get("tags") or [])),
                        "audioVariants": row.get("audioVariants") or [],
                        "sourceAttribution": row.get("sourceAttribution") or [],
                    }
                    counts["new"] += 1
                    continue

                current = merged[key]
                current["tags"] = sorted(set(current.get("tags", [])) | set(row.get("tags") or []))
                current["audioVariants"] = merge_audio(current.get("audioVariants") or [], row.get("audioVariants") or [])
                current["sourceAttribution"] = merge_attr(current.get("sourceAttribution") or [], row.get("sourceAttribution") or [])

                if not current.get("frontTranscript") and row.get("frontTranscript"):
                    current["frontTranscript"] = row["frontTranscript"]
                if not current.get("backRuPronHint") and row.get("backRuPronHint"):
                    current["backRuPronHint"] = row["backRuPronHint"]
                if not current.get("imagePath") and row.get("imagePath"):
                    current["imagePath"] = row["imagePath"]
                if not current.get("dateIso") and row.get("dateIso"):
                    current["dateIso"] = row["dateIso"]

                counts["merged"] += 1

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as out:
        for row in merged.values():
            out.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(
        f"merge_complete read={counts['read']} unique={len(merged)} merged={counts['merged']} output={output_path}"
    )


if __name__ == "__main__":
    main()
