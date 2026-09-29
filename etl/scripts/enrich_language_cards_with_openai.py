#!/usr/bin/env python3
"""Enrich WORD/PHRASE card rows with Russian translation and transcript.

Input: manifest-like JSONL with Polish-side text and empty/rough RU fields.
Output: JSONL ready for audio generation and import.
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
You edit flashcards for Russian-speaking learners of Polish.
Return strict JSON only.

Rules:
1) Keep Polish front text natural and concise.
2) Translate to natural Russian suitable for memorization.
3) Provide frontTranscript in Cyrillic (Russian-oriented pronunciation helper).
4) backRuPronHint should be short optional context/style note in Russian.
5) Avoid long explanations, uncertain facts, or invented meanings.
6) Preserve card type intent: WORD => dictionary-style translation, PHRASE => phrase-level translation.
7) If text is offensive slang, keep translation but add tagsAdd ["vulgar"].
""".strip()


USER_TEMPLATE_BATCH = """
Refine these cards.
Return JSON object with key "items".
"items" must be an array of same length and same order as input.

For each item return object keys:
- frontTextPl
- frontTranscript
- backRuTranslation
- backRuPronHint
- tagsAdd
- qualityNote

Input cards JSON:
{cards_json}
""".strip()


@dataclass
class RefinedItem:
    row: dict[str, Any]
    retries: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich language cards with OpenAI")
    parser.add_argument("--input", required=True, help="Input JSONL")
    parser.add_argument("--output", required=True, help="Output JSONL")
    parser.add_argument("--model", default="gpt-5-mini", help="OpenAI model")
    parser.add_argument("--api-key", default="", help="OpenAI API key (or OPENAI_API_KEY env)")
    parser.add_argument("--batch-size", type=int, default=20, help="Cards per request")
    parser.add_argument("--retry", type=int, default=3, help="Retries per request")
    parser.add_argument("--sleep-ms", type=int, default=80, help="Delay between requests")
    parser.add_argument("--max-cards", type=int, default=0, help="Limit records for test runs")
    parser.add_argument("--resume", action="store_true", help="Resume using existing output")
    return parser.parse_args()


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip()


def norm_for_key(text: str) -> str:
    return normalize_space(text).lower()


def build_canonical_key(row: dict[str, Any]) -> str:
    card_type = str(row.get("type", "WORD")).strip().upper() or "WORD"
    front = norm_for_key(str(row.get("frontTextPl", "")))
    back = norm_for_key(str(row.get("backRuTranslation", "")))
    return f"{card_type}|{front}|{back}"


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def extract_text_response(resp: Any) -> str:
    output_text = getattr(resp, "output_text", None)
    if output_text:
        return output_text

    data = resp.model_dump() if hasattr(resp, "model_dump") else resp
    if isinstance(data, dict):
        for output in data.get("output", []):
            for content in output.get("content", []):
                if content.get("type") in ("output_text", "text") and content.get("text"):
                    return content["text"]
    raise ValueError("No text content found in OpenAI response")


def ensure_item(raw: dict[str, Any], original: dict[str, Any]) -> dict[str, Any]:
    front = normalize_space(str(raw.get("frontTextPl") or original.get("frontTextPl") or ""))
    transcript = normalize_space(str(raw.get("frontTranscript") or original.get("frontTranscript") or ""))
    translation = normalize_space(str(raw.get("backRuTranslation") or original.get("backRuTranslation") or ""))
    pron_hint = normalize_space(str(raw.get("backRuPronHint") or original.get("backRuPronHint") or ""))

    if not front:
        front = normalize_space(str(original.get("frontTextPl", "")))
    if not translation:
        translation = front

    tags = [str(tag).strip() for tag in (original.get("tags") or []) if str(tag).strip()]
    add_tags = raw.get("tagsAdd") or []
    if isinstance(add_tags, list):
        tags.extend(str(tag).strip() for tag in add_tags if str(tag).strip())
    tags.append("ai_refined")
    tags = sorted(set(tags))

    out = dict(original)
    out["frontTextPl"] = front
    out["frontTranscript"] = transcript or None
    out["backRuTranslation"] = translation
    out["backRuPronHint"] = pron_hint or None
    out["tags"] = tags
    out["canonicalKey"] = build_canonical_key(out)
    return out


