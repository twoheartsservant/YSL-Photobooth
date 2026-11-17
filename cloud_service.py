"""Private Supabase-backed guest gallery service for Render.

The kiosk Flask app remains local for camera, capture, processing and printing.
This service validates guest bearer tokens before reading private Supabase Storage.
"""
from __future__ import annotations
import hashlib, hmac, io, json, os, re, time
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote
import requests
from flask import Flask, abort, jsonify, request, render_template, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("CLOUD_MAX_UPLOAD_BYTES", str(180 * 1024 * 1024)))
SID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
ALLOWED = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif"}
BUCKET = os.getenv("SUPABASE_BUCKET", "ysl-photobooth")
_hits: dict[str, list[float]] = {}


def _config():
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_SECRET_KEY in the Render environment.")
    return url, key


def _headers(extra=None):
    _, key = _config()
    result = {"apikey": key, "Authorization": "Bearer " + key}
    if extra: result.update(extra)
    return result


def _db_get(session_id=None, token_hash=None):
    url, _ = _config()
    params = {"select": "*"}
    if session_id: params["id"] = "eq." + session_id
    if token_hash: params["access_token_hash"] = "eq." + token_hash
    response = requests.get(url + "/rest/v1/photobooth_sessions", headers=_headers(), params=params, timeout=25)
    response.raise_for_status()
    rows = response.json()
    return rows[0] if rows else None


def _db_upsert(record):
    url, _ = _config()
    response = requests.post(url + "/rest/v1/photobooth_sessions", headers=_headers({"Content-Type":"application/json", "Prefer":"resolution=merge-duplicates,return=minimal"}), json=record, timeout=30)
    response.raise_for_status()


def _db_delete(session_id):
    url, _ = _config()
    response = requests.delete(url + "/rest/v1/photobooth_sessions", headers=_headers(), params={"id":"eq." + session_id}, timeout=25)
    response.raise_for_status()


def _storage_upload(path, payload, content_type):
    url, _ = _config()
    endpoint = url + "/storage/v1/object/" + quote(BUCKET, safe="") + "/" + "/".join(quote(p, safe="") for p in path.split("/"))
    response = requests.put(endpoint, headers=_headers({"Content-Type":content_type, "x-upsert":"true"}), data=payload, timeout=(20, 240))
    response.raise_for_status()


def _storage_download(path):
    if not path or ".." in path.split("/"): abort(404)
    url, _ = _config()
    endpoint = url + "/storage/v1/object/" + quote(BUCKET, safe="") + "/" + "/".join(quote(p, safe="") for p in path.split("/"))
    response = requests.get(endpoint, headers=_headers(), timeout=(15, 120))
    if response.status_code == 404: abort(404)
    response.raise_for_status()
    return response.content, response.headers.get("Content-Type", "application/octet-stream")


def _storage_remove(paths):
    paths = [p for p in paths if p]
    if not paths: return
    url, _ = _config()
    response = requests.delete(url + "/storage/v1/object/" + quote(BUCKET, safe=""), headers=_headers({"Content-Type":"application/json"}), json={"prefixes":paths}, timeout=60)
    response.raise_for_status()


def _internal_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        expected = os.getenv("CLOUD_SYNC_SECRET", "")
        supplied = request.headers.get("Authorization", "")
        supplied = supplied[7:] if supplied.startswith("Bearer ") else ""
        if not expected or not hmac.compare_digest(expected, supplied): abort(401)
        return fn(*args, **kwargs)
    return wrapped


def _guest_rate_limit():
    now = time.time(); ip = request.remote_addr or "unknown"
    values = [t for t in _hits.get(ip, []) if now - t < 60]
    if len(values) >= int(os.getenv("GUEST_RATE_LIMIT_PER_MINUTE", "90")): abort(429)
    values.append(now); _hits[ip] = values


def _unpack(row):
    if not row: return None
    meta = row.get("metadata") or {}
    return {**meta, "sessionId":row.get("id"), "accessTokenHash":row.get("access_token_hash"),
            "createdAt":row.get("created_at"), "photos":meta.get("photos") or {},
            "strip":meta.get("strip") or {}, "gif":meta.get("gif") or {},
            "stripPath":row.get("strip_path") or "", "gifPath":row.get("gif_path") or ""}


