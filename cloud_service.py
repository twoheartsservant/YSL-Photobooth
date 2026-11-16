"""Private Firebase-backed guest gallery service for Render.

The kiosk Flask app remains local so it can access the attached camera and printer.
This separate Flask service runs on Render and serves authenticated guest galleries.
"""
from __future__ import annotations
import hashlib, hmac, io, json, os, re, time
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, abort, jsonify, request, render_template, send_file
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("CLOUD_MAX_UPLOAD_BYTES", str(180 * 1024 * 1024)))
SID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
ALLOWED = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif"}
_firebase_error = None
_db = _bucket = None
_hits: dict[str, list[float]] = {}


def _init_firebase():
    global _db, _bucket, _firebase_error
    if _db is not None:
        return
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore, storage
        if not firebase_admin._apps:
            raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
            if not raw:
                import base64
                encoded = os.getenv("FIREBASE_SERVICE_ACCOUNT_BASE64", "").strip()
                raw = base64.b64decode(encoded).decode() if encoded else ""
            if not raw:
                raise RuntimeError("Set FIREBASE_SERVICE_ACCOUNT_JSON or FIREBASE_SERVICE_ACCOUNT_BASE64.")
            cred = credentials.Certificate(json.loads(raw))
            firebase_admin.initialize_app(cred, {"storageBucket": os.environ["FIREBASE_STORAGE_BUCKET"]})
        _db = firestore.client()
        _bucket = storage.bucket()
        _firebase_error = None
    except Exception as exc:
        _firebase_error = str(exc)
        raise


def _internal_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        expected = os.getenv("CLOUD_SYNC_SECRET", "")
        supplied = request.headers.get("Authorization", "")
        supplied = supplied[7:] if supplied.startswith("Bearer ") else ""
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(401)
        return fn(*args, **kwargs)
    return wrapped


def _guest_rate_limit():
    now = time.time(); ip = request.remote_addr or "unknown"
    values = [t for t in _hits.get(ip, []) if now - t < 60]
    if len(values) >= int(os.getenv("GUEST_RATE_LIMIT_PER_MINUTE", "90")):
        abort(429)
    values.append(now); _hits[ip] = values


def _token_doc(session_id: str, token: str):
    _guest_rate_limit()
    if not SID_RE.fullmatch(session_id) or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token or ""):
        abort(404)
    _init_firebase()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    matches = _db.collection("photobooth_sessions").where("accessTokenHash", "==", digest).limit(1).stream()
    for snap in matches:
        if snap.id != session_id:
            continue
        data = snap.to_dict() or {}
        if data.get("status") == "deleted":
            break
        return data
    abort(404)


def _read_blob(path: str):
    if not path or ".." in path.split("/"):
        abort(404)
    _init_firebase()
    blob = _bucket.blob(path)
    if not blob.exists():
        abort(404)
    data = blob.download_as_bytes()
    content_type = blob.content_type or "application/octet-stream"
    return data, content_type


@app.get("/health")
def health():
    try:
        _init_firebase()
        return jsonify(ok=True, service="ysl-photobooth-cloud", firebase=True)
    except Exception as exc:
        return jsonify(ok=False, service="ysl-photobooth-cloud", firebase=False, error=str(exc)), 503


@app.post("/internal/sessions")
@_internal_required
def upsert_session():
    _init_firebase()
    try:
        meta = json.loads(request.form.get("metadata", "{}"))
    except ValueError:
        return jsonify(ok=False, error="Invalid session metadata."), 400
    sid = str(meta.get("sessionId", "")); token = str(meta.get("accessToken", ""))
    if not SID_RE.fullmatch(sid) or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token):
        return jsonify(ok=False, error="Invalid session ID or access token."), 400
    files = {}
    for field in ("photo1", "photo2", "photo3", "strip", "gif"):
        upload = request.files.get(field)
        if not upload:
            continue
        content_type = (upload.mimetype or "").lower()
        if content_type not in ALLOWED:
            return jsonify(ok=False, error=f"Unsupported file type for {field}."), 415
        payload = upload.read()
        if not payload or len(payload) > app.config["MAX_CONTENT_LENGTH"]:
            return jsonify(ok=False, error=f"Invalid or oversized file: {field}."), 413
        signatures = {"image/jpeg": payload.startswith(b"\xff\xd8\xff"),
                      "image/png": payload.startswith(b"\x89PNG\r\n\x1a\n"),
                      "image/gif": payload.startswith((b"GIF87a", b"GIF89a"))}
        if not signatures.get(content_type, False):
            return jsonify(ok=False, error=f"File content does not match its declared type: {field}."), 415
        ext = ALLOWED[content_type]
        slot = field.replace("photo", "") if field.startswith("photo") else field
        storage_path = f"guest_sessions/{sid}/{slot}{ext}"
        blob = _bucket.blob(storage_path)
        blob.upload_from_string(payload, content_type=content_type, timeout=180)
        files[field] = {"path": storage_path, "contentType": content_type, "size": len(payload)}
    doc = _db.collection("photobooth_sessions").document(sid)
    old = doc.get().to_dict() or {}
    photo_map = dict(old.get("photos") or {})
    active_slots = {str(v) for v in meta.get("photoSlots", ("1", "2", "3")) if str(v) in ("1", "2", "3")}
    for stale_slot in set(photo_map) - active_slots:
        stale = photo_map.pop(stale_slot, {})
        if stale.get("path"):
            try: _bucket.blob(stale["path"]).delete()
            except Exception: app.logger.warning("Could not remove stale guest photo %s", stale["path"])
    for slot in ("1", "2", "3"):
        f = files.get("photo" + slot)
        if f: photo_map[slot] = f
    strip = files.get("strip") or old.get("strip") or {}
    gif = files.get("gif") or old.get("gif") or {}
    if meta.get("clearOutputs"):
        for stale in (strip, gif):
            if stale.get("path"):
                try: _bucket.blob(stale["path"]).delete()
                except Exception: app.logger.warning("Could not remove stale output %s", stale["path"])
        strip, gif = {}, {}
    now = datetime.now(timezone.utc).isoformat()
    created = old.get("createdAt") or now
    record = {
        "sessionId": sid,
        "accessTokenHash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "createdAt": created,
        "updatedAt": now,
        "photos": photo_map,
        "strip": strip,
        "gif": gif,
        "stripPath": strip.get("path", ""),
        "gifPath": gif.get("path", ""),
        "stripUrl": f"/g/{sid}/<guest-token>/file/strip" if strip.get("path") else "",
        "gifUrl": f"/g/{sid}/<guest-token>/file/gif" if gif.get("path") else "",
        # Never persist the bearer token or a usable guest URL in Firestore.
        "qrUrl": f"/g/{sid}/<guest-token>",
        "status": str(meta.get("status") or "ready"),
        "revision": int(meta.get("revision") or 0),
        "capturedAt": str(meta.get("createdAt") or meta.get("capturedAt") or created),
    }
    doc.set(record, merge=True)
    return jsonify(ok=True, sessionId=sid, qrUrl=record["qrUrl"], uploaded=list(files), status=record["status"])


