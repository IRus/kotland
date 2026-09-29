#!/usr/bin/env python3
"""Multi-approach PDF to flashcard extractor.

Approaches:
1) Text layer extraction via PyPDF.
2) Text layer extraction via PyMuPDF.
3) Text layer extraction via pdfplumber.
4) Optional OCR fallback via RapidOCR for weak pages or all pages.

Outputs:
- Per-engine text dump by page.
- Merged text dump by page.
- Card JSONL files by type + merged manifest-compatible JSONL.
- Extraction diagnostics report.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import fitz
import pdfplumber
from pypdf import PdfReader

try:
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
except Exception:  # pragma: no cover - optional dependency
    RapidOCR = None
    np = None


YEAR_RE = re.compile(r"\b(1\d{3}|20\d{2})\b")
DATE_RE = re.compile(r"\b(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}|1\d{3}|20\d{2})\b")
DEFINITION_RE = re.compile(r"^([A-ZĄĆĘŁŃÓŚŹŻ][^–:-]{2,120})\s*[–-]\s*(.{8,})$")
QUESTION_RE = re.compile(r"\?\s*$")
HEADING_RE = re.compile(r"^(?:[IVXLC]+\.|\d+\.)\s+.+")


@dataclass
class ExtractedPage:
    page: int
    pypdf_text: str
    pymupdf_text: str
    pdfplumber_text: str
    ocr_text: str
    merged_text: str


def slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    cleaned = "".join(ch for ch in normalized if ch.isalnum() or ch in ("-", "_", " "))
    return re.sub(r"\s+", "-", cleaned.strip().lower()).strip("-") or "document"


def normalize_ws(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def cleanup_lines(text: str) -> list[str]:
    raw = [line.strip() for line in text.splitlines()]
    lines: list[str] = []
    for line in raw:
        if not line:
            continue
        if re.fullmatch(r"\d+", line):
            continue
        if re.fullmatch(r"[_\-]{5,}", line):
            continue
        if line.lower() in {"treść", "spis treści"}:
            continue
        lines.append(line)
    return lines


def collapse_lines_to_paragraphs(lines: Iterable[str]) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        if current:
            text = normalize_ws(" ".join(current))
            if len(text) > 5:
                paragraphs.append(text)
            current = []

    for line in lines:
        if not line:
            flush()
            continue

        bullet = line.startswith(("-", "•", "+", "→"))
        headingish = line.isupper() or HEADING_RE.match(line)
        if bullet or headingish:
            flush()
            text = normalize_ws(line)
            if len(text) > 2:
                paragraphs.append(text)
            continue

        current.append(line)
        if line.endswith((".", "?", "!", ":")):
            flush()

    flush()
    return paragraphs


def paragraph_key(text: str) -> str:
    key = unicodedata.normalize("NFKD", text)
    key = "".join(ch for ch in key if not unicodedata.combining(ch))
    key = key.lower()
    key = re.sub(r"[^a-z0-9а-яёąćęłńóśźż ]+", "", key)
    key = re.sub(r"\s+", " ", key).strip()
    return key


def text_score(text: str) -> float:
    if not text:
        return 0.0
    letters = sum(ch.isalpha() for ch in text)
    digits = sum(ch.isdigit() for ch in text)
    spaces = sum(ch.isspace() for ch in text)
    length = len(text)
    return length + letters * 0.25 - digits * 0.05 - spaces * 0.02


def best_engine_text(*variants: str) -> str:
    return max(variants, key=text_score)


def extract_with_pypdf(pdf_path: Path) -> dict[int, str]:
    reader = PdfReader(str(pdf_path))
    out: dict[int, str] = {}
    for idx, page in enumerate(reader.pages, start=1):
        out[idx] = normalize_ws(page.extract_text() or "")
    return out


def extract_with_pymupdf(pdf_path: Path) -> dict[int, str]:
    doc = fitz.open(str(pdf_path))
    out: dict[int, str] = {}
    for idx, page in enumerate(doc, start=1):
        out[idx] = normalize_ws(page.get_text("text"))
    return out


def extract_with_pdfplumber(pdf_path: Path) -> dict[int, str]:
    out: dict[int, str] = {}
    with pdfplumber.open(str(pdf_path)) as doc:
        for idx, page in enumerate(doc.pages, start=1):
            out[idx] = normalize_ws(page.extract_text() or "")
    return out


def extract_with_rapidocr(pdf_path: Path, page_numbers: set[int], dpi_scale: float = 2.2) -> dict[int, str]:
    if RapidOCR is None or np is None or not page_numbers:
        return {}

    doc = fitz.open(str(pdf_path))
    ocr = RapidOCR()
    out: dict[int, str] = {}

    for page_no in sorted(page_numbers):
        page = doc[page_no - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi_scale, dpi_scale), alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        result, _ = ocr(img)
        if not result:
            out[page_no] = ""
            continue

        lines = [entry[1] for entry in result if entry and len(entry) > 1 and entry[1].strip()]
        out[page_no] = normalize_ws("\n".join(lines))

    return out


def extract_page_images(
    pdf_path: Path,
    media_root: Path,
    doc_name: str,
    min_side: int,
    min_area: int,
) -> tuple[dict[int, list[dict]], dict[int, str], Path]:
    doc = fitz.open(str(pdf_path))
    images_dir = media_root / "pdf" / doc_name / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    page_images: dict[int, list[dict]] = {}
    primary_image_by_page: dict[int, str] = {}

    for page_no, page in enumerate(doc, start=1):
        extracted: list[dict] = []
        img_idx = 1
        for image_info in page.get_images(full=True):
            xref = image_info[0]
            try:
                pix = fitz.Pixmap(doc, xref)
            except Exception:
                continue

            try:
                if pix.n - pix.alpha > 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)

                width, height = pix.width, pix.height
                area = width * height
                if width < min_side or height < min_side or area < min_area:
                    continue

                file_name = f"page-{page_no:03d}-img-{img_idx:02d}.png"
                abs_path = images_dir / file_name
                pix.save(str(abs_path))

                rel_path = (Path("pdf") / doc_name / "images" / file_name).as_posix()
                extracted.append(
                    {
                        "path": rel_path,
                        "width": width,
                        "height": height,
                        "area": area,
                    }
                )
                img_idx += 1
            finally:
                pix = None

        extracted.sort(key=lambda item: item["area"], reverse=True)
        page_images[page_no] = extracted
        if extracted:
            primary_image_by_page[page_no] = extracted[0]["path"]

    return page_images, primary_image_by_page, images_dir


def merge_page_text(
    pypdf_text: str,
    pymupdf_text: str,
    pdfplumber_text: str,
    ocr_text: str,
    allow_ocr_unique: bool,
) -> str:
    candidates = {
        "pypdf": collapse_lines_to_paragraphs(cleanup_lines(pypdf_text)),
        "pymupdf": collapse_lines_to_paragraphs(cleanup_lines(pymupdf_text)),
        "pdfplumber": collapse_lines_to_paragraphs(cleanup_lines(pdfplumber_text)),
        "ocr": collapse_lines_to_paragraphs(cleanup_lines(ocr_text)),
    }

    merged: dict[str, dict] = {}
    for engine, paragraphs in candidates.items():
        for para in paragraphs:
            key = paragraph_key(para)
            if not key:
                continue
            item = merged.setdefault(
                key,
                {
                    "text": para,
                    "engines": set(),
                },
            )
            item["engines"].add(engine)
            if len(para) > len(item["text"]):
                item["text"] = para

    filtered = []
    for item in merged.values():
        engines = item["engines"]
        if engines == {"ocr"} and not allow_ocr_unique:
            continue
        filtered.append(item)

    ordered = sorted(filtered, key=lambda x: (-len(x["engines"]), -len(x["text"]), x["text"]))

    return "\n".join(item["text"] for item in ordered)


def sentence_split(text: str) -> list[str]:
    text = normalize_ws(text)
    if not text:
        return []
    text = re.sub(r"\s+", " ", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ])", text)
    return [p.strip() for p in parts if len(p.strip()) > 10]


def build_definition_cards(
    page: int,
    paragraphs: list[str],
    deck_code: str,
    deck_title: str,
    image_path: str | None = None,
) -> list[dict]:
    cards: list[dict] = []
    seen = set()

    for para in paragraphs:
        m = DEFINITION_RE.match(para)
        if not m:
            continue
        term = m.group(1).strip(" :")
        definition = m.group(2).strip()
        if len(term) < 3 or len(definition) < 10:
            continue

        key = f"def|{paragraph_key(term)}|{paragraph_key(definition)}"
        if key in seen:
            continue
        seen.add(key)

        tags = ["pdf", "definition", f"page-{page}"]
        if image_path:
            tags.append("has-image")

        cards.append(
            {
                "canonicalKey": key,
                "deckCode": deck_code,
                "deckTitle": deck_title,
                "type": "HISTORY_FACT",
                "frontTextPl": f"Что это: {term}?",
                "backRuTranslation": definition,
                "backRuPronHint": f"Определение из PDF, стр. {page}",
                "imagePath": image_path,
                "tags": tags,
                "audioVariants": [],
                "sourceAttribution": [
                    {
                        "sourceName": "pdf_definition",
                        "sourceRef": f"page:{page}",
                        "confidenceScore": 0.95,
                    }
                ],
            }
        )

    return cards


def build_year_cards(
    page: int,
    paragraphs: list[str],
    deck_code: str,
    deck_title: str,
    image_path: str | None = None,
) -> list[dict]:
    cards: list[dict] = []
    seen = set()

    for para in paragraphs:
        years = YEAR_RE.findall(para)
        if not years:
            continue
        if len(para) < 30 or len(para) > 260:
            continue

        years_sorted = sorted(set(years))
        question_body = YEAR_RE.sub("____", para)
        if question_body == para:
            continue

        key = f"year|{paragraph_key(question_body)}|{'-'.join(years_sorted)}"
        if key in seen:
            continue
        seen.add(key)

        tags = ["pdf", "date", f"page-{page}"]
        if image_path:
            tags.append("has-image")

        cards.append(
            {
                "canonicalKey": key,
                "deckCode": deck_code,
                "deckTitle": deck_title,
                "type": "HISTORY_FACT",
                "frontTextPl": f"В каком году: {question_body}",
                "backRuTranslation": ", ".join(years_sorted),
                "backRuPronHint": f"Год(ы) из факта, стр. {page}",
                "imagePath": image_path,
                "tags": tags,
                "audioVariants": [],
                "sourceAttribution": [
                    {
                        "sourceName": "pdf_year_fact",
                        "sourceRef": f"page:{page}",
                        "confidenceScore": 0.93,
                    }
                ],
            }
        )

    return cards


def build_qna_cards(
    page: int,
    paragraphs: list[str],
    deck_code: str,
    deck_title: str,
    image_path: str | None = None,
) -> list[dict]:
    cards: list[dict] = []
    seen = set()

    for idx, para in enumerate(paragraphs):
        if not QUESTION_RE.search(para):
            continue
        if len(para) < 8 or len(para) > 180:
            continue

        answer = None
        for next_idx in range(idx + 1, min(idx + 5, len(paragraphs))):
            candidate = paragraphs[next_idx]
            if QUESTION_RE.search(candidate):
                break
            if len(candidate) >= 8 and not candidate.startswith("_"):
                answer = candidate
                break

        if not answer:
            continue

        key = f"qna|{paragraph_key(para)}|{paragraph_key(answer)}"
        if key in seen:
            continue
        seen.add(key)

        tags = ["pdf", "qna", f"page-{page}"]
        if image_path:
            tags.append("has-image")

        cards.append(
            {
                "canonicalKey": key,
                "deckCode": deck_code,
                "deckTitle": deck_title,
                "type": "HISTORY_FACT",
                "frontTextPl": para,
                "backRuTranslation": answer,
                "backRuPronHint": f"Q/A из PDF, стр. {page}",
                "imagePath": image_path,
                "tags": tags,
                "audioVariants": [],
                "sourceAttribution": [
                    {
                        "sourceName": "pdf_qna",
                        "sourceRef": f"page:{page}",
                        "confidenceScore": 0.9,
                    }
                ],
            }
        )

    return cards


def build_fact_cards(
    page: int,
    paragraphs: list[str],
    deck_code: str,
    deck_title: str,
    image_path: str | None = None,
) -> list[dict]:
    cards: list[dict] = []
    seen = set()

    context_heading = ""
    for para in paragraphs:
        if para.isupper() or HEADING_RE.match(para):
            context_heading = para
            continue

        if len(para) < 35 or len(para) > 300:
            continue
        if para.startswith(("Treść", "P.S.")):
            continue

        sentences = sentence_split(para)
        if not sentences:
            continue

        anchor = sentences[0]
        if len(anchor) < 25:
            continue

        question = (
            f"Что нужно помнить: {context_heading}?" if context_heading else "Что нужно помнить из материала о Польше?"
        )
        answer = anchor

        key = f"fact|{paragraph_key(question)}|{paragraph_key(answer)}"
        if key in seen:
            continue
        seen.add(key)

        tags = ["pdf", "fact", f"page-{page}"]
        if image_path:
            tags.append("has-image")

        cards.append(
            {
                "canonicalKey": key,
                "deckCode": deck_code,
                "deckTitle": deck_title,
                "type": "HISTORY_FACT",
                "frontTextPl": question,
                "backRuTranslation": answer,
                "backRuPronHint": f"Факт из PDF, стр. {page}",
                "imagePath": image_path,
                "tags": tags,
                "audioVariants": [],
                "sourceAttribution": [
                    {
                        "sourceName": "pdf_fact",
                        "sourceRef": f"page:{page}",
                        "confidenceScore": 0.82,
                    }
                ],
            }
        )

    return cards


def dedupe_cards(cards: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}

    def card_fingerprint(card: dict) -> str:
        front = paragraph_key(card.get("frontTextPl", ""))
        back = paragraph_key(card.get("backRuTranslation", ""))
        return f"{front}|{back}"

    def confidence(card: dict) -> float:
        attrs = card.get("sourceAttribution") or []
        if not attrs:
            return 0.0
        return max(float(item.get("confidenceScore", 0.0)) for item in attrs)

    def polish_quality(text: str) -> int:
        return sum(text.count(ch) for ch in "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")

    def card_score(card: dict) -> float:
        return confidence(card) * 100.0 + polish_quality(card.get("backRuTranslation", "")) * 0.5 + len(
            card.get("backRuTranslation", "")
        ) * 0.01

    for card in cards:
        key = card_fingerprint(card)
        existing = merged.get(key)
        if existing is None:
            merged[key] = card
        else:
            if card_score(card) > card_score(existing):
                winner, loser = card, existing
            else:
                winner, loser = existing, card

            winner["tags"] = sorted(set(winner.get("tags", [])) | set(loser.get("tags", [])))
            winner["sourceAttribution"] = winner.get("sourceAttribution", []) + loser.get("sourceAttribution", [])
            merged[key] = winner

    return list(merged.values())


def write_text_dump(path: Path, text_by_page: dict[int, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for page in sorted(text_by_page):
            fh.write(f"===== PAGE {page} =====\n")
            fh.write(text_by_page[page])
            fh.write("\n\n")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract flashcards from PDF using multiple extraction approaches")
    parser.add_argument("--pdf", required=True, help="Input PDF path")
    parser.add_argument("--output-dir", required=True, help="Output directory")
    parser.add_argument(
        "--media-root",
        default="",
        help="Media root for extracted images (default: <output-dir-parent>/media)",
    )
    parser.add_argument("--deck-code", default="history", help="Deck code for generated cards")
    parser.add_argument("--deck-title", default="History Facts", help="Deck title for generated cards")
    parser.add_argument(
        "--ocr-mode",
        choices=["off", "low", "all"],
        default="low",
        help="OCR strategy: off, low (only weak pages), all pages",
    )
    parser.add_argument(
        "--min-text-chars-for-no-ocr",
        type=int,
        default=180,
        help="Pages below this char threshold are OCR candidates in low mode",
    )
    parser.add_argument(
        "--max-fact-cards-per-page",
        type=int,
        default=6,
        help="Limit generic fact cards per page",
    )
    parser.add_argument("--image-min-side", type=int, default=120, help="Minimum width/height for extracted images")
    parser.add_argument("--image-min-area", type=int, default=30000, help="Minimum area for extracted images")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    doc_name = slugify(pdf_path.stem)
    work_dir = output_dir / doc_name
    text_dir = work_dir / "text"
    cards_dir = work_dir / "cards"
    media_root = Path(args.media_root) if args.media_root else (output_dir.parent / "media")
    media_root.mkdir(parents=True, exist_ok=True)

    pypdf_text = extract_with_pypdf(pdf_path)
    pymupdf_text = extract_with_pymupdf(pdf_path)
    pdfplumber_text = extract_with_pdfplumber(pdf_path)
    page_images, primary_image_by_page, images_dir = extract_page_images(
        pdf_path=pdf_path,
        media_root=media_root,
        doc_name=doc_name,
        min_side=args.image_min_side,
        min_area=args.image_min_area,
    )

    page_count = max(pypdf_text.keys() | pymupdf_text.keys() | pdfplumber_text.keys())

    weak_pages = set()
    for page in range(1, page_count + 1):
        best = best_engine_text(
            pypdf_text.get(page, ""),
            pymupdf_text.get(page, ""),
            pdfplumber_text.get(page, ""),
        )
        if len(best) < args.min_text_chars_for_no_ocr:
            weak_pages.add(page)

    if args.ocr_mode == "all":
        ocr_pages = set(range(1, page_count + 1))
    elif args.ocr_mode == "low":
        ocr_pages = weak_pages
    else:
        ocr_pages = set()

    ocr_text = extract_with_rapidocr(pdf_path, ocr_pages)

    extracted_pages: list[ExtractedPage] = []
    merged_by_page: dict[int, str] = {}

    for page in range(1, page_count + 1):
        merged = merge_page_text(
            pypdf_text.get(page, ""),
            pymupdf_text.get(page, ""),
            pdfplumber_text.get(page, ""),
            ocr_text.get(page, ""),
            allow_ocr_unique=(page in weak_pages),
        )
        merged_by_page[page] = merged
        extracted_pages.append(
            ExtractedPage(
                page=page,
                pypdf_text=pypdf_text.get(page, ""),
                pymupdf_text=pymupdf_text.get(page, ""),
                pdfplumber_text=pdfplumber_text.get(page, ""),
                ocr_text=ocr_text.get(page, ""),
                merged_text=merged,
            )
        )

    write_text_dump(text_dir / "pypdf.txt", pypdf_text)
    write_text_dump(text_dir / "pymupdf.txt", pymupdf_text)
    write_text_dump(text_dir / "pdfplumber.txt", pdfplumber_text)
    write_text_dump(text_dir / "ocr.txt", {p: ocr_text.get(p, "") for p in range(1, page_count + 1)})
    write_text_dump(text_dir / "merged.txt", merged_by_page)

    cards_by_type: dict[str, list[dict]] = defaultdict(list)

    for page_item in extracted_pages:
        paragraphs = collapse_lines_to_paragraphs(cleanup_lines(page_item.merged_text))
        image_path = primary_image_by_page.get(page_item.page)

        definition_cards = build_definition_cards(
            page_item.page,
            paragraphs,
            args.deck_code,
            args.deck_title,
            image_path=image_path,
        )
        year_cards = build_year_cards(
            page_item.page,
            paragraphs,
            args.deck_code,
            args.deck_title,
            image_path=image_path,
        )
        qna_cards = build_qna_cards(
            page_item.page,
            paragraphs,
            args.deck_code,
            args.deck_title,
            image_path=image_path,
        )
        fact_cards = build_fact_cards(
            page_item.page,
            paragraphs,
            args.deck_code,
            args.deck_title,
            image_path=image_path,
        )[: args.max_fact_cards_per_page]

        cards_by_type["definitions"].extend(definition_cards)
        cards_by_type["years"].extend(year_cards)
        cards_by_type["qna"].extend(qna_cards)
        cards_by_type["facts"].extend(fact_cards)

    for key in list(cards_by_type.keys()):
        cards_by_type[key] = dedupe_cards(cards_by_type[key])

    all_cards = dedupe_cards(
        cards_by_type["definitions"]
        + cards_by_type["years"]
        + cards_by_type["qna"]
        + cards_by_type["facts"]
    )

    for kind, rows in cards_by_type.items():
        write_jsonl(cards_dir / f"{kind}.jsonl", rows)
    write_jsonl(cards_dir / "all.jsonl", all_cards)

    report = {
        "pdf": str(pdf_path),
        "pages": page_count,
        "ocr_mode": args.ocr_mode,
        "ocr_pages": sorted(ocr_pages),
        "weak_pages": sorted(weak_pages),
        "chars": {
            "pypdf": sum(len(v) for v in pypdf_text.values()),
            "pymupdf": sum(len(v) for v in pymupdf_text.values()),
            "pdfplumber": sum(len(v) for v in pdfplumber_text.values()),
            "ocr": sum(len(v) for v in ocr_text.values()),
            "merged": sum(len(v) for v in merged_by_page.values()),
        },
        "images": {
            "media_root": str(media_root),
            "images_dir": str(images_dir),
            "pages_with_images": len(primary_image_by_page),
            "pages_without_images": [p for p in range(1, page_count + 1) if p not in primary_image_by_page],
            "total_images_extracted": sum(len(items) for items in page_images.values()),
        },
        "cards": {
            "definitions": len(cards_by_type["definitions"]),
            "years": len(cards_by_type["years"]),
            "qna": len(cards_by_type["qna"]),
            "facts": len(cards_by_type["facts"]),
            "all": len(all_cards),
        },
        "outputs": {
            "text_dir": str(text_dir),
            "cards_dir": str(cards_dir),
            "all_cards": str(cards_dir / "all.jsonl"),
        },
    }

    report_path = work_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
