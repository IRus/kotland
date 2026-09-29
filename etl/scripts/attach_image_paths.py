#!/usr/bin/env python3
"""Attach imagePath values from a source manifest to a target manifest.

Priority:
1) Match by canonicalKey.
2) Fallback by page tag (page-NNN).
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


PAGE_TAG_RE = re.compile(r"^page-(\d+)$")


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def page_tag(row: dict) -> str | None:
    for tag in row.get("tags", []):
        if PAGE_TAG_RE.match(str(tag)):
            return str(tag)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Attach imagePath values from source cards to target cards")
    parser.add_argument("--source", required=True, help="JSONL with imagePath fields")
    parser.add_argument("--target", required=True, help="JSONL to enrich")
    parser.add_argument("--output", required=True, help="Output JSONL")
    parser.add_argument("--prefer-existing", action="store_true", help="Keep target imagePath if already set")
    args = parser.parse_args()

    source = read_jsonl(Path(args.source))
    target = read_jsonl(Path(args.target))

    by_key: dict[str, str] = {}
    by_page: dict[str, str] = {}

    for row in source:
        image = (row.get("imagePath") or "").strip()
        if not image:
            continue

        key = str(row.get("canonicalKey") or "").strip()
        if key and key not in by_key:
            by_key[key] = image

        ptag = page_tag(row)
        if ptag and ptag not in by_page:
            by_page[ptag] = image

    out_rows: list[dict] = []
    attached = 0
    kept_existing = 0
    missing = 0

    for row in target:
        out = dict(row)
        current = (out.get("imagePath") or "").strip()

        if current and args.prefer_existing:
            kept_existing += 1
            out_rows.append(out)
            continue

        key = str(out.get("canonicalKey") or "").strip()
        image = by_key.get(key)
        if not image:
            ptag = page_tag(out)
            if ptag:
                image = by_page.get(ptag)

        if image:
            if not current or current != image:
                attached += 1
            out["imagePath"] = image
            tags = list(out.get("tags", []))
            if "has-image" not in tags:
                tags.append("has-image")
            out["tags"] = tags
        else:
            missing += 1

        out_rows.append(out)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as fh:
        for row in out_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(
        f"attach_images_complete source={len(source)} target={len(target)} attached={attached} "
        f"kept_existing={kept_existing} missing={missing} output={output}"
    )


if __name__ == "__main__":
    main()
