#!/usr/bin/env python3
"""Attach native Tatoeba audio variants to existing card manifest JSONL."""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


SENTENCE_ID_RE = re.compile(r"sentence_id=(\d+)")


@dataclass(frozen=True)
class TatoebaAudio:
    audio_id: int
    sentence_id: int
    username: str
    license: str
    attribution_url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Attach Tatoeba native audio to card JSONL")
    parser.add_argument("--input", required=True, help="Input JSONL")
    parser.add_argument("--output", required=True, help="Output JSONL")
    parser.add_argument(
        "--audio-csv",
        default="raw/tatoeba/sentences_with_audio.csv",
        help="Path to Tatoeba sentences_with_audio.csv",
    )
    parser.add_argument("--media-root", required=True, help="Root media directory")
    parser.add_argument("--media-subdir", default="audio/native-tatoeba", help="Relative media subdirectory")
    parser.add_argument("--max-cards", type=int, default=0, help="Process only first N rows, 0 = all")
    parser.add_argument("--timeout-sec", type=int, default=30, help="HTTP timeout in seconds")
    parser.add_argument("--resume", action="store_true", help="Reuse existing output rows if present")
    parser.add_argument("--no-download", action="store_true", help="Attach only if target files already exist")
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def parse_sentence_id(row: dict) -> int | None:
    for item in row.get("sourceAttribution") or []:
        source_name = str(item.get("sourceName") or "")
        if not source_name.startswith("tatoeba"):
            continue
        source_ref = str(item.get("sourceRef") or "")
        match = SENTENCE_ID_RE.search(source_ref)
        if match:
            return int(match.group(1))
    return None


def load_tatoeba_audio_index(path: Path) -> dict[int, list[TatoebaAudio]]:
    if not path.exists():
        raise FileNotFoundError(f"Tatoeba audio CSV not found: {path}")

    by_sentence: dict[int, list[TatoebaAudio]] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            try:
                sentence_id = int(parts[0])
                audio_id = int(parts[1])
            except ValueError:
                continue
            username = parts[2].strip() if len(parts) > 2 else ""
            license_text = parts[3].strip() if len(parts) > 3 else ""
            attribution_url = parts[4].strip() if len(parts) > 4 else ""
            row = TatoebaAudio(
                audio_id=audio_id,
                sentence_id=sentence_id,
                username=username,
                license=license_text,
                attribution_url=attribution_url,
            )
            by_sentence.setdefault(sentence_id, []).append(row)

    for sentence_id, items in by_sentence.items():
        # Prefer more permissive license text and newer audio id.
        def score(item: TatoebaAudio) -> tuple[int, int]:
            lic = item.license.upper()
            permissive = 0
            if "CC BY" in lic and "NC" not in lic and "ND" not in lic:
                permissive = 3
            elif "CC BY-SA" in lic:
                permissive = 2
            elif lic:
                permissive = 1
            return (permissive, item.audio_id)

        by_sentence[sentence_id] = sorted(items, key=score, reverse=True)

    return by_sentence


def has_tatoeba_variant(row: dict) -> bool:
    for variant in row.get("audioVariants") or []:
        if variant.get("source") == "NATIVE_TATOEBA":
            return True
    return False


def ensure_primary_variant(row: dict) -> None:
    variants = row.get("audioVariants") or []
    if variants and not any(v.get("isPrimary") for v in variants):
        variants[0]["isPrimary"] = True
    row["audioVariants"] = variants


def add_source_attribution(row: dict, entry: TatoebaAudio) -> None:
    ref = f"sentence_id={entry.sentence_id};audio_id={entry.audio_id};author={entry.username or 'unknown'}"
    attrs = row.get("sourceAttribution") or []
    for item in attrs:
        if item.get("sourceName") == "tatoeba-audio" and item.get("sourceRef") == ref:
            return

    attrs.append(
        {
            "sourceName": "tatoeba-audio",
            "sourceRef": ref,
            "confidenceScore": 0.99,
            "license": entry.license or None,
        }
    )
    row["sourceAttribution"] = attrs


