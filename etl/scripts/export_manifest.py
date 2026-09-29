#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

AUDIO_LABELS = {
    "TTS_GOOGLE": "TTS (Google)",
    "TTS_AZURE": "TTS (Azure)",
    "TTS_OPENAI": "TTS (OpenAI)",
    "NATIVE_TATOEBA": "Native (Tatoeba)",
    "NATIVE_OPUS": "Native (OPUS)",
    "USER_RECORDING": "User Recording",
}


def ensure_audio_defaults(row: dict) -> None:
    variants = row.get("audioVariants") or []
    for variant in variants:
        src = variant.get("source", "TTS_GOOGLE")
        variant.setdefault("source", src)
        variant.setdefault("label", AUDIO_LABELS.get(src, src))
        variant.setdefault("quality", "AUTO")
        variant.setdefault("isPrimary", False)

    if variants and not any(v.get("isPrimary") for v in variants):
        variants[0]["isPrimary"] = True

    row["audioVariants"] = variants


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare final importer manifest JSONL")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows = 0
    with input_path.open("r", encoding="utf-8") as src, output_path.open("w", encoding="utf-8") as dst:
        for line in src:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            ensure_audio_defaults(row)
            dst.write(json.dumps(row, ensure_ascii=False) + "\n")
            rows += 1

    print(f"manifest_export_complete rows={rows} output={output_path}")


if __name__ == "__main__":
    main()
