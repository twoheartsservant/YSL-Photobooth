# YSL Photobooth online migration changelog

- `app.py`: retained local camera/capture/print workflow and added Render sync, retry, guest QR, operator cloud lookup, private strip preview, and session deletion integration.
- `cloud_service.py`: Render Flask service now uses Supabase REST APIs for PostgreSQL session metadata and private Storage uploads/downloads/deletion.
- `requirements.txt`: removed Firebase Admin; uses requests, Flask, Gunicorn, and python-dotenv.
- `.env.example` and `render.yaml`: replaced Firebase credentials with Supabase URL, server-side secret key, and bucket configuration.
- `supabase_schema.sql`: reproducible table definition with RLS enabled and no public policies.
- Removed obsolete Firebase rules files.
- `test_cloud_service.py`: cloud route and token-validation smoke tests.
- `README_ONLINE_MIGRATION.md`: Supabase setup and deployment instructions.
