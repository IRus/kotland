#!/usr/bin/env python3
"""Build an image-first HISTORY_FACT deck from (KP) Kultura polska PDF pages 3-4.

The deck cards are:
- Front: person name (+ portrait image)
- Back: description text from the page table

This script uses image placements from page layout blocks (not only image xrefs),
so repeated xrefs that are drawn multiple times are still exported as distinct
row portraits.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import fitz


@dataclass(frozen=True)
class PersonRow:
    page: int
    name: str
    description_pl: str


ROWS: list[PersonRow] = [
    PersonRow(
        page=3,
        name="Tadeusz Kościuszko",
        description_pl=(
            "Amerykański i polski generał (inżynier wojskowy). "
            "Uczestnik wojny o niepodległość Stanów Zjednoczonych. "
            "Naczelnik powstania 1794 roku. "
            "Narodowy bohater Białorusi, Polski i Stanów Zjednoczonych."
        ),
    ),
    PersonRow(
        page=3,
        name="Jan Henryk Dąbrowski",
        description_pl=(
            "Polski generał. "
            "Uczestnik powstania 1794 roku. "
            "Twórca Legionów Polskich we Włoszech."
        ),
    ),
    PersonRow(
        page=3,
        name="Józef Rufin Wybicki",
        description_pl=(
            "Polski pisarz i polityk. "
            "Autor słów Mazurka Dąbrowskiego (1797)."
        ),
    ),
    PersonRow(
        page=3,
        name="Józef Piłsudski",
        description_pl=(
            "Polski wojskowy i polityk. "
            "Pierwszy Marszałek Polski, naczelnik państwa. "
            "Ogłosił niepodległość Polski 11 listopada 1918 roku."
        ),
    ),
    PersonRow(
        page=3,
        name="Jan Paweł II (Karol Józef Wojtyła)",
        description_pl=(
            "Były papież, najbardziej znany Polak na świecie. "
            "Urodził się w Wadowicach pod Krakowem. "
            "Był biskupem krakowskim. "
            "Pierwszy papież-Polak (1978-2005). "
            "Kanonizowany jako święty katolicki w 2014 roku."
        ),
    ),
    PersonRow(
        page=4,
        name="Maria Skłodowska-Curie",
        description_pl=(
            "Badaczka i naukowiec. "
            "Otrzymała dwie Nagrody Nobla w dziedzinach fizyki i chemii: "
            "w fizyce za badania promieniotwórczości (1903, wraz z Pierre Curie i Henri Becquerelem), "
            "w chemii za wydzielenie czystego radu (1911). "
            "Jedyna kobieta na świecie, która otrzymała dwie Nagrody Nobla w różnych dziedzinach."
        ),
    ),
    PersonRow(
        page=4,
        name="Lech Wałęsa",
        description_pl=(
            "Polityk. "
            "Otrzymał Pokojową Nagrodę Nobla za wspieranie wolności i demokracji (1983). "
            "Zapoczątkował Niezależny Samorządny Związek Zawodowy „Solidarność” (1980). "
            "Pierwszy niekomunistyczny prezydent Polski (1990-1995). "
            "Z zawodu elektryk."
        ),
    ),
    PersonRow(
        page=4,
        name="Henryk Sienkiewicz",
        description_pl=(
            "Pisarz. "
            "Otrzymał Nagrodę Nobla w dziedzinie literatury za całą twórczość (1905). "
            "Napisał m.in. „Quo vadis”, „Krzyżacy” oraz Trylogię "
            "(„Ogniem i mieczem”, „Potop”, „Pan Wołodyjewski”)."
        ),
    ),
    PersonRow(
        page=4,
        name="Władysław Reymont",
        description_pl=(
            "Pisarz. "
            "Otrzymał Nagrodę Nobla w dziedzinie literatury za utwór „Chłopi” (1924). "
            "Napisał utwory „Chłopi” i „Ziemia obiecana”."
        ),
    ),
    PersonRow(
        page=4,
        name="Czesław Miłosz",
        description_pl=(
            "Poeta i eseista. "
            "Otrzymał Nagrodę Nobla w dziedzinie literatury za całą twórczość (1980). "
            "Emigrował do Stanów Zjednoczonych."
        ),
    ),
    PersonRow(
        page=4,
        name="Wisława Szymborska",
        description_pl=(
            "Poetka. "
            "Otrzymała Nagrodę Nobla w dziedzinie literatury za całą twórczość (1996)."
        ),
    ),
]


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "item"


def left_column_image_blocks(page: fitz.Page) -> list[fitz.Rect]:
    blocks = page.get_text("dict").get("blocks", [])
    out: list[fitz.Rect] = []
    for block in blocks:
        if block.get("type") != 1:
            continue
        x0, y0, x1, y1 = block.get("bbox", (0, 0, 0, 0))
        if x1 > 200:
            continue
        area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
        if area < 3000:
            continue
        out.append(fitz.Rect(x0, y0, x1, y1))
    out.sort(key=lambda r: (r.y0, r.x0))
    return out


def clamp_rect(rect: fitz.Rect, page_rect: fitz.Rect) -> fitz.Rect:
    return fitz.Rect(
        max(page_rect.x0, rect.x0),
        max(page_rect.y0, rect.y0),
        min(page_rect.x1, rect.x1),
        min(page_rect.y1, rect.y1),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True, help="Path to (KP) Kultura polska.pdf")
    parser.add_argument("--media-root", required=True, help="Media root used by the app/importer")
    parser.add_argument("--output-manifest", required=True, help="Output JSONL path")
    parser.add_argument("--deck-code", default="kp-p3p4-people")
    parser.add_argument("--deck-title", default="KP: Znani Polacy (str. 3-4)")
    parser.add_argument("--zoom", type=float, default=2.6, help="Crop render zoom")
    parser.add_argument("--margin-pt", type=float, default=3.0, help="Extra crop margin in points")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pdf_path = Path(args.pdf)
    media_root = Path(args.media_root)
    output_manifest = Path(args.output_manifest)

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    media_root.mkdir(parents=True, exist_ok=True)
    output_manifest.parent.mkdir(parents=True, exist_ok=True)

    doc_name = slugify(pdf_path.stem)
    out_images_dir = media_root / "pdf" / doc_name / "images" / "p3p4-people"
    out_images_dir.mkdir(parents=True, exist_ok=True)

    rows_by_page: dict[int, list[PersonRow]] = {}
    for row in ROWS:
        rows_by_page.setdefault(row.page, []).append(row)

    manifest_rows: list[dict] = []

    with fitz.open(str(pdf_path)) as doc:
        for page_num in (3, 4):
            page_rows = rows_by_page.get(page_num, [])
            page = doc[page_num - 1]
            image_rects = left_column_image_blocks(page)
            if len(image_rects) != len(page_rows):
                raise RuntimeError(
                    f"Page {page_num}: expected {len(page_rows)} left-column images, found {len(image_rects)}. "
                    "Layout mismatch; refusing to produce incorrect mapping."
                )

            for idx, (row, img_rect) in enumerate(zip(page_rows, image_rects, strict=True), start=1):
                padded = fitz.Rect(
                    img_rect.x0 - args.margin_pt,
                    img_rect.y0 - args.margin_pt,
                    img_rect.x1 + args.margin_pt,
                    img_rect.y1 + args.margin_pt,
                )
                clip = clamp_rect(padded, page.rect)
                pix = page.get_pixmap(matrix=fitz.Matrix(args.zoom, args.zoom), clip=clip, alpha=False)

                slug = slugify(row.name)
                file_name = f"page-{page_num:03d}-person-{idx:02d}-{slug}.png"
                abs_path = out_images_dir / file_name
                rel_path = (Path("pdf") / doc_name / "images" / "p3p4-people" / file_name).as_posix()
                pix.save(str(abs_path))

                manifest_rows.append(
                    {
                        "canonicalKey": f"HISTORY_FACT|kp-p3p4|{slug}",
                        "deckCode": args.deck_code,
                        "deckTitle": args.deck_title,
                        "type": "HISTORY_FACT",
                        "frontTextPl": row.name,
                        "frontTranscript": None,
                        "backRuTranslation": row.description_pl,
                        "backRuPronHint": f"Opis z tabeli PDF, str. {page_num}",
                        "historyAnswerPl": row.description_pl,
                        "historyAnswerRu": None,
                        "dateIso": None,
                        "imagePath": rel_path,
                        "tags": ["pdf", "people", "photo-question", f"page-{page_num}"],
                        "audioVariants": [],
                        "sourceAttribution": [
                            {
                                "sourceName": "kp_pdf_table_people",
                                "sourceRef": f"page:{page_num}",
                                "confidenceScore": 1.0,
                                "license": None,
                            }
                        ],
                    }
                )

    with output_manifest.open("w", encoding="utf-8") as f:
        for row in manifest_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(
        json.dumps(
            {
                "cards": len(manifest_rows),
                "deckCode": args.deck_code,
                "manifest": str(output_manifest),
                "imagesDir": str(out_images_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