@app.get("/internal/sessions/<session_id>")
@_internal_required
def internal_session(session_id):
    if not SID_RE.fullmatch(session_id): abort(404)
    _init_firebase(); snap = _db.collection("photobooth_sessions").document(session_id).get()
    if not snap.exists: abort(404)
    data = snap.to_dict() or {}; data.pop("accessTokenHash", None)
    return jsonify(data)


@app.delete("/internal/sessions/<session_id>")
@_internal_required
def internal_delete_session(session_id):
    if not SID_RE.fullmatch(session_id): abort(404)
    _init_firebase(); doc = _db.collection("photobooth_sessions").document(session_id)
    snap = doc.get()
    if snap.exists:
        data = snap.to_dict() or {}
        paths = [item.get("path") for item in (data.get("photos") or {}).values()]
        paths += [(data.get(k) or {}).get("path") for k in ("strip", "gif")]
        for path in paths:
            if path:
                try: _bucket.blob(path).delete()
                except Exception: app.logger.warning("Could not delete cloud object %s", path)
        doc.delete()
    return jsonify(ok=True, deleted=True)


@app.get("/internal/sessions/<session_id>/file/<kind>")
@_internal_required
def internal_session_file(session_id, kind):
    if not SID_RE.fullmatch(session_id) or kind not in ("strip", "gif"):
        abort(404)
    _init_firebase(); snap = _db.collection("photobooth_sessions").document(session_id).get()
    if not snap.exists: abort(404)
    data = snap.to_dict() or {}; item = data.get(kind) or {}
    payload, mime = _read_blob(item.get("path", ""))
    return send_file(io.BytesIO(payload), mimetype=mime, download_name=f"{session_id}-{kind}", max_age=0)


@app.get("/g/<session_id>/<token>")
def guest_gallery(session_id, token):
    data = _token_doc(session_id, token)
    return render_template("cloud_guest.html", sid=session_id, token=token, data=data)


@app.get("/g/<session_id>/<token>/photo/<slot>")
def guest_photo(session_id, token, slot):
    data = _token_doc(session_id, token)
    item = (data.get("photos") or {}).get(str(slot))
    if not item: abort(404)
    payload, mime = _read_blob(item.get("path", ""))
    return send_file(io.BytesIO(payload), mimetype=mime, download_name=f"YSL-Photo-{slot}.jpg", as_attachment=False, max_age=0)


@app.get("/g/<session_id>/<token>/file/<kind>")
def guest_final(session_id, token, kind):
    data = _token_doc(session_id, token)
    item = data.get("strip" if kind == "strip" else "gif" if kind == "gif" else "")
    if not item: abort(404)
    payload, mime = _read_blob(item.get("path", ""))
    ext = ".gif" if kind == "gif" else (".png" if mime == "image/png" else ".jpg")
    return send_file(io.BytesIO(payload), mimetype=mime, download_name=f"YSL-{session_id}-Photo-Strip{ext}", as_attachment=request.args.get("download") == "1", max_age=0)


@app.get("/g/<session_id>/<token>/download/all.zip")
def guest_all(session_id, token):
    import zipfile
    data = _token_doc(session_id, token); memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
        for slot, item in sorted((data.get("photos") or {}).items()):
            payload, _ = _read_blob(item.get("path", "")); archive.writestr(f"Photo-{slot}.jpg", payload)
        for key, name in (("strip", "Photo-Strip.jpg"), ("gif", "Flashback.gif")):
            item = data.get(key) or {}
            if item.get("path"):
                payload, _ = _read_blob(item["path"]); archive.writestr(name, payload)
    memory.seek(0)
    return send_file(memory, mimetype="application/zip", as_attachment=True, download_name=f"YSL-{session_id}.zip")


@app.after_request
def security_headers(response):
    if request.path.startswith("/g/"):
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.errorhandler(413)
def too_large(_): return jsonify(ok=False, error="Upload exceeds the configured size limit."), 413


@app.errorhandler(500)
def internal_error(_):
    app.logger.exception("Unhandled cloud service error")
    return jsonify(ok=False, error="The cloud gallery service encountered an internal error."), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
