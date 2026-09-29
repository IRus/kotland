#!/usr/bin/env python3
"""Refine extracted cards using OpenAI API for higher quality.

- Fixes OCR/noise issues.
- Normalizes Polish wording.
- Produces Russian answers suitable for flashcards.
- Keeps manifest-compatible JSONL format.

Usage:
  OPENAI_API_KEY=... python3 scripts/refine_cards_with_openai.py \
    --input out/pdf_cards/<doc>/cards/all.jsonl \
    --output out/pdf_cards/<doc>/cards/all.refined.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI


SYSTEM_PROMPT = """
You are a strict flashcard editor for Russian-speaking learners of Polish history/culture.
Return only valid JSON matching the requested schema.

Goals:
1) Keep factual meaning from source card.
2) Clean OCR and spelling artifacts.
3) Keep front question concise and natural.
4) Make answer in clear Russian (unless the card is explicitly about Polish text wording).
5) Preserve date/year facts.
6) Avoid hallucinations; if uncertain, keep original fact minimally edited.

Card style rules:
- frontTextPl: short question/prompt.
- backRuTranslation: concise factual answer in Russian.
- backRuPronHint: optional short helper in Russian.
- tags: keep existing tags; may add one of: ai_refined, ai_needs_review.
""".strip()

USER_TEMPLATE = """
Refine this card.
Return JSON object exactly with keys:
frontTextPl, backRuTranslation, backRuPronHint, tags, qualityNote

Input card JSON:
{card_json}
""".strip()

USER_TEMPLATE_BATCH = """
Refine these cards.
Return JSON object exactly with key `items`, where `items` is an array with the same length and order as input cards.
Each item must be an object with keys:
frontTextPl, backRuTranslation, backRuPronHint, tags, qualityNote