def attach_variant(row: dict, rel_path: str, entry: TatoebaAudio) -> None:
    variants = row.get("audioVariants") or []
    key = ("NATIVE_TATOEBA", rel_path)
    for variant in variants:
        if (variant.get("source"), variant.get("path")) == key:
            variant.setdefault("label", "Native (Tatoeba)")
            variant.setdefault("quality", "AUTO")
            variant.setdefault("isPrimary", False)
            if entry.license and not variant.get("license"):
                variant["license"] = entry.license
            row["audioVariants"] = variants
            ensure_primary_variant(row)
            add_source_attribution(row, entry)
            return

    variants.append(
        {
            "source": "NATIVE_TATOEBA",
            "label": "Native (Tatoeba)",
            "path": rel_path,
            "quality": "AUTO",
            "isPrimary": False,
            "license": entry.license or None,
        }
    )
    row["audioVariants"] = variants
    ensure_primary_variant(row)
    add_source_attribution(row, entry)


def download_audio(entry: TatoebaAudio, dst: Path, timeout_sec: int) -> bool:
    if dst.exists() and dst.stat().st_size > 0:
        return True

    url = f"https://tatoeba.org/en/audio/download/{entry.audio_id}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "kotland-etl/1.0 (+https://example.local)",
            "Accept": "audio/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as resp:  # noqa: S310
            data = resp.read()
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if not data or len(data) < 100:
                return False
            if "audio" not in content_type:
                return False
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            return True
    except urllib.error.HTTPError:
        return False
    except urllib.error.URLError:
        return False


def main() -> None:
    args = parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    audio_csv_path = Path(args.audio_csv)
    media_root = Path(args.media_root)
    media_subdir = args.media_subdir.strip().strip("/")

    if not input_path.exists():
        raise FileNotFoundError(f"Input JSONL not found: {input_path}")

    rows = load_jsonl(input_path)
    if args.max_cards > 0:
        rows = rows[: args.max_cards]

    audio_index = load_tatoeba_audio_index(audio_csv_path)

    out_rows: list[dict] = []
    start = 0

    stats = {
        "rows": len(rows),
        "cards_with_sentence_id": 0,
        "cards_with_audio_candidates": 0,
        "cards_attached": 0,
        "cards_skipped_existing": 0,
        "download_success": 0,
        "download_failed": 0,
    }

    if args.resume and output_path.exists():
        existing = load_jsonl(output_path)
        out_rows.extend(existing)
        start = len(existing)
        if start > len(rows):
            raise ValueError(f"resume rows={start} exceeds input rows={len(rows)}")
        print(f"resume_detected rows={start}", flush=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if start > 0 else "w"

    with output_path.open(mode, encoding="utf-8") as out:
        for idx in range(start, len(rows)):
            row = dict(rows[idx])

            sentence_id = parse_sentence_id(row)
            if sentence_id is not None:
                stats["cards_with_sentence_id"] += 1

            if has_tatoeba_variant(row):
                stats["cards_skipped_existing"] += 1
                out_rows.append(row)
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                continue

            attached = False
            if sentence_id is not None:
                candidates = audio_index.get(sentence_id) or []
                if candidates:
                    stats["cards_with_audio_candidates"] += 1
                for entry in candidates:
                    rel_path = f"{media_subdir}/{entry.sentence_id}-{entry.audio_id}.mp3"
                    abs_path = media_root / rel_path
                    ok = abs_path.exists() and abs_path.stat().st_size > 0
                    if not ok and not args.no_download:
                        ok = download_audio(entry, abs_path, args.timeout_sec)
                        if ok:
                            stats["download_success"] += 1
                        else:
                            stats["download_failed"] += 1

                    if ok:
                        attach_variant(row, rel_path, entry)
                        stats["cards_attached"] += 1
                        attached = True
                        break

            if not attached:
                ensure_primary_variant(row)

            out_rows.append(row)
            out.write(json.dumps(row, ensure_ascii=False) + "\n")

            if (idx + 1) % 200 == 0 or (idx + 1) >= len(rows):
                print(f"progress {idx+1}/{len(rows)}", flush=True)

    print(
        "tatoeba_ingest_complete "
        f"rows={stats['rows']} "
        f"cards_with_sentence_id={stats['cards_with_sentence_id']} "
        f"cards_with_audio_candidates={stats['cards_with_audio_candidates']} "
        f"cards_attached={stats['cards_attached']} "
        f"cards_skipped_existing={stats['cards_skipped_existing']} "
        f"download_success={stats['download_success']} "
        f"download_failed={stats['download_failed']} "
        f"output={output_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
