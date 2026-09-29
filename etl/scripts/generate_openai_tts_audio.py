#!/usr/bin/env python3
"""Generate OpenAI TTS audio for card fronts and attach audioVariants rows."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import time
import threading
from pathlib import Path

from openai import OpenAI


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate OpenAI TTS audio for card JSONL")
    parser.add_argument("--input", required=True, help="Input JSONL")
    parser.add_argument("--output", required=True, help="Output JSONL")
    parser.add_argument("--media-root", required=True, help="Root dir where audio files are written")
    parser.add_argument("--media-subdir", default="audio/tts-openai", help="Relative media directory")
    parser.add_argument("--model", default="gpt-4o-mini-tts", help="OpenAI speech model")
    parser.add_argument("--voice", default="alloy", help="TTS voice")
    parser.add_argument("--speed", type=float, default=1.0, help="Speech speed")
    parser.add_argument("--api-key", default="", help="OpenAI API key (or OPENAI_API_KEY env)")
    parser.add_argument("--max-cards", type=int, default=0, help="Limit cards for test runs")
    parser.add_argument("--resume", action="store_true", help="Resume using existing output JSONL")
    parser.add_argument("--sleep-ms", type=int, default=0, help="Delay between TTS requests")
    parser.add_argument("--workers", type=int, default=4, help="Parallel TTS workers")
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip()


def slugify(text: str, max_len: int = 40) -> str:
    lowered = text.lower()
    lowered = re.sub(r"[^\w\s-]", "", lowered, flags=re.UNICODE)
    lowered = re.sub(r"[\s_]+", "-", lowered).strip("-")
    if not lowered:
        lowered = "card"
    return lowered[:max_len]


def ensure_audio_variant(row: dict, rel_path: str) -> dict:
    variants = row.get("audioVariants") or []
    key = ("TTS_OPENAI", rel_path)

    has_variant = False
    for variant in variants:
        current_key = (variant.get("source"), variant.get("path"))
        if current_key == key:
            has_variant = True
            variant.setdefault("label", "TTS (OpenAI)")
            variant.setdefault("quality", "AUTO")
            variant.setdefault("isPrimary", False)

    if not has_variant:
        variants.append(
            {
                "source": "TTS_OPENAI",
                "label": "TTS (OpenAI)",
                "path": rel_path,
                "quality": "AUTO",
                "isPrimary": False,
                "license": "Generated with OpenAI TTS",
            }
        )

    if variants and not any(v.get("isPrimary") for v in variants):
        variants[0]["isPrimary"] = True

    row["audioVariants"] = variants
    return row


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    media_root = Path(args.media_root)
    media_subdir = args.media_subdir.strip().strip("/")

    if not input_path.exists():
        raise FileNotFoundError(f"Input JSONL not found: {input_path}")

    rows = load_jsonl(input_path)
    if args.max_cards > 0:
        rows = rows[: args.max_cards]

    api_key = args.api_key.strip() or None

    out_rows: list[dict] = []
    generated = 0
    reused = 0
    failures = 0
    start = 0

    if args.resume and output_path.exists():
        existing = load_jsonl(output_path)
        out_rows.extend(existing)
        start = len(existing)
        if start > len(rows):
            raise ValueError(f"resume output rows={start} exceeds input rows={len(rows)}")
        print(f"resume_detected rows={start}", flush=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    pending: list[tuple[int, int, str, Path, str]] = []
    processed_rows: list[dict] = []

    for idx in range(start, len(rows)):
        row = dict(rows[idx])
        front = normalize_space(str(row.get("frontTextPl") or ""))
        if not front:
            failures += 1
            processed_rows.append(row)
            continue

        unique = hashlib.sha1(f"{args.model}|{args.voice}|{front}".encode("utf-8")).hexdigest()[:16]
        filename = f"{slugify(front)}-{unique}.mp3"
        rel_path = f"{media_subdir}/{filename}"
        abs_path = media_root / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        local_index = len(processed_rows)
        processed_rows.append(row)

        if abs_path.exists() and abs_path.stat().st_size > 0:
            reused += 1
            processed_rows[local_index] = ensure_audio_variant(processed_rows[local_index], rel_path)
            continue

        pending.append((idx, local_index, front, abs_path, rel_path))

    thread_local = threading.local()

    def synthesize(task: tuple[int, int, str, Path, str]) -> tuple[int, int, str, str | None]:
        global_idx, local_index, front, abs_path, rel_path = task
        try:
            client = getattr(thread_local, "client", None)
            if client is None:
                client = OpenAI(api_key=api_key)
                thread_local.client = client
            response = client.audio.speech.create(
                model=args.model,
                voice=args.voice,
                input=front,
                response_format="mp3",
                speed=args.speed,
            )
            abs_path.write_bytes(response.content)
            if args.sleep_ms > 0:
                time.sleep(args.sleep_ms / 1000.0)
            return (global_idx, local_index, rel_path, None)
        except Exception as exc:  # noqa: PERF203
            return (global_idx, local_index, rel_path, f"{type(exc).__name__}: {exc}")

    if pending:
        workers = max(1, args.workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(synthesize, task) for task in pending]
            done = 0
            for future in concurrent.futures.as_completed(futures):
                global_idx, local_index, rel_path, error = future.result()
                done += 1
                if error is None:
                    generated += 1
                    processed_rows[local_index] = ensure_audio_variant(processed_rows[local_index], rel_path)
                else:
                    failures += 1
                    tags = processed_rows[local_index].get("tags") or []
                    if "audio_missing" not in tags:
                        tags = list(tags) + ["audio_missing"]
                    processed_rows[local_index]["tags"] = sorted(set(tags))
                    print(f"warn card={global_idx+1} tts_failed={error}", flush=True)

                if done % 50 == 0 or done >= len(pending):
                    print(f"tts_progress generated_or_checked={done}/{len(pending)}", flush=True)

    mode = "a" if start > 0 else "w"
    with output_path.open(mode, encoding="utf-8") as out:
        for row in processed_rows:
            out_rows.append(row)
            out.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(
        f"tts_complete input={len(rows)} output={len(out_rows)} generated={generated} reused={reused} "
        f"failures={failures} media_root={media_root} output_file={output_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
