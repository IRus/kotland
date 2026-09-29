# Kotland

An Anki-style Polish learning web app for Russian-speaking users.

## Tech stack

- Java 21
- Kotlin 2.3.10
- Ktor + SQLite backend
- Preact (no build step) frontend
- Offline ETL scripts in `etl/`

## Local run

1. Export env vars:

```bash
export GOOGLE_CLIENT_ID="..."
export GOOGLE_CLIENT_SECRET="..."
export GOOGLE_REDIRECT_URI="http://localhost:8080/auth/callback"
export SESSION_SECRET="replace-with-long-random-secret"
export SESSION_SECURE="false"
export DB_PATH="$(pwd)/.data/app.sqlite"
export MEDIA_ROOT="$(pwd)/etl/out/media"
```

2. Start app:

```bash
./gradlew :app:run
```

3. Open <http://localhost:8080>

## Offline import

Run importer with JSONL manifest:

```bash
./gradlew :app:runImporter -PimportArgs="--manifest $(pwd)/etl/out/manifest/sample.jsonl --db $(pwd)/.data/app.sqlite --media-root $(pwd)/etl/out/media"
```

## Docker run

1. Copy `.env.example` to `.env` and fill values.
2. Ensure NAS mount exists at `/mnt/nas/polish`.
3. Start:

```bash
docker compose up --build -d
```

## Data pipeline

All heavy processing (collection, merge, audio generation, PDF extraction) lives in `etl/scripts` and is intentionally offline.
