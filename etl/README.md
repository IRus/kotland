# Offline ETL Workspace

This folder contains all heavy data-processing logic. The web app only reads imported data.

## Directory layout

- `raw/`: downloaded corpora and PDFs.
- `work/`: normalized intermediate files.
- `out/manifest/`: JSONL manifests ready for Kotlin importer.
- `out/media/`: generated audio and images.
- `scripts/`: ETL scripts.

## Manifest format (JSONL)

Each line represents one card and is imported idempotently by canonical key.

```json
{
  "canonicalKey": "WORD|dzień dobry|добрый день",
  "deckCode": "polish-core",
  "deckTitle": "Polish Core",
  "type": "PHRASE",
  "frontTextPl": "dzień dobry",
  "frontTranscript": "джень добры",
  "backRuTranslation": "добрый день",
  "backRuPronHint": "формально",
  "dateIso": null,
  "imagePath": null,
  "tags": ["greeting", "core"],
  "audioVariants": [
    {
      "source": "TTS_GOOGLE",
      "label": "TTS (Google)",
      "path": "audio/tts-google/dzien-dobry.mp3",
      "quality": "AUTO",
      "isPrimary": true
    }
  ],
  "sourceAttribution": [
    {
      "sourceName": "frequency-list",
      "sourceRef": "subtlex_pl",
      "confidenceScore": 0.98
    }
  ]
}
```

## Typical flow

1. Collect and normalize candidates into JSONL files under `work/`.
2. Merge and dedupe:

```bash
python3 scripts/merge_candidates.py \
  --inputs work/words.jsonl work/phrases.jsonl work/history.jsonl \
  --output work/merged.jsonl
```

3. Build final manifest:

```bash
python3 scripts/export_manifest.py \
  --input work/merged.jsonl \
  --output out/manifest/seed.jsonl
```

4. Import into SQLite:

```bash
./gradlew :app:runImporter -PimportArgs="--manifest /absolute/path/etl/out/manifest/seed.jsonl --db /data/db/app.sqlite --media-root /data/library/media"
```

## PDF one-off extractor (multi-approach)

```bash
python3 scripts/extract_history_pdf.py \
  --pdf raw/history/book.pdf \
  --output-dir out/pdf_cards \
  --media-root out/media \
  --deck-code history \
  --deck-title "History Facts" \
  --ocr-mode low
```

This command writes:

- `out/pdf_cards/<doc>/text/` with per-engine text dumps (`pypdf`, `pymupdf`, `pdfplumber`, `ocr`, merged)
- `out/pdf_cards/<doc>/cards/*.jsonl` with extracted card sets (`definitions`, `years`, `qna`, `facts`, `all`)
- `out/media/pdf/<doc>/images/` with extracted PDF images used as `imagePath` in cards
- `out/pdf_cards/<doc>/report.json` with extraction diagnostics and counts

`--ocr-mode` options:

- `off`: text-layer only
- `low`: OCR only weak pages (recommended)
- `all`: OCR all pages for maximal recall checks

If you already refined cards (for example with OpenAI) and want to inject extracted image paths without re-refining:

```bash
python3 scripts/attach_image_paths.py \
  --source out/pdf_cards/<doc>/cards/all.jsonl \
  --target out/pdf_cards/<doc>/cards/all.refined.openai.jsonl \
  --output out/pdf_cards/<doc>/cards/all.refined.openai.with-images.jsonl
```

## OpenAI quality refinement (optional, recommended)

After extraction, run OpenAI refinement to improve card quality and Russian answer clarity:

```bash
export OPENAI_API_KEY="..."
python3 scripts/refine_cards_with_openai.py \
  --input out/pdf_cards/<doc>/cards/all.jsonl \
  --output out/pdf_cards/<doc>/cards/all.refined.jsonl \
  --model gpt-5-mini
```

Optional: pass key directly with `--api-key ...`.

Then import:

```bash
./gradlew :app:runImporter -PimportArgs="--manifest /absolute/path/etl/out/pdf_cards/<doc>/cards/all.refined.jsonl --db /data/db/app.sqlite --media-root /data/library/media"
```

Install dependencies from `scripts/requirements.txt` before running.

## Polish core dataset generation (WORDS + PHRASES)

This flow builds a 5,000-card language deck with two independent approaches:

- Approach A: frequency-first words from Wiktionary/OpenSubtitles frequency list.
- Approach B: short practical phrases from Tatoeba Polish sentence corpus, ranked by common-word coverage.

```bash
# 1) Build candidates
python3 scripts/build_polish_word_phrase_candidates.py \
  --raw-dir raw \
  --work-dir work \
  --word-count 3500 \
  --phrase-count 1500

# 2) Merge and dedupe
python3 scripts/merge_candidates.py \
  --inputs work/polish_core_words.jsonl work/polish_core_phrases.jsonl \
  --output work/polish_core_merged.jsonl

# 3) Enrich with OpenAI (RU translation + transcript + pronunciation hint)
OPENAI_API_KEY=... python3 scripts/enrich_language_cards_with_openai.py \
  --input work/polish_core_merged.jsonl \
  --output work/polish_core_enriched.jsonl \
  --model gpt-5-mini \
  --batch-size 20

# 4) Generate local audio files with OpenAI TTS
OPENAI_API_KEY=... python3 scripts/generate_openai_tts_audio.py \
  --input work/polish_core_enriched.jsonl \
  --output work/polish_core_enriched.audio.jsonl \
  --media-root out/media \
  --media-subdir audio/tts-openai \
  --model gpt-4o-mini-tts \
  --voice alloy

# 5) Attach native Tatoeba audio where available
python3 scripts/ingest_tatoeba_audio.py \
  --input work/polish_core_enriched.audio.jsonl \
  --output work/polish_core_enriched.audio.tatoeba.jsonl \
  --audio-csv raw/tatoeba/sentences_with_audio.csv \
  --media-root out/media \
  --media-subdir audio/native-tatoeba

# 6) Add Azure TTS variants (all discovered Polish voices in region)
AZURE_SPEECH_KEY=... AZURE_SPEECH_REGION=westeurope python3 scripts/generate_azure_tts_audio.py \
  --input work/polish_core_enriched.audio.tatoeba.jsonl \
  --output work/polish_core_enriched.audio.tatoeba.azure.jsonl \
  --media-root out/media \
  --media-subdir audio/tts-azure \
  --workers 12

# 7) Final manifest
python3 scripts/export_manifest.py \
  --input work/polish_core_enriched.audio.tatoeba.azure.jsonl \
  --output out/manifest/polish_core_5000.jsonl
```
