# YSL Photobooth — Supabase Online Guest Gallery + Local Kiosk

## Architecture

This project uses two Flask processes:

1. `app.py` runs on the photobooth computer and keeps the camera, capture, image processing, GIF generation, printing, and offline workflow local.
2. `cloud_service.py` runs on Render and serves private online guest galleries and the sync API.
3. Supabase PostgreSQL stores session metadata and SHA-256 guest-token hashes. Supabase Storage stores private photos, strips, and GIFs.
4. GitHub stores source code. GitHub Pages cannot run Flask routes or Jinja templates; do not use its URL as the kiosk/backend origin.

The guest QR points to the Render cloud service, so guests can access their own gallery using mobile data or a different Wi-Fi network.

## Supabase setup

1. Create a Supabase project.
2. Create a Storage bucket named `ysl-photobooth` and keep **Public bucket OFF**.
3. In SQL Editor, run `supabase_schema.sql` (or use the table already created with the same columns).
4. In Project Settings → API Keys, create/copy a server-side **Secret key**. Keep it private. Never put it in frontend JavaScript, HTML, or GitHub.
5. Copy the Project URL from the Connect panel or API settings.

The Render backend uses the secret key server-side to access the table and private bucket. No browser-facing Supabase credentials or public bucket URLs are used. Row Level Security is enabled and no public policies are created.

## Render deployment

1. Push this updated project to GitHub. Do not commit `.env`, service credentials, `.venv`, or local `storage/` session photos.
2. In Render, create a Python web service from the repository (or use the included `render.yaml`).
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn cloud_service:app --workers 1 --threads 8 --timeout 180`
5. Set these Render environment variables:
   - `CLOUD_SYNC_SECRET`: generate a long random secret.
   - `SUPABASE_URL`: your Supabase Project URL.
   - `SUPABASE_SECRET_KEY`: your Supabase Secret API key (`sb_secret_...`).
   - `SUPABASE_BUCKET`: `ysl-photobooth`.
   - `GUEST_RATE_LIMIT_PER_MINUTE`: optional, defaults to 90.
6. Deploy. Visit `https://YOUR-RENDER-SERVICE.onrender.com/health`; it should return `ok: true` and `supabase: true`.

## Photobooth computer configuration

Copy `.env.example` to `.env` and set:

```env
CLOUD_API_URL=https://YOUR-RENDER-SERVICE.onrender.com
CLOUD_PUBLIC_URL=https://YOUR-RENDER-SERVICE.onrender.com
CLOUD_SYNC_SECRET=THE_EXACT_SAME_SECRET_AS_RENDER
```

Install updated requirements in the kiosk's Python environment and start the existing local app using `start.bat` or `python app.py`. The kiosk keeps using its local Flask URL for camera and printer access.

## Security and session behavior

- QR links contain a cryptographically random per-session token.
- Supabase stores only the SHA-256 token hash, never the raw token.
- Guest files remain in a private Storage bucket. Render validates the session ID and token before downloading and streaming any file.
- Session ID alone is not authorization.
- Internal sync, lookup, and deletion endpoints require `CLOUD_SYNC_SECRET`.
- Uploads are checked against MIME type and file magic bytes and are size-limited.
- Guest responses are no-store and have security headers; guest routes are rate-limited per process.
- Upload paths are deterministic, so retries replace the same session objects rather than creating duplicates.
- Offline capture and exports continue locally. Cloud sync remains pending and retries after connectivity returns.

## Existing UI and local features

- Existing kiosk UI, camera flow, local operator workflow, and printing are retained.
- Ready Gallery and Finished Photos use the actual generated strip preview, not a GIF substitute.
- Exported strip/GIF use `static/assets/Logo.png`.
- GIF footer places Facebook and Messenger at the bottom.
- Online guest gallery supports individual photos, strip, GIF, and ZIP download.

## Limitations to test after deployment

- Render hosts the cloud gallery only; it cannot access a USB camera or printer attached to the kiosk PC.
- GitHub Pages cannot execute Flask or Jinja.
- Real Supabase credentials and a deployed Render service are required for end-to-end cloud testing.
- Test one full session and scan its QR from a phone using mobile data before an event.
- The current retry worker and in-memory rate limiter are process-local. For multiple kiosks or high traffic, use a durable queue and shared rate limiter.
- A QR token is a bearer credential. Anyone holding the complete QR/link can access that one session, so do not post QR codes publicly.

## Tests

```bash
python -m unittest -v test_app.py test_cloud_service.py
python -m py_compile app.py cloud_service.py
```