def _token_doc(session_id: str, token: str):
    _guest_rate_limit()
    if not SID_RE.fullmatch(session_id) or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token or ""): abort(404)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    try: row = _db_get(session_id=session_id, token_hash=digest)
    except Exception as exc:
        app.logger.exception("Supabase session lookup failed")
        abort(503)
    data = _unpack(row)
    if not data or data.get("status") == "deleted": abort(404)
    expires = row.get("expires_at")
    if expires:
        try:
            if datetime.fromisoformat(expires.replace("Z", "+00:00")) < datetime.now(timezone.utc): abort(404)
        except ValueError: pass
    return data


@app.get("/health")
def health():
    try:
        _config()
        response = requests.get(os.getenv("SUPABASE_URL", "").rstrip("/") + "/rest/v1/photobooth_sessions?select=id&limit=1", headers=_headers(), timeout=15)
        response.raise_for_status()
        return jsonify(ok=True, service="ysl-photobooth-cloud", supabase=True, bucket=BUCKET)
    except Exception as exc:
        app.logger.exception("Supabase health check failed")
        return jsonify(ok=False, service="ysl-photobooth-cloud", supabase=False, error=str(exc)), 503


@app.post("/internal/sessions")
@_internal_required
def upsert_session():
    try: meta = json.loads(request.form.get("metadata", "{}"))
    except ValueError: return jsonify(ok=False, error="Invalid session metadata."), 400
    sid = str(meta.get("sessionId", "")); token = str(meta.get("accessToken", ""))
    if not SID_RE.fullmatch(sid) or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token): return jsonify(ok=False, error="Invalid session ID or access token."), 400
    try:
        old_row = _db_get(session_id=sid)
        old = _unpack(old_row) or {}
        files = {}
        for field in ("photo1", "photo2", "photo3", "strip", "gif"):
            upload = request.files.get(field)
            if not upload: continue
            content_type = (upload.mimetype or "").lower()
            if content_type not in ALLOWED: return jsonify(ok=False, error=f"Unsupported file type for {field}."), 415
            payload = upload.read()
            if not payload or len(payload) > app.config["MAX_CONTENT_LENGTH"]: return jsonify(ok=False, error=f"Invalid or oversized file: {field}."), 413
            signatures = {"image/jpeg":payload.startswith(b"\xff\xd8\xff"), "image/png":payload.startswith(b"\x89PNG\r\n\x1a\n"), "image/gif":payload.startswith((b"GIF87a",b"GIF89a"))}
            if not signatures.get(content_type, False): return jsonify(ok=False, error=f"File content does not match its declared type: {field}."), 415
            slot = field.replace("photo", "") if field.startswith("photo") else field
            path = f"guest_sessions/{sid}/{slot}{ALLOWED[content_type]}"
            _storage_upload(path, payload, content_type)
            files[field] = {"path":path, "contentType":content_type, "size":len(payload)}
        photos = dict(old.get("photos") or {})
        active_slots = {str(v) for v in meta.get("photoSlots", ("1","2","3")) if str(v) in ("1","2","3")}
        stale_paths = []
        for slot in set(photos) - active_slots:
            stale = photos.pop(slot, {})
            if stale.get("path"): stale_paths.append(stale["path"])
        for slot in ("1","2","3"):
            if "photo" + slot in files: photos[slot] = files["photo" + slot]
        strip = files.get("strip") or old.get("strip") or {}
        gif = files.get("gif") or old.get("gif") or {}
        if meta.get("clearOutputs"):
            stale_paths.extend(x.get("path") for x in (strip,gif) if x.get("path"))
            strip, gif = {}, {}
        if stale_paths: _storage_remove(stale_paths)
        now = datetime.now(timezone.utc).isoformat()
        record_meta = {"photos":photos,"strip":strip,"gif":gif,"status":str(meta.get("status") or "ready"),
                       "revision":int(meta.get("revision") or 0),"capturedAt":str(meta.get("createdAt") or meta.get("capturedAt") or now),
                       "updatedAt":now,"qrUrl":f"/g/{sid}/<guest-token>",
                       "stripUrl":f"/g/{sid}/<guest-token>/file/strip" if strip.get("path") else "",
                       "gifUrl":f"/g/{sid}/<guest-token>/file/gif" if gif.get("path") else ""}
        _db_upsert({"id":sid,"access_token_hash":hashlib.sha256(token.encode()).hexdigest(),
                    "created_at":(old_row or {}).get("created_at") or now,"photo_slots":sorted(active_slots),
                    "strip_path":strip.get("path"),"gif_path":gif.get("path"),"metadata":record_meta})
        return jsonify(ok=True, sessionId=sid, qrUrl=record_meta["qrUrl"], uploaded=list(files), status=record_meta["status"])
    except Exception as exc:
        app.logger.exception("Supabase session upload failed")
        return jsonify(ok=False, error="Supabase sync failed. Check Render logs and Supabase configuration."), 502


