#!/usr/bin/env python3
"""Generate Azure TTS audio variants for cards."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AzureVoice:
    short_name: str
    locale: str
    gender: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Azure TTS audio variants")
    parser.add_argument("--input", required=True, help="Input JSONL")
    parser.add_argument("--output", required=True, help="Output JSONL")
    parser.add_argument("--media-root", required=True, help="Media root path")
    parser.add_argument("--media-subdir", default="audio/tts-azure", help="Relative output media directory")
    parser.add_argument("--api-key", default=os.getenv("AZURE_SPEECH_KEY", ""), help="Azure Speech key")
    parser.add_argument("--endpoint", default=os.getenv("AZURE_SPEECH_ENDPOINT", ""), help="Azure endpoint")
    parser.add_argument("--region", default=os.getenv("AZURE_SPEECH_REGION", ""), help="Azure region")
    parser.add_argument(
        "--locale-prefix",
        default="pl-",
        help="Use voices where Locale starts with this prefix; empty means all locales",
    )
    parser.add_argument(
        "--voices",
        default="",
        help="Comma-separated list of exact ShortName voices; empty means auto-discover",
    )
    parser.add_argument("--workers", type=int, default=8, help="Parallel synthesis workers")
    parser.add_argument("--retry", type=int, default=3, help="Retries per failed request")
    parser.add_argument("--sleep-ms", type=int, default=0, help="Sleep after successful request in worker")
    parser.add_argument("--max-cards", type=int, default=0, help="Only process first N cards")
    parser.add_argument("--resume", action="store_true", help="Resume by appending to existing output")
    return parser.parse_args()


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip()


def slugify(text: str, max_len: int = 50) -> str:
    lowered = text.lower()
    lowered = re.sub(r"[^\w\s-]", "", lowered, flags=re.UNICODE)
    lowered = re.sub(r"[\s_]+", "-", lowered).strip("-")
    return (lowered or "card")[:max_len]


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def parse_region_from_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint:
        return ""
    parsed = urllib.parse.urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
    host = parsed.netloc or parsed.path
    parts = host.split(".")
    if len(parts) >= 3 and parts[1] == "api" and parts[2] == "cognitive":
        return parts[0]
    return ""


def speech_base_url(endpoint: str, region: str) -> str:
    endpoint = endpoint.strip().rstrip("/")
    region = region.strip()
    if not region and endpoint:
        region = parse_region_from_endpoint(endpoint)
    if not region:
        raise ValueError("Azure region is required (use --region or AZURE_SPEECH_REGION)")
    return f"https://{region}.tts.speech.microsoft.com"


def fetch_voices(base_url: str, api_key: str) -> list[AzureVoice]:
    url = f"{base_url}/cognitiveservices/voices/list"
    request = urllib.request.Request(
        url,
        headers={
            "Ocp-Apim-Subscription-Key": api_key,
            "User-Agent": "kotland-etl/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310
        data = json.load(resp)

    voices: list[AzureVoice] = []
    for item in data:
        short_name = str(item.get("ShortName") or "").strip()
        locale = str(item.get("Locale") or "").strip()
        gender = str(item.get("Gender") or "").strip()
        if short_name and locale:
            voices.append(AzureVoice(short_name=short_name, locale=locale, gender=gender))
    return voices


def select_voices(all_voices: list[AzureVoice], locale_prefix: str, names: str) -> list[AzureVoice]:
    by_name = {v.short_name: v for v in all_voices}
    if names.strip():
        selected: list[AzureVoice] = []
        for name in [x.strip() for x in names.split(",") if x.strip()]:
            if name not in by_name:
                raise ValueError(f"Requested voice not found: {name}")
            selected.append(by_name[name])
        return selected

    prefix = locale_prefix.strip()
    selected = [v for v in all_voices if (not prefix or v.locale.startswith(prefix))]
    selected.sort(key=lambda v: v.short_name)
    return selected


def has_variant(row: dict, source: str, path: str) -> bool:
    for variant in row.get("audioVariants") or []:
        if variant.get("source") == source and variant.get("path") == path:
            return True
    return False


def add_variant(row: dict, voice: AzureVoice, rel_path: str) -> None:
    variants = row.get("audioVariants") or []
    if has_variant(row, "TTS_AZURE", rel_path):
        row["audioVariants"] = variants
        return
    variants.append(
        {
            "source": "TTS_AZURE",
            "label": f"TTS (Azure: {voice.short_name})",
            "path": rel_path,
            "quality": "AUTO",
            "isPrimary": False,
            "license": "Generated with Azure Speech",
        }
    )
    if variants and not any(v.get("isPrimary") for v in variants):
        variants[0]["isPrimary"] = True
    row["audioVariants"] = variants


def build_ssml(text: str, voice: AzureVoice) -> str:
    escaped_text = html.escape(text, quote=False)
    return (
        "<speak version='1.0' xml:lang='{lang}'>"
        "<voice xml:lang='{lang}' name='{voice}'>{text}</voice>"
        "</speak>"
    ).format(lang=voice.locale, voice=voice.short_name, text=escaped_text)


def synthesize_one(
    synth_url: str,
    api_key: str,
    voice: AzureVoice,
    text: str,
    dst: Path,
    retry: int,
    sleep_ms: int,
) -> tuple[bool, str]:
    ssml = build_ssml(text, voice).encode("utf-8")
    request = urllib.request.Request(
        synth_url,
        data=ssml,
        headers={
            "Ocp-Apim-Subscription-Key": api_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
            "User-Agent": "kotland-etl/1.0",
        },
        method="POST",
    )

    last_error = ""
    for attempt in range(retry + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:  # noqa: S310
                audio = resp.read()
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if "audio" not in content_type or len(audio) < 100:
                    return False, f"invalid_response content_type={content_type} bytes={len(audio)}"
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(audio)
                if sleep_ms > 0:
                    time.sleep(sleep_ms / 1000.0)
                return True, ""
        except urllib.error.HTTPError as exc:
            last_error = f"http_{exc.code}"
            if exc.code in (429, 500, 502, 503, 504) and attempt < retry:
                time.sleep(0.8 * (attempt + 1))
                continue
            break
        except urllib.error.URLError as exc:
            last_error = f"url_error:{exc.reason}"
            if attempt < retry:
                time.sleep(0.8 * (attempt + 1))
                continue
            break
        except Exception as exc:  # noqa: PERF203
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retry:
                time.sleep(0.8 * (attempt + 1))
                continue
            break

    return False, last_error or "unknown_error"


def main() -> None:
    args = parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    media_root = Path(args.media_root)
    media_subdir = args.media_subdir.strip().strip("/")

    if not input_path.exists():
        raise FileNotFoundError(f"Input JSONL not found: {input_path}")
    api_key = args.api_key.strip()
    if not api_key:
        raise ValueError("Azure Speech key is required (--api-key or AZURE_SPEECH_KEY)")

    base_url = speech_base_url(args.endpoint, args.region)
    voices_all = fetch_voices(base_url, api_key)
    voices = select_voices(voices_all, args.locale_prefix, args.voices)
    if not voices:
        raise ValueError("No voices selected")

    print(
        f"azure_voices_selected count={len(voices)} voices={[v.short_name for v in voices]} base_url={base_url}",
        flush=True,
    )

    rows = load_jsonl(input_path)
    if args.max_cards > 0:
        rows = rows[: args.max_cards]

    out_rows: list[dict] = []
    start = 0
    if args.resume and output_path.exists():
        existing = load_jsonl(output_path)
        out_rows.extend(existing)
        start = len(existing)
        if start > len(rows):
            raise ValueError(f"resume output rows={start} exceeds input rows={len(rows)}")
        print(f"resume_detected rows={start}", flush=True)

    pending: list[tuple[int, AzureVoice, str, Path, str]] = []
    success_map: dict[int, list[tuple[AzureVoice, str]]] = {}
    reused = 0
    skipped_empty = 0

    for idx in range(start, len(rows)):
        row = rows[idx]
        text = normalize_space(str(row.get("frontTextPl") or ""))
        if not text:
            skipped_empty += 1
            continue

        for voice in voices:
            h = hashlib.sha1(f"{voice.short_name}|{text}".encode("utf-8")).hexdigest()[:16]
            filename = f"{slugify(text)}-{h}.mp3"
            rel_path = f"{media_subdir}/{voice.short_name}/{filename}"
            abs_path = media_root / rel_path

            if has_variant(row, "TTS_AZURE", rel_path):
                continue

            if abs_path.exists() and abs_path.stat().st_size > 0:
                success_map.setdefault(idx, []).append((voice, rel_path))
                reused += 1
            else:
                pending.append((idx, voice, text, abs_path, rel_path))

    synth_url = f"{base_url}/cognitiveservices/v1"
    generated = 0
    failures = 0
    thread_local = threading.local()

    def worker(task: tuple[int, AzureVoice, str, Path, str]) -> tuple[int, AzureVoice, str, bool, str]:
        idx, voice, text, abs_path, rel_path = task
        _ = getattr(thread_local, "ready", None)
        ok, err = synthesize_one(
            synth_url=synth_url,
            api_key=api_key,
            voice=voice,
            text=text,
            dst=abs_path,
            retry=args.retry,
            sleep_ms=args.sleep_ms,
        )
        thread_local.ready = True
        return idx, voice, rel_path, ok, err

    if pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = [executor.submit(worker, task) for task in pending]
            done = 0
            for future in concurrent.futures.as_completed(futures):
                idx, voice, rel_path, ok, err = future.result()
                done += 1
                if ok:
                    generated += 1
                    success_map.setdefault(idx, []).append((voice, rel_path))
                else:
                    failures += 1
                    if failures <= 20:
                        print(
                            f"warn task={done}/{len(pending)} card={idx+1} voice={voice.short_name} error={err}",
                            flush=True,
                        )
                if done % 100 == 0 or done >= len(pending):
                    print(f"azure_tts_progress {done}/{len(pending)}", flush=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if start > 0 else "w"
    with output_path.open(mode, encoding="utf-8") as out:
        for idx in range(start, len(rows)):
            row = dict(rows[idx])
            for voice, rel_path in sorted(success_map.get(idx, []), key=lambda x: x[0].short_name):
                add_variant(row, voice, rel_path)
            if row.get("audioVariants") and not any(v.get("isPrimary") for v in row.get("audioVariants") or []):
                row["audioVariants"][0]["isPrimary"] = True
            out_rows.append(row)
            out.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(
        f"azure_tts_complete input={len(rows)} output={len(out_rows)} voices={len(voices)} "
        f"pending={len(pending)} generated={generated} reused={reused} failures={failures} "
        f"skipped_empty={skipped_empty} output_file={output_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