Input cards JSON array:
{cards_json}
""".strip()


@dataclass
class RefineResult:
    updated: dict[str, Any]
    retries: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refine card JSONL with OpenAI")
    parser.add_argument("--input", required=True, help="Input JSONL")
    parser.add_argument("--output", required=True, help="Output JSONL")
    parser.add_argument("--model", default="gpt-5-mini", help="OpenAI model")
    parser.add_argument("--api-key", default="", help="OpenAI API key (or use OPENAI_API_KEY env)")
    parser.add_argument("--batch-size", type=int, default=1, help="Cards per API call (kept 1 for stricter validation)")
    parser.add_argument("--max-cards", type=int, default=0, help="Process only first N cards, 0 = all")
    parser.add_argument("--retry", type=int, default=3, help="Retries per card")
    parser.add_argument("--sleep-ms", type=int, default=150, help="Delay between requests")
    parser.add_argument("--keep-polish-answer", action="store_true", help="Do not force Russian answer text")
    parser.add_argument("--resume", action="store_true", help="Resume from existing output JSONL if present")
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def ensure_valid_output(raw: dict[str, Any], original: dict[str, Any]) -> dict[str, Any]:
    front = str(raw.get("frontTextPl", "")).strip()
    back = str(raw.get("backRuTranslation", "")).strip()
    pron = str(raw.get("backRuPronHint", "")).strip()
    tags = raw.get("tags")

    if not front:
        front = original.get("frontTextPl", "")
    if not back:
        back = original.get("backRuTranslation", "")

    if not isinstance(tags, list):
        tags = list(original.get("tags", []))

    merged_tags = sorted(set(str(t) for t in (list(original.get("tags", [])) + tags) if str(t).strip()))
    if "ai_refined" not in merged_tags:
        merged_tags.append("ai_refined")

    out = dict(original)
    out["frontTextPl"] = normalize_text(front)
    out["backRuTranslation"] = normalize_text(back)
    out["backRuPronHint"] = normalize_text(pron) if pron else original.get("backRuPronHint")
    out["tags"] = merged_tags

    quality_note = str(raw.get("qualityNote", "")).strip().lower()
    if "uncertain" in quality_note or "review" in quality_note:
        if "ai_needs_review" not in out["tags"]:
            out["tags"].append("ai_needs_review")

    return out


def normalize_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_text_response(resp: Any) -> str:
    # Compatible extractor for modern OpenAI response objects.
    text = getattr(resp, "output_text", None)
    if text:
        return text

    # Fallback: inspect dict-like payload
    data = resp.model_dump() if hasattr(resp, "model_dump") else resp
    if isinstance(data, dict):
        output = data.get("output", [])
        for item in output:
            content = item.get("content", [])
            for c in content:
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    return c["text"]

    raise ValueError("No text content found in OpenAI response")


def refine_one(client: OpenAI, model: str, card: dict, retry: int) -> RefineResult:
    payload = USER_TEMPLATE.format(card_json=json.dumps(card, ensure_ascii=False))

    last_error: Exception | None = None
    for attempt in range(retry + 1):
        try:
            resp = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": payload},
                ],
                text={"format": {"type": "json_object"}},
            )
            body = extract_text_response(resp)
            parsed = json.loads(body)
            return RefineResult(updated=ensure_valid_output(parsed, card), retries=attempt)
        except Exception as exc:  # noqa: PERF203
            last_error = exc
            if attempt < retry:
                time.sleep(0.8 * (attempt + 1))
                continue
            break

    assert last_error is not None
    raise last_error


def refine_batch(client: OpenAI, model: str, cards: list[dict], retry: int) -> list[RefineResult]:
    payload = USER_TEMPLATE_BATCH.format(cards_json=json.dumps(cards, ensure_ascii=False))
    last_error: Exception | None = None

    for attempt in range(retry + 1):
        try:
            resp = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": payload},
                ],
                text={"format": {"type": "json_object"}},
            )
            body = extract_text_response(resp)
            parsed = json.loads(body)
            items = parsed.get("items")
            if not isinstance(items, list):
                raise ValueError("Batch response has no `items` list")
            if len(items) != len(cards):
                raise ValueError(f"Batch response length mismatch: expected {len(cards)}, got {len(items)}")

            out: list[RefineResult] = []
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValueError(f"Batch item #{idx} is not object")
                out.append(RefineResult(updated=ensure_valid_output(item, cards[idx]), retries=attempt))
            return out
        except Exception as exc:  # noqa: PERF203
            last_error = exc
            if attempt < retry:
                time.sleep(0.8 * (attempt + 1))
                continue
            break

    assert last_error is not None
    raise last_error


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    cards = load_jsonl(input_path)
    if args.max_cards > 0:
        cards = cards[: args.max_cards]

    api_key = args.api_key.strip() or None
    client = None if args.keep_polish_answer else OpenAI(api_key=api_key)

    out_rows = []
    failures = 0
    total_retries = 0
    start_index = 0

    if args.resume and output_path.exists():
        existing = load_jsonl(output_path)
        out_rows.extend(existing)
        start_index = len(existing)
        if start_index > len(cards):
            raise ValueError(f"Resume output has {start_index} rows but input has only {len(cards)} rows")
        print(f"resume_detected existing_rows={start_index}", flush=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    file_mode = "a" if start_index > 0 else "w"
    out_file = output_path.open(file_mode, encoding="utf-8")

    try:
        processed = start_index
        i = start_index
        while i < len(cards):
            batch_cards = [dict(c) for c in cards[i : i + max(1, args.batch_size)]]
            if not batch_cards:
                break

            if args.keep_polish_answer:
                for working in batch_cards:
                    if "backRuTranslation" in working:
                        working["tags"] = sorted(set((working.get("tags") or []) + ["ai_refined", "keep_polish_answer"]))
                    out_rows.append(working)
                    out_file.write(json.dumps(working, ensure_ascii=False) + "\n")
                    out_file.flush()
                    processed += 1
                    if processed % 10 == 0:
                        print(f"progress {processed}/{len(cards)}", flush=True)
                i += len(batch_cards)
                continue

            try:
                if len(batch_cards) == 1:
                    single = refine_one(client, args.model, batch_cards[0], args.retry)
                    batch_results = [single]
                else:
                    batch_results = refine_batch(client, args.model, batch_cards, args.retry)

                for result in batch_results:
                    out_rows.append(result.updated)
                    total_retries += result.retries
                    out_file.write(json.dumps(result.updated, ensure_ascii=False) + "\n")
                    out_file.flush()
                    processed += 1
                    if processed % 10 == 0:
                        print(f"progress {processed}/{len(cards)}", flush=True)
            except Exception as exc:
                # Fallback to per-card refinement for this batch.
                print(
                    f"warn batch_start={i+1} batch_size={len(batch_cards)} fallback_to_single_due_to={type(exc).__name__}: {exc}",
                    flush=True,
                )
                for offset, working in enumerate(batch_cards):
                    idx = i + offset + 1
                    try:
                        result = refine_one(client, args.model, working, args.retry)
                        out_rows.append(result.updated)
                        total_retries += result.retries
                        out_file.write(json.dumps(result.updated, ensure_ascii=False) + "\n")
                        out_file.flush()
                    except Exception as inner:
                        failures += 1
                        fallback = dict(working)
                        fallback["tags"] = sorted(set((fallback.get("tags") or []) + ["ai_refined", "ai_needs_review"]))
                        out_rows.append(fallback)
                        out_file.write(json.dumps(fallback, ensure_ascii=False) + "\n")
                        out_file.flush()
                        print(f"warn card={idx} fallback_due_to={type(inner).__name__}: {inner}", flush=True)

                    processed += 1
                    if processed % 10 == 0:
                        print(f"progress {processed}/{len(cards)}", flush=True)

            if args.sleep_ms > 0:
                time.sleep(args.sleep_ms / 1000.0)
            i += len(batch_cards)
    finally:
        out_file.close()

    print(
        f"refine_complete input={len(cards)} output={len(out_rows)} failures={failures} total_retries={total_retries} file={output_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
