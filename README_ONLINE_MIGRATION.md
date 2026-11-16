# YSL Photobooth — Online Guest Gallery + Local Kiosk

## Important architecture note

The existing camera, preview relay, capture, image processing, GIF generation, and printing features require access to hardware attached to the photobooth computer. A Render web service cannot access that computer's USB/Wi-Fi camera or local printer. Therefore this migration uses **two Flask processes**:

1. `app.py` runs on the photobooth computer and keeps all hardware-dependent work local, including offline capture and printing.
2. `cloud_service.py` runs on Render and provides the private online guest gallery and Firebase synchronization API.

The existing Flask templates remain the kiosk/operator interface and are served by the local Flask process. GitHub stores the source code; GitHub Pages cannot execute Flask routes or render the Jinja templates, so do not use the GitHub Pages URL as the kiosk API origin. If you keep GitHub Pages enabled, use it only for static project information. The QR points to the Render cloud guest gallery, so guests can use mobile data or any Wi-Fi network.

## What changed

- Added `cloud_service.py`, a separate Render-hosted private gallery/API.
- Added Firebase Admin SDK integration for private Firestore and Storage access.
- Added session-scoped, cryptographically random guest tokens. Firestore stores only SHA-256 token hashes; the raw token is never written to Firestore.
- Added idempotent Storage paths under `guest_sessions/{sessionId}/` and Firestore records in `photobooth_sessions`.
- Added cloud sync after Ready Gallery processing, retry with bounded exponential backoff, QR regeneration, and cloud deletion when a local session is deleted.
- Added operator-only cloud session lookup and private strip preview proxy.
- Ready Gallery cards now use the actual generated photo strip, not the GIF, as their preview.
- Corrected exported strip and GIF logo lookup to `static/assets/Logo.png` only.
- GIF social footer now places Facebook and Messenger rows at the bottom of the exported frames.
- Added Render, Firebase rules, environment example, and tests.

## Firebase setup

1. Create a Firebase project.
2. Enable Firestore Database and Firebase Storage.
3. In Project Settings, create a service account key for the backend. Keep the JSON private.
4. Copy the Storage bucket name from Firebase Console.
5. Deploy `firestore.rules` to Firestore Rules and `storage.rules` to Storage Rules.
6. The rules intentionally deny direct browser access. The Render service uses Firebase Admin SDK and authorizes each guest request using the session ID plus secret token.

Do not put service-account JSON in GitHub, JavaScript, HTML, or a public GitHub Pages site.

## Render deployment

1. Push the project source to a private or public GitHub repository. Do not include `.env`, service-account JSON, `.venv`, or local `storage/` photo contents. The included `.gitignore` excludes these local session files.
2. In Render, create a Blueprint from the repository and select `render.yaml`, or create a Python web service manually.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn cloud_service:app --workers 1 --threads 8 --timeout 180`
5. Set these Render environment variables:
   - `CLOUD_SYNC_SECRET`: generate a long random value. Use the exact same value on the photobooth computer.
   - `FIREBASE_SERVICE_ACCOUNT_JSON`: paste the complete service-account JSON as a secret environment variable; alternatively set `FIREBASE_SERVICE_ACCOUNT_BASE64`.
   - `FIREBASE_STORAGE_BUCKET`: the bucket name shown in Firebase Console.
   - `GUEST_RATE_LIMIT_PER_MINUTE`: optional, defaults to 90.
6. Deploy and test `https://YOUR-RENDER-SERVICE.onrender.com/health`. It should return `ok: true` and `firebase: true`.

Render hosts only the cloud gallery/API, not the local camera/printer controller.

## Photobooth computer environment

Copy `.env.example` to `.env` (or configure environment variables in the Windows launch environment) and set:

```env
CLOUD_API_URL=https://YOUR-RENDER-SERVICE.onrender.com
CLOUD_PUBLIC_URL=https://YOUR-RENDER-SERVICE.onrender.com
CLOUD_SYNC_SECRET=THE_EXACT_SAME_SECRET_AS_RENDER
```

Install the updated requirements in the local Python environment and start the original kiosk using `start.bat` or `python app.py`. The local Flask service continues to use the existing `config.json` port and hardware integration.

## Session and security behavior

- Local capture continues to create one session ID; the same ID is used for cloud storage and the QR link.
- The QR URL is `https://YOUR-RENDER-SERVICE.onrender.com/g/{sessionId}/{accessToken}`.
- The token is generated with Python's cryptographic `secrets` module. Firestore stores only `accessTokenHash`.
- Guest files are not public Storage URLs. Render checks the token before reading private objects and streaming them to the guest.
- Session ID alone is never guest authorization.
- The internal sync and operator lookup routes require `CLOUD_SYNC_SECRET`.
- File type MIME and magic bytes are checked, and uploads are size-limited.
- Guest endpoints have per-process IP rate limiting and private/no-store response headers.
- Uploads use deterministic object paths and a single Firestore document per session, so retrying does not create duplicate session records.
- When internet is unavailable, local capture and exports continue. Cloud sync is marked pending and retried with backoff after connectivity returns.

## Offline behavior and limitations

- Keep using the photobooth computer's local Flask URL for the operator and guest capture screens. This preserves camera relay, capture, image processing, and printing.
- Online QR guest galleries require the Render service and Firebase configuration.
- Local-only mode remains available when cloud variables are absent; its QR uses the local LAN URL and therefore requires the same local network.
- Render's filesystem is not used as permanent photo storage. Guest media lives in private Firebase Storage.
- The current retry worker is process-local and checks pending session manifests every 30 seconds. For a multi-kiosk/high-volume production deployment, use a durable task queue and shared rate limiter.
- A guest token is a bearer credential: anyone possessing that complete QR/link can view that one session. Do not post QR codes publicly.
- Cloud session search is restricted to the local operator endpoint. The cloud internal endpoints are protected by the shared service secret.

## Test commands

```bash
python -m unittest -v test_app.py test_cloud_service.py
python -m py_compile app.py cloud_service.py
```

Firebase/Render integration tests require real configured credentials and a deployed service. Without them, test `/health` and one real QR on a mobile device after deployment.
