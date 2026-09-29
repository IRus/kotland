#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE_DIR="${1:-$ROOT_DIR/deploy/unraid_bundle}"
TARBALL_PATH="${BUNDLE_DIR}.tar.gz"

DB_SRC="${DB_SRC:-$ROOT_DIR/.data/app.sqlite}"
MEDIA_SRC="${MEDIA_SRC:-$ROOT_DIR/etl/out/media}"
MANIFEST_SRC="${MANIFEST_SRC:-$ROOT_DIR/etl/out/manifest}"

if [[ ! -f "$DB_SRC" ]]; then
  echo "DB file not found: $DB_SRC" >&2
  exit 1
fi
if [[ ! -d "$MEDIA_SRC" ]]; then
  echo "Media directory not found: $MEDIA_SRC" >&2
  exit 1
fi
if [[ ! -d "$MANIFEST_SRC" ]]; then
  echo "Manifest directory not found: $MANIFEST_SRC" >&2
  exit 1
fi

echo "[1/6] Building app distribution (Java 21 / Kotlin 2.3.10)..."
(
  cd "$ROOT_DIR"
  ./gradlew :app:installDist --no-daemon >/tmp/kotland-installDist.log
)

APP_DIST_SRC="$ROOT_DIR/app/build/install/app"
if [[ ! -x "$APP_DIST_SRC/bin/app" ]]; then
  echo "App distribution not found after build: $APP_DIST_SRC" >&2
  exit 1
fi

echo "[2/6] Recreating bundle folder: $BUNDLE_DIR"
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR/app-dist"
mkdir -p "$BUNDLE_DIR/runtime/db"
mkdir -p "$BUNDLE_DIR/runtime/library/media"
mkdir -p "$BUNDLE_DIR/runtime/library/manifests"

echo "[3/6] Copying app runtime..."
cp -R "$APP_DIST_SRC"/. "$BUNDLE_DIR/app-dist/"

echo "[4/6] Copying data (SQLite + media + manifests)..."
cp "$DB_SRC" "$BUNDLE_DIR/runtime/db/app.sqlite"
cp -R "$MEDIA_SRC"/. "$BUNDLE_DIR/runtime/library/media/"
cp -R "$MANIFEST_SRC"/. "$BUNDLE_DIR/runtime/library/manifests/"

echo "[5/6] Writing Unraid deploy files..."
cat >"$BUNDLE_DIR/Dockerfile.appdist" <<'EOF'
FROM eclipse-temurin:21-jre
WORKDIR /opt/app
COPY app-dist/ /opt/app/
ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["/opt/app/bin/app"]
EOF

cat >"$BUNDLE_DIR/docker-compose.yml" <<'EOF'
services:
  app:
    build:
      context: .
      dockerfile: Dockerfile.appdist
    environment:
      PORT: 8080
      DB_PATH: /data/db/app.sqlite
      MEDIA_ROOT: /data/library/media
      SESSION_SECRET: ${SESSION_SECRET}
      SESSION_SECURE: ${SESSION_SECURE:-true}
      ADMIN_EMAILS: ${ADMIN_EMAILS:-}
      GOOGLE_CLIENT_ID: ${GOOGLE_CLIENT_ID}
      GOOGLE_CLIENT_SECRET: ${GOOGLE_CLIENT_SECRET}
      GOOGLE_REDIRECT_URI: ${GOOGLE_REDIRECT_URI}
    volumes:
      - ./runtime/db:/data/db
      - ./runtime/library:/data/library
    ports:
      - "8080:8080"
    restart: unless-stopped

  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel --no-autoupdate run --token ${CF_TUNNEL_TOKEN}
    depends_on:
      - app
    restart: unless-stopped
    profiles:
      - tunnel
EOF

cat >"$BUNDLE_DIR/.env.example" <<'EOF'
GOOGLE_CLIENT_ID=replace-me.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=replace-me
GOOGLE_REDIRECT_URI=https://your-hostname.example.com/callback
SESSION_SECRET=replace-with-long-random-secret
SESSION_SECURE=true
ADMIN_EMAILS=you@example.com
CF_TUNNEL_TOKEN=replace-me
EOF

cat >"$BUNDLE_DIR/README_UNRAID.md" <<'EOF'
# Unraid Deployment Bundle

## What is included

- `app-dist/` - prebuilt Kotlin/Ktor runtime.
- `runtime/db/app.sqlite` - current local SQLite database.
- `runtime/library/media/` - all media files (audio/images).
- `runtime/library/manifests/` - importable JSONL manifests.
- `docker-compose.yml` - app + optional cloudflared service.
- `.env.example` - env template (copy to `.env` and fill).

## Start on Unraid

1. Copy this whole folder to Unraid (for example into `/mnt/user/appdata/kotland`).
2. In that folder:
   - `cp .env.example .env`
   - fill Google OAuth + session + optional Cloudflare token.
3. Start app:
   - `docker compose up -d --build`
4. Optional tunnel:
   - `docker compose --profile tunnel up -d`

## Google OAuth callback

Set `GOOGLE_REDIRECT_URI` to:
- `https://your-public-hostname/callback`

## Data strategy

- Fastest: use bundled DB directly (`runtime/db/app.sqlite`).
- Re-import (optional): manifests are under `runtime/library/manifests/`.
  To run importer inside running app container:

```bash
docker compose exec app \
  java -cp "/opt/app/lib/*" org.example.etl.ImportCliKt \
  --manifest /data/library/manifests/kp_people_page3_4_photo.jsonl \
  --db /data/db/app.sqlite \
  --media-root /data/library/media
```
EOF

cat >"$BUNDLE_DIR/import_manifest.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

MANIFEST_PATH="${1:-/data/library/manifests/kp_people_page3_4_photo.jsonl}"
DB_PATH="${2:-/data/db/app.sqlite}"
MEDIA_ROOT="${3:-/data/library/media}"

exec java -cp "/opt/app/lib/*" org.example.etl.ImportCliKt \
  --manifest "$MANIFEST_PATH" \
  --db "$DB_PATH" \
  --media-root "$MEDIA_ROOT"
EOF
chmod +x "$BUNDLE_DIR/import_manifest.sh"

echo "[6/6] Creating tarball: $TARBALL_PATH"
tar -czf "$TARBALL_PATH" -C "$(dirname "$BUNDLE_DIR")" "$(basename "$BUNDLE_DIR")"

echo
echo "Bundle ready:"
echo "  Folder : $BUNDLE_DIR"
echo "  Tarball: $TARBALL_PATH"
echo
du -sh "$BUNDLE_DIR" "$TARBALL_PATH"
