# Online migration change list

## Modified existing files
- `app.py` — atomic session manifests, Render/Firebase synchronization client, retry worker, cloud guest QR links, cloud session search/proxy, local session gallery correction, correct strip previews, and strict `static/assets/Logo.png` usage.
- `requirements.txt` — requests, Firebase Admin SDK, Gunicorn, and dotenv dependencies.
- `templates/ready_gallery.html` — exact session ID search and cloud strip preview.
- `templates/finished_photos.html` — photo-strip card preview and corrected alt text.
- `test_app.py` — verifies the Finished Photos operator gallery.

## New files
- `cloud_service.py` — Render-hosted private Firebase guest gallery and service API.
- `templates/cloud_guest.html` — responsive online guest gallery.
- `render.yaml` — Render Blueprint service definition.
- `.env.example` — placeholder-only environment configuration.
- `.gitignore` — excludes credentials, virtual environments, caches, and local guest media from Git.
- `firestore.rules` and `storage.rules` — deny direct client access; backend uses Admin SDK.
- `README_ONLINE_MIGRATION.md` — setup, deployment, security, and architecture instructions.
- `test_cloud_service.py` — cloud upload and access-control tests.
- `static/assets/messenger.png` — GIF footer Messenger icon.