@app.get("/internal/sessions/<session_id>")
@_internal_required
def internal_session(session_id):
    if not SID_RE.fullmatch(session_id): abort(404)
    try: data = _unpack(_db_get(session_id=session_id))
    except Exception: abort(503)
    if not data: abort(404)
    data.pop("accessTokenHash", None)
    return jsonify(data)


@app.delete("/internal/sessions/<session_id>")
@_internal_required
def internal_delete_session(session_id):
    if not SID_RE.fullmatch(session_id): abort(404)
    try:
        row = _db_get(session_id=session_id)
        if row:
            data = _unpack(row); paths = [v.get("path") for v in data.get("photos",{}).values()]
            paths += [(data.get(k) or {}).get("path") for k in ("strip","gif")]
            _storage_remove(paths); _db_delete(session_id)
        return jsonify(ok=True, deleted=True)
    except Exception: abort(502)


@app.get("/internal/sessions/<session_id>/file/<kind>")
@_internal_required
def internal_session_file(session_id, kind):
    if not SID_RE.fullmatch(session_id) or kind not in ("strip","gif"): abort(404)
    try: data = _unpack(_db_get(session_id=session_id))
    except Exception: abort(503)
    if not data: abort(404)
    item = data.get(kind) or {}; payload,mime = _storage_download(item.get("path", ""))
    return send_file(io.BytesIO(payload),mimetype=mime,download_name=f"{session_id}-{kind}",max_age=0)


@app.get("/g/<session_id>/<token>")
def guest_gallery(session_id, token):
    data = _token_doc(session_id, token)
    return render_template("cloud_guest.html",sid=session_id,token=token,data=data)


@app.get("/g/<session_id>/<token>/photo/<slot>")
def guest_photo(session_id, token, slot):
    data = _token_doc(session_id,token); item=(data.get("photos") or {}).get(str(slot))
    if not item: abort(404)
    payload,mime=_storage_download(item.get("path", ""))
    return send_file(io.BytesIO(payload),mimetype=mime,download_name=f"YSL-Photo-{slot}.jpg",as_attachment=False,max_age=0)


@app.get("/g/<session_id>/<token>/file/<kind>")
def guest_final(session_id, token, kind):
    data=_token_doc(session_id,token); item=data.get("strip" if kind=="strip" else "gif" if kind=="gif" else "")
    if not item: abort(404)
    payload,mime=_storage_download(item.get("path", "")); ext=".gif" if kind=="gif" else (".png" if mime=="image/png" else ".jpg")
    return send_file(io.BytesIO(payload),mimetype=mime,download_name=f"YSL-{session_id}-Photo-Strip{ext}",as_attachment=request.args.get("download")=="1",max_age=0)


@app.get("/g/<session_id>/<token>/download/all.zip")
def guest_all(session_id, token):
    import zipfile
    data=_token_doc(session_id,token); memory=io.BytesIO()
    with zipfile.ZipFile(memory,"w",zipfile.ZIP_DEFLATED) as archive:
        for slot,item in sorted((data.get("photos") or {}).items()):
            payload,_=_storage_download(item.get("path", "")); archive.writestr(f"Photo-{slot}.jpg",payload)
        for key,name in (("strip","Photo-Strip"),("gif","Flashback.gif")):
            item=data.get(key) or {}
            if item.get("path"):
                payload,_=_storage_download(item["path"]); archive.writestr(name if name.endswith(".gif") else name+".jpg",payload)
    memory.seek(0)
    return send_file(memory,mimetype="application/zip",as_attachment=True,download_name=f"YSL-{session_id}.zip")