def refine_batch(
    client: OpenAI,
    model: str,
    batch: list[dict[str, Any]],
    retry: int,
) -> list[RefinedItem]:
    compact = [
        {
            "type": row.get("type"),
            "frontTextPl": row.get("frontTextPl"),
            "frontTranscript": row.get("frontTranscript"),
            "backRuTranslation": row.get("backRuTranslation"),
            "backRuPronHint": row.get("backRuPronHint"),
            "tags": row.get("tags", []),
        }
        for row in batch
    ]
    payload = USER_TEMPLATE_BATCH.format(cards_json=json.dumps(compact, ensure_ascii=False))

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
                timeout=120,
            )
            body = extract_text_response(resp)
            parsed = json.loads(body)
            items = parsed.get("items")
            if not isinstance(items, list):
                raise ValueError("Response has no items[]")
            if len(items) != len(batch):
                raise ValueError(f"items length mismatch expected={len(batch)} got={len(items)}")

            results: list[RefinedItem] = []
            for idx, raw_item in enumerate(items):
                if not isinstance(raw_item, dict):
                    raise ValueError(f"Item #{idx} is not object")
                results.append(RefinedItem(row=ensure_item(raw_item, batch[idx]), retries=attempt))
            return results
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
        raise FileNotFoundError(f"Input JSONL not found: {input_path}")

    rows = load_jsonl(input_path)
    if args.max_cards > 0:
        rows = rows[: args.max_cards]

    api_key = args.api_key.strip() or None
    client = OpenAI(api_key=api_key)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    out_rows: list[dict] = []
    total_retries = 0
    failures = 0
    start = 0

    if args.resume and output_path.exists():
        existing = load_jsonl(output_path)
        out_rows.extend(existing)
        start = len(existing)
        if start > len(rows):
            raise ValueError(f"resume output rows={start} exceeds input rows={len(rows)}")
        print(f"resume_detected rows={start}", flush=True)

    mode = "a" if start > 0 else "w"
    with output_path.open(mode, encoding="utf-8") as out:
        i = start
        while i < len(rows):
            batch = [dict(row) for row in rows[i : i + max(1, args.batch_size)]]
            if not batch:
                break

            try:
                refined = refine_batch(client, args.model, batch, args.retry)
                for item in refined:
                    out_rows.append(item.row)
                    total_retries += item.retries
                    out.write(json.dumps(item.row, ensure_ascii=False) + "\n")
            except Exception as exc:
                failures += len(batch)
                print(
                    f"warn batch_start={i+1} batch_size={len(batch)} fallback_due_to={type(exc).__name__}: {exc}",
                    flush=True,
                )
                for row in batch:
                    fallback = dict(row)
                    fallback["tags"] = sorted(set((fallback.get("tags") or []) + ["ai_refined", "ai_needs_review"]))
                    if not normalize_space(str(fallback.get("backRuTranslation") or "")):
                        fallback["backRuTranslation"] = normalize_space(str(fallback.get("frontTextPl") or ""))
                    fallback["canonicalKey"] = build_canonical_key(fallback)
                    out_rows.append(fallback)
                    out.write(json.dumps(fallback, ensure_ascii=False) + "\n")

            out.flush()
            i += len(batch)
            if i % 50 == 0 or i >= len(rows):
                print(f"progress {i}/{len(rows)}", flush=True)
            if args.sleep_ms > 0:
                time.sleep(args.sleep_ms / 1000.0)

    print(
        f"enrich_complete input={len(rows)} output={len(out_rows)} failures={failures} "
        f"total_retries={total_retries} file={output_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
