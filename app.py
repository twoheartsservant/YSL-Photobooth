from flask import Flask, render_template, jsonify, request, send_file, abort, redirect, make_response
from pathlib import Path
from PIL import Image, ImageOps, ImageDraw, ImageFont
import threading, json, uuid, io, os, socket, time, subprocess, shutil, zipfile, re, base64, secrets, hmac, ipaddress
from datetime import datetime
from urllib.parse import urlparse
from werkzeug.exceptions import HTTPException
import qrcode
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
STORE = ROOT / "storage"
CONFIG_PATH = ROOT / "config.json"
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
lock = threading.RLock()
gif_generation_lock = threading.Lock()

def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

def require_local_operator():
    """Keep operator galleries and destructive controls on the photobooth computer."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(404)


def require_local_camera_relay():
    """Allow the live preview relay on loopback and private LAN addresses only.

    The operator may open the dashboard through the PC's LAN address. In that
    case Flask sees the PC's private address rather than 127.0.0.1, and a strict
    loopback-only check incorrectly blocks the camera frames needed by guest.html.
    Public Internet addresses remain rejected.
    """
    address = request.remote_addr or ""
    try:
        ip = ipaddress.ip_address(address)
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not (ip.is_loopback or ip.is_private or ip.is_link_local):
            abort(404)
    except ValueError:
        abort(404)


def session_meta(sid):
    if not valid_session_id(sid):
        return None
    path = STORE / "sessions" / sid / "session.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def write_session_meta(sid, payload):
    """Atomically replace a session manifest so polling readers never see partial JSON."""
    if not valid_session_id(sid):
        raise ValueError("Invalid session ID.")
    folder = STORE / "sessions" / sid
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "session.json"
    temporary = folder / f".session-{uuid.uuid4().hex}.tmp"
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    return target


def get_or_create_access_token(sid, saved=None):
    saved = saved if saved is not None else session_meta(sid)
    if not isinstance(saved, dict):
        return ""
    token = str(saved.get("access_token") or "")
    try:
        expires_at = float(saved.get("access_token_expires_at", 0))
    except (TypeError, ValueError):
        expires_at = 0
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token) or expires_at <= time.time():
        token = secrets.token_urlsafe(32)
        saved["access_token"] = token
        saved["access_token_expires_at"] = time.time() + (30 * 24 * 60 * 60)
        try:
            write_session_meta(sid, saved)
        except OSError:
            return ""
    return token



def local_gallery_url(request_host=None):
    """Build a gallery URL reachable only on the photobooth LAN.

    Deliberately ignores photo_share_base_url: cloud/public URLs are not valid
    for the no-internet YSL PHOTOBOOTH router.
    """
    cfg = load_config()
    configured_host = str(cfg.get("lan_host") or "").strip()
    host = configured_host
    if host:
        try:
            parsed_ip = ipaddress.ip_address(host)
            if not (parsed_ip.is_private or parsed_ip.is_loopback or parsed_ip.is_link_local):
                host = ""
        except ValueError:
            # Accept a local hostname only when explicitly configured.
            if "." in host and not host.lower().endswith((".local",)):
                host = ""
    if not host:
        candidate = (request_host or request.host or "").split(":")[0].strip("[]")
        try:
            candidate_ip = ipaddress.ip_address(candidate)
            if candidate_ip.is_private or candidate_ip.is_link_local:
                host = candidate
        except ValueError:
            if candidate.lower() in ("localhost",):
                host = ""
    if not host:
        detected = current_ip()
        try:
            detected_ip = ipaddress.ip_address(detected)
            if detected_ip.is_private or detected_ip.is_link_local:
                host = detected
        except ValueError:
            pass
    if not host:
        raise RuntimeError("Could not detect a private LAN address. Set 'lan_host' in config.json to the photobooth computer's Wi-Fi IPv4 address.")
    return f"http://{host}:{int(cfg.get('port', 5000))}"

def session_from_token(token):
    if not token or len(token) > 128:
        return None, None
    for folder in (STORE / "sessions").iterdir():
        if not folder.is_dir():
            continue
        saved = session_meta(folder.name)
        stored = get_or_create_access_token(folder.name, saved) if saved else ""
        if stored and hmac.compare_digest(stored, token):
            return folder.name, saved
    return None, None


def require_token_session(token):
    sid, saved = session_from_token(token)
    if not sid or not saved:
        abort(404)
    return sid, saved


def cloud_configured():
    return bool(os.getenv("CLOUD_API_URL", "").strip() and os.getenv("CLOUD_SYNC_SECRET", "").strip())


def cloud_public_url():
    return os.getenv("CLOUD_PUBLIC_URL", "").strip().rstrip("/") or os.getenv("CLOUD_API_URL", "").strip().rstrip("/")


def sync_session_to_cloud(sid, saved):
    """Idempotently synchronize this session's private media to the Render service."""
    if not cloud_configured():
        return {"ok": False, "pending": True, "message": "Cloud service is not configured."}
    if not valid_session_id(sid):
        return {"ok": False, "pending": True, "message": "Invalid session ID."}
    token = get_or_create_access_token(sid, saved)
    if not token:
        return {"ok": False, "pending": True, "message": "Could not create the guest access token."}
    final = saved.get("final") or {}
    photos = saved.get("photos") or {}
    fields = {"metadata": (None, json.dumps({
        "sessionId": sid, "accessToken": token, "status": saved.get("status", "ready"),
        "revision": saved.get("revision", 0), "createdAt": saved.get("started_at") or saved.get("createdAt"),
        "photoSlots": sorted(str(k) for k in (saved.get("photos") or {}).keys()),
        "clearOutputs": not bool((saved.get("final") or {}).get("jpg") and (saved.get("final") or {}).get("gif"))
    }))}
    opened = []
    try:
        upload_media = (saved.get("cloudSync") or {}).get("status") != "synced"
        for slot in (("1", "2", "3") if upload_media else ()):
            filename = Path(str(photos.get(str(slot)) or "")).name
            path = STORE / "original" / filename
            if filename.startswith(sid) and path.is_file():
                fh = path.open("rb"); opened.append(fh)
                fields[f"photo{slot}"] = (filename, fh, "image/jpeg")
        strip_name = Path(str(final.get("jpg") or "")).name
        strip_path = STORE / "final" / strip_name
        if upload_media and strip_name.startswith(sid) and strip_path.is_file():
            fh = strip_path.open("rb"); opened.append(fh)
            fields["strip"] = (strip_name, fh, "image/jpeg")
        gif_name = Path(str(final.get("gif") or "")).name
        gif_path = STORE / "final" / gif_name
        if upload_media and gif_name.startswith(sid) and gif_path.is_file():
            fh = gif_path.open("rb"); opened.append(fh)
            fields["gif"] = (gif_name, fh, "image/gif")
        response = requests.post(
            cloud_public_url() + "/internal/sessions", data={k:v[1] for k,v in fields.items() if k == "metadata"},
            files={k:v for k,v in fields.items() if k != "metadata"},
            headers={"Authorization": "Bearer " + os.getenv("CLOUD_SYNC_SECRET", "")}, timeout=(15, 240)
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "Cloud service rejected the upload.")
        return result
    except Exception as exc:
        app.logger.warning("Cloud sync pending for %s: %s", sid, exc)
        return {"ok": False, "pending": True, "message": str(exc)}
    finally:
        for fh in opened:
            try: fh.close()
            except Exception: pass


def delete_cloud_session(sid):
    if not cloud_configured() or not valid_session_id(sid):
        return
    try:
        requests.delete(cloud_public_url() + f"/internal/sessions/{sid}",
            headers={"Authorization": "Bearer " + os.getenv("CLOUD_SYNC_SECRET", "")}, timeout=30).raise_for_status()
    except requests.RequestException:
        app.logger.exception("Could not delete cloud copy for session %s", sid)


def apply_cloud_guest_link(sid, saved):
    """After cloud upload succeeds, make both QR surfaces use the public HTTPS URL."""
    result = sync_session_to_cloud(sid, saved)
    if not result.get("ok"):
        previous = saved.get("cloudSync") or {}
        attempts = int(previous.get("attempts", 0)) + 1
        delay = min(600, 30 * (2 ** min(attempts - 1, 4)))
        saved["cloudSync"] = {"status": "pending", "attempts": attempts,
            "retryAfter": time.time() + delay, "lastError": result.get("message", "Upload pending")}
        try:
            write_session_meta(sid, saved)
        except OSError:
            app.logger.exception("Could not persist cloud retry state for %s", sid)
        return False
    token = get_or_create_access_token(sid, saved)
    url = f"{cloud_public_url()}/g/{sid}/{token}"
    parsed_url = urlparse(url)
    hostname = (parsed_url.hostname or "").lower()
    if (parsed_url.scheme != "https" or not hostname or hostname in ("localhost", "127.0.0.1", "::1")
            or not parsed_url.path.endswith(f"/g/{sid}/{token}")):
        previous = saved.get("cloudSync") or {}
        saved["cloudSync"] = {"status": "pending", "attempts": int(previous.get("attempts", 0)) + 1,
                              "retryAfter": time.time() + 60,
                              "lastError": "The configured cloud public URL is not a valid HTTPS gallery address."}
        write_session_meta(sid, saved)
        return False
    qr_name = f"{sid}_finished_qr.png"
    qr_path = STORE / "qr" / qr_name
    qrcode.make(url, box_size=8, border=2).save(qr_path)
    final = dict(saved.get("final") or {})
    final.update(url=url, qr=f"/qr/{qr_name}")
    saved["final"] = final
    saved["cloudSync"] = {"status": "synced", "updatedAt": datetime.now().isoformat(timespec="seconds")}
    write_session_meta(sid, saved)
    with lock:
        if state.get("session_id") == sid:
            state["final"] = final
            state["revision"] = max(int(state.get("revision", 0)), int(saved.get("revision", 0)))
    return True


def cloud_retry_worker():
    """Retry pending uploads with bounded exponential backoff while the kiosk runs."""
    while True:
        time.sleep(30)
        if not cloud_configured():
            continue
        try:
            for folder in (STORE / "sessions").iterdir():
                meta_path = folder / "session.json"
                if not folder.is_dir() or not meta_path.is_file():
                    continue
                try:
                    saved = json.loads(meta_path.read_text(encoding="utf-8"))
                    cloud_state = saved.get("cloudSync") or {}
                    if cloud_state.get("status") != "pending" or float(cloud_state.get("retryAfter", 0)) > time.time():
                        continue
                    if saved.get("status") not in ("ready", "finished"):
                        continue
                    if not gif_generation_lock.acquire(blocking=False):
                        continue
                    try:
                        apply_cloud_guest_link(folder.name, saved)
                    finally:
                        gif_generation_lock.release()
                except (OSError, ValueError, TypeError):
                    app.logger.exception("Could not retry cloud sync for %s", folder.name)
        except Exception:
            app.logger.exception("Cloud retry worker encountered an error")


def ensure_dirs():
    for p in ["original", "final", "qr", "sessions"]:
        (STORE / p).mkdir(parents=True, exist_ok=True)

ensure_dirs()
state = {"active": False, "session_id": None, "started_at": None, "photos": {}, "complete": False, "revision": 0, "shot_count": 3, "final": None, "status": "idle", "access_token": None, "phone_upload_token": None, "capture_requested": False}
selected_camera = {"device_id": ""}
live_frame_lock = threading.Lock()
live_frame_condition = threading.Condition(live_frame_lock)
live_camera_frame = {"data": b"", "updated": 0.0, "sequence": 0}
threading.Thread(target=cloud_retry_worker, daemon=True, name="ysl-cloud-sync-retry").start()

def snapshot():
    # Copy mutable state while holding the same re-entrant lock.
    with lock:
        return {**state, "photos": dict(state["photos"])}

def public_state():
    s = snapshot()
    s.pop("access_token", None)  # Never expose bearer tokens through the kiosk state API.
    s.pop("access_token_expires_at", None)
    s.pop("phone_upload_token", None)
    s["photo_count"] = len(s["photos"])
    s["shot_count"] = int(s.get("shot_count") or load_config().get("photo_count", 3))
    s["final"] = s.get("final")
    s["photos"] = [{"slot": int(k), "filename": v, "url": f"/photo/{s['session_id']}/{v}"}
                   for k, v in sorted(s["photos"].items())] if s["session_id"] else []
    return s

def start_session(shot_count=None):
    with lock:
        if state["active"]:
            return False, "A session is already active."
        sid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        (STORE/"sessions"/sid).mkdir(parents=True, exist_ok=True)
        state.update(active=True, session_id=sid, started_at=datetime.now().isoformat(timespec="seconds"),
                     photos={}, complete=False, final=None, status="capturing", shot_count=3, capture_requested=False, access_token=secrets.token_urlsafe(32), access_token_expires_at=time.time() + (30 * 24 * 60 * 60), phone_upload_token=secrets.token_urlsafe(24), revision=state["revision"]+1)
        persist_state()
        return True, "Session started."

def persist_state():
    with lock:
        sid = state.get("session_id")
        if sid:
            # Preserve cloud-sync metadata and other manifest fields not mirrored in live state.
            existing = session_meta(sid) or {}
            payload = {**existing, **state, "photos": dict(state["photos"])}
            write_session_meta(sid, payload)

def current_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

@app.after_request
def protect_private_guest_responses(response):
    if request.path.startswith("/g/"):
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@app.get("/")
def operator():
    return render_template("operator.html")

@app.get("/guest")
def guest():
    return render_template("guest.html")

@app.get("/guest.html")
def guest_review_page():
    # Legacy un-tokenized URLs show only the capture screen, never a session gallery.
    return render_template("guest.html")


@app.get("/g/<token>")
def token_guest_gallery(token):
    sid, saved = require_token_session(token)
    return render_template("guest_session.html", sid=sid, photos=saved.get("photos", {}),
                           final=saved.get("final") or {}, revision=saved.get("revision", 0), access_token=token)


@app.get("/g/<token>/photo/<filename>")
def token_guest_photo(token, filename):
    sid, saved = require_token_session(token)
    safe = Path(filename).name
    if safe not in [Path(str(v)).name for v in (saved.get("photos") or {}).values()] or not safe.startswith(sid):
        abort(404)
    path = STORE / "sessions" / sid / safe
    if not path.is_file(): path = STORE / "original" / safe
    if not path.is_file(): abort(404)
    return send_file(path, as_attachment=False)


@app.get("/g/<token>/download/photo/<filename>")
def token_guest_download_photo(token, filename):
    sid, saved = require_token_session(token)
    safe = Path(filename).name
    if safe not in [Path(str(v)).name for v in (saved.get("photos") or {}).values()] or not safe.startswith(sid):
        abort(404)
    path = STORE / "sessions" / sid / safe
    if not path.is_file(): path = STORE / "original" / safe
    if not path.is_file(): abort(404)
    return send_file(path, as_attachment=True, download_name=f"YSL-Photobooth-{sid}-Photo-{safe.split('_photo_')[1].split('_')[0]}.jpg")


@app.get("/g/<token>/view/<kind>")
def token_guest_view_final(token, kind):
    sid, saved = require_token_session(token)
    final = saved.get("final") or {}
    key = {"gif":"gif", "strip":"jpg", "strip-png":"png"}.get(kind)
    if not key: abort(404)
    name = Path(str(final.get(key) or "")).name
    if not name or not name.startswith(sid): abort(404)
    path = STORE / "final" / name
    if not path.is_file(): abort(404)
    return send_file(path, as_attachment=False)


@app.get("/g/<token>/final/<kind>")
def token_guest_final(token, kind):
    sid, saved = require_token_session(token)
    final = saved.get("final") or {}
    key = {"gif":"gif", "strip":"jpg", "strip-png":"png"}.get(kind)
    if not key: abort(404)
    name = Path(str(final.get(key) or "")).name
    if not name or not name.startswith(sid): abort(404)
    path = STORE / "final" / name
    if not path.is_file(): abort(404)
    return send_file(path, as_attachment=True, download_name=f"YSL-Photobooth-{sid}-{'Flashback.gif' if kind == 'gif' else 'Photo-Strip.jpg'}")


@app.get("/g/<token>/download/all.zip")
def token_guest_download_all(token):
    sid, saved = require_token_session(token)
    photos = saved.get("photos") or {}
    if len(photos) != 3: abort(404)
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
        folder = STORE / "sessions" / sid
        for n in (1, 2, 3):
            safe = Path(str(photos.get(str(n)) or "")).name
            if not safe.startswith(sid): abort(404)
            path = folder / safe
            if not path.is_file(): path = STORE / "original" / safe
            if not path.is_file(): abort(404)
            archive.write(path, f"YSL-Photobooth-{sid}/Photo-{n}.jpg")
        final = saved.get("final") or {}
        for key, archive_name in (("gif", "Flashback.gif"), ("jpg", "Photo-Strip.jpg")):
            output_name = Path(str(final.get(key) or "")).name
            if not output_name or not output_name.startswith(sid): abort(404)
            output_path = STORE / "final" / output_name
            if not output_path.is_file(): abort(404)
            archive.write(output_path, f"YSL-Photobooth-{sid}/{archive_name}")
    memory.seek(0)
    return send_file(memory, mimetype="application/zip", as_attachment=True, download_name=f"YSL-Photobooth-{sid}-Complete-Gallery.zip")

@app.get("/preview/main")
def main_preview():
    return render_template("main_preview.html")

@app.get("/phone")
def phone():
    abort(404)


@app.get("/phone/<token>")
def phone_with_token(token):
    with lock:
        valid = bool(state.get("active") and state.get("phone_upload_token") and hmac.compare_digest(str(state["phone_upload_token"]), token))
    if not valid: abort(404)
    return render_template("phone_camera.html", phone_token=token)

@app.get("/api/state")
def api_state():
    require_local_operator()
    return jsonify(public_state())

@app.post("/api/session/start")
def api_start():
    require_local_operator()
    data = request.get_json(silent=True) or {}
    ok, msg = start_session(data.get("shot_count"))
    return jsonify(ok=ok, message=msg, state=public_state()), (200 if ok else 409)

@app.post("/api/capture/start")
def api_capture_start():
    """Signal the guest display to run the single synchronized three-shot sequence."""
    require_local_operator()
    with lock:
        if not state.get("active") or not state.get("session_id"):
            return jsonify(ok=False, message="Start a session before capturing."), 409
        if state.get("capture_requested") or state.get("photos"):
            return jsonify(ok=False, message="This session's capture sequence has already started."), 409
        state["capture_requested"] = True
        state["revision"] += 1
        persist_state()
        return jsonify(ok=True, message="Capture sequence sent to the guest screen.", state=public_state())


@app.post("/api/session/reset")
def api_reset():
    require_local_operator()
    # Reset only the live pointers. Original files and finished exports remain on disk.
    with lock:
        state.update(active=False, session_id=None, started_at=None, photos={}, complete=False, final=None,
                     status="idle", shot_count=3, capture_requested=False, access_token=None, access_token_expires_at=None, phone_upload_token=None, revision=state["revision"]+1)
        return jsonify(ok=True, message="Ready for a new session. Previously saved photos were kept.", state=public_state())

@app.post("/api/session/end")
def api_end():
    require_local_operator()
    with lock:
        if not state["active"]:
            return jsonify(ok=False, message="No active session."), 409
        state["active"] = False
        state["capture_requested"] = False
        state["status"] = "ready" if state.get("photos") else "draft"
        state["revision"] += 1
        persist_state()
    return jsonify(ok=True, message="Session ended.", state=public_state())

@app.post("/api/session/shot-count")
def api_shot_count():
    require_local_operator()
    data=request.get_json(silent=True) or {}
    try: count=3
    except (TypeError,ValueError): return jsonify(ok=False,message="Shot count must be between 1 and 10."),400
    with lock:
        if state["active"] and state["photos"]:
            return jsonify(ok=False,message="Shot count cannot change after capturing begins. Start a new session to change it."),409
        state["shot_count"]=count
        cfg=load_config(); cfg["photo_count"]=count; save_config(cfg)
        state["revision"]+=1
        if state["session_id"]: persist_state()
    return jsonify(ok=True,message=f"Shot count set to {count}.",state=public_state())

@app.post("/api/prep-frames")
def save_prep_frames():
    require_local_operator()
    data=request.get_json(silent=True) or {}
    try:
        slot=int(data.get("slot", 0))
        frames=data.get("frames") or []
        if slot not in (1, 2, 3) or not isinstance(frames, list) or len(frames) < 12:
            return jsonify(ok=False, message="The preparation recording is incomplete."), 400
        with lock:
            if not state["active"] or not state.get("session_id"):
                return jsonify(ok=False, message="There is no active photo session."), 409
            sid=state["session_id"]
        clip_dir=STORE/"sessions"/sid/f"prep_{slot}"
        clip_dir.mkdir(parents=True, exist_ok=True)
        for old in clip_dir.glob("frame_*.jpg"):
            old.unlink(missing_ok=True)
        saved=0
        for index, encoded in enumerate(frames[:30], start=1):
            raw=base64.b64decode(str(encoded).split(",")[-1], validate=True)
            with Image.open(io.BytesIO(raw)) as source:
                frame=source.convert("RGB")
                if frame.width < 80 or frame.height < 60:
                    continue
                frame.thumbnail((640, 360), Image.Resampling.LANCZOS)
                frame.save(clip_dir/f"frame_{index:03d}.jpg", "JPEG", quality=82, optimize=True)
                saved += 1
        if saved < 12:
            return jsonify(ok=False, message="Not enough preparation frames were received."), 400
        return jsonify(ok=True, message=f"Saved preparation footage for photo {slot}.", frames=saved)
    except (ValueError, TypeError, base64.binascii.Error, OSError):
        return jsonify(ok=False, message="Could not process the preparation footage."), 400

@app.post("/api/photo")
def api_photo():
    require_local_operator()
    with lock:
        if not state["active"]:
            return jsonify(ok=False, message="There is no active session."), 409
        sid = state["session_id"]
        try:
            slot = int(request.form.get("slot", "0"))
        except ValueError:
            return jsonify(ok=False, message="Invalid photo slot."), 400
        if slot < 1 or slot > int(state.get("shot_count", 3)):
            return jsonify(ok=False, message=f"Photo slot must be between 1 and {state.get('shot_count',3)}."), 400
        if str(slot) in state["photos"]:
            return jsonify(ok=False, message="That photo slot is already filled. Ask the operator to retake it."), 409
        file = request.files.get("photo")
        if not file:
            return jsonify(ok=False, message="No image was received."), 400
        try:
            image = Image.open(file.stream)
            image.verify()
            file.stream.seek(0)
            image = Image.open(file.stream).convert("RGB")
        except Exception:
            return jsonify(ok=False, message="The uploaded file is not a valid image."), 400
        filename = f"{sid}_photo_{slot}_{uuid.uuid4().hex[:8]}.jpg"
        target = STORE/"original"/filename
        image.save(target, "JPEG", quality=95, optimize=True)
        shutil.copy2(target, STORE/"sessions"/sid/filename)
        state["photos"][str(slot)] = filename
        state["complete"] = len(state["photos"]) == int(state.get("shot_count",3))
        state["status"] = "capturing"
        state["revision"] += 1
        persist_state()
    return jsonify(ok=True, message="Photo saved.", state=public_state())

@app.delete("/api/photo/<int:slot>")
def api_delete_photo(slot):
    require_local_operator()
    if slot not in (1,2,3): abort(400)
    with lock:
        filename = state["photos"].pop(str(slot), None)
        if not filename:
            return jsonify(ok=False, message="That photo slot is empty."), 404
        (STORE/"original"/filename).unlink(missing_ok=True)
        (STORE/"sessions"/state["session_id"]/filename).unlink(missing_ok=True)
        # A retake invalidates every derived deliverable so stale strips/GIFs are never shared.
        old_final=state.get("final") or {}
        for key in ("jpg", "png", "gif"):
            old_name=Path(str(old_final.get(key, ""))).name
            if old_name:
                (STORE/"final"/old_name).unlink(missing_ok=True)
        old_qr=(STORE/"qr"/f"{state.get('session_id')}_finished_qr.png")
        old_qr.unlink(missing_ok=True)
        state["final"] = None
        state["complete"] = False
        state["revision"] += 1
        persist_state()
    return jsonify(ok=True, message=f"Photo {slot} removed.", state=public_state())

@app.get("/download/photo/<sid>/<filename>")
def download_photo(sid, filename):
    require_local_operator()
    path = STORE/"original"/Path(filename).name
    if not path.is_file() or not Path(filename).name.startswith(sid):
        abort(404)
    return send_file(path, as_attachment=True, download_name=f"YSL-Photobooth-Photo-{filename.split('_photo_')[1].split('_')[0]}.jpg")

@app.get("/photo/<sid>/<filename>")
def get_photo(sid, filename):
    require_local_operator()
    # Legacy ID-only image URLs are operator-local only.
    path = STORE/"sessions"/sid/Path(filename).name
    if not path.is_file():
        path = STORE/"original"/Path(filename).name
    if not path.is_file() or not Path(filename).name.startswith(sid):
        abort(404)
    return send_file(path)

def cover_resize(img, size):
    w,h=size
    return ImageOps.fit(img, (w,h), method=Image.Resampling.LANCZOS, centering=(0.5,0.5))

def rounded_mask(size, radius):
    mask=Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0,0,size[0]-1,size[1]-1), radius=radius, fill=255)
    return mask

def _load_social_icon(name, size=(22, 22)):
    """Load the bundled local raster brand icon; no internet or SVG runtime is needed."""
    icon_path = ROOT / "static" / "assets" / f"{name}.png"
    if not icon_path.is_file():
        return None
    try:
        with Image.open(icon_path) as icon:
            icon = icon.convert("RGBA")
            icon.thumbnail(size, Image.Resampling.LANCZOS)
            return icon.copy()
    except OSError:
        return None


def _brand_gif_frame(frame, logo, facebook_icon=None, instagram_icon=None):
    """Bake a clear YSL logo and the two approved social identities into each GIF frame."""
    canvas = frame.convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")

    # The source logo is portrait-oriented. Give it a portrait-sized box so it
    # does not collapse to a tiny mark when constrained by a landscape box.
    logo_copy = logo.copy().convert("RGBA")
    logo_copy.thumbnail((84, 68), Image.Resampling.LANCZOS)
    canvas.alpha_composite(logo_copy, (10, 9))

    def load_font(size):
        for font_name in ("DejaVuSans.ttf", "arial.ttf", "Arial.ttf"):
            try:
                return ImageFont.truetype(font_name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    title_font = load_font(10)
    detail_font = load_font(8)
    icon_size, text_gap, group_gap = 20, 6, 24

    # Two horizontal groups, each with its own icon and two readable text lines.
    groups = [
        (facebook_icon, ("Facebook", "YSL Valencia")),
        (instagram_icon, ("YSL Valencia", "@ysl.valencia")),
    ]
    measured = []
    for source_icon, lines in groups:
        widths = [draw.textbbox((0, 0), line, font=font)[2] for line, font in zip(lines, (title_font, detail_font))]
        measured.append((source_icon, lines, icon_size + text_gap + max(widths)))
    total_width = sum(item[2] for item in measured) + group_gap
    left = max(8, (canvas.width - total_width) // 2)
    top = canvas.height - icon_size - 10
    for source_icon, lines, group_width in measured:
        icon = source_icon.copy().convert("RGBA") if source_icon else None
        if icon:
            icon.thumbnail((icon_size, icon_size), Image.Resampling.LANCZOS)
            canvas.alpha_composite(icon, (left, top))
        else:
            draw.rounded_rectangle((left, top, left + icon_size, top + icon_size), radius=4,
                                   outline=(255, 255, 255, 240), width=2)
        text_x = left + icon_size + text_gap
        draw.text((text_x, top + 1), lines[0], font=title_font,
                  fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(35, 55, 70, 220))
        draw.text((text_x, top + 11), lines[1], font=detail_font,
                  fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(35, 55, 70, 220))
        left += group_width + group_gap
    return canvas.convert("RGB")

def generate_flashback(photos, sid):
    """Build a 9-second GIF and bake the same YSL branding into every frame."""
    slots = sorted((str(slot) for slot in photos if str(slot) in {"1", "2", "3"}), key=int)
    if not slots:
        raise ValueError("At least one saved photo is required before generating the GIF.")
    for slot in slots:
        filename = Path(str(photos.get(slot, ""))).name
        if not filename or not filename.startswith(sid) or not (STORE / "original" / filename).is_file():
            raise ValueError(f"Photo {slot} is missing. Add or retake it before generating the GIF.")
    frames=[]
    session_dir=STORE/"sessions"/sid
    for slot_text in slots:
        slot = int(slot_text)
        clip_dir=session_dir/f"prep_{slot}"
        clip_files=sorted(clip_dir.glob("frame_*.jpg")) if clip_dir.is_dir() else []
        clip_frames=[]
        for file in clip_files[:30]:
            try:
                with Image.open(file) as source:
                    clip_frames.append(ImageOps.fit(source.convert("RGB"), (640, 360), method=Image.Resampling.LANCZOS))
            except Exception:
                continue
        if clip_frames:
            while len(clip_frames) < 30:
                clip_frames.append(clip_frames[-1].copy())
            frames.extend(clip_frames[:30])
        else:
            filename=photos.get(str(slot))
            if filename:
                with Image.open(STORE/"original"/filename) as source:
                    still=ImageOps.fit(source.convert("RGB"), (640, 360), method=Image.Resampling.LANCZOS)
                frames.extend(still.copy() for _ in range(30))
    if not frames:
        raise ValueError("No captured footage is available for the flashback GIF.")

    logo_path=ROOT/"static"/"assets"/"Logo.png"
    if not logo_path.is_file():
        raise FileNotFoundError("The YSL logo is missing: static/assets/Logo.png")
    with Image.open(logo_path) as logo_source:
        logo=logo_source.convert("RGBA")
    facebook_icon = _load_social_icon("facebook", size=(48, 48))
    instagram_icon = _load_social_icon("instagram", size=(48, 48))
    branded=[_brand_gif_frame(frame, logo, facebook_icon, instagram_icon) for frame in frames]
    # A single shared palette prevents color/palette shifts and flicker between frames.
    palette_source=branded[0].convert("P", palette=Image.Palette.ADAPTIVE, colors=256)
    palette=palette_source.getpalette()
    indexed=[]
    for frame in branded:
        converted=frame.convert("RGB").quantize(palette=palette_source, dither=Image.Dither.FLOYDSTEINBERG)
        indexed.append(converted)
    path=STORE/"final"/f"{sid}_flashback.gif"
    indexed[0].save(path, save_all=True, append_images=indexed[1:], duration=100, loop=0, optimize=False, disposal=2)
    if not path.is_file() or path.stat().st_size < 1000:
        raise IOError("The branded flashback GIF could not be saved completely.")
    return path



def _build_ready_preview(sid, photos, request_host=None):
    """Build the visible photo strip and branded GIF before showing SESSION COMPLETE.

    This is a preview/export preparation only: the session remains in Ready Gallery
    until the operator explicitly chooses Finish & Save.
    """
    slots = {str(slot) for slot in photos if str(slot) in {"1", "2", "3"}}
    if slots != {"1", "2", "3"}:
        raise ValueError("All three photo slots are required to prepare the photo strip.")
    for slot in ("1", "2", "3"):
        filename = Path(str(photos.get(slot, ""))).name
        if not filename or not filename.startswith(sid) or not (STORE / "original" / filename).is_file():
            raise ValueError(f"Photo {slot} is missing. Please retake it before continuing.")
    final, _size = _build_outputs_for_session(sid, photos, request_host=request_host)
    final["ready_preview"] = True
    return final

def _prepare_session_background(sid, photos, request_host):
    try:
        preview = _build_ready_preview(sid, photos, request_host=request_host)
        meta_path = STORE / "sessions" / sid / "session.json"
        saved = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        saved.update(active=False, complete=True, status="ready", final=preview,
                     revision=int(saved.get("revision", 0)) + 1)
        write_session_meta(sid, saved)
        with lock:
            if state.get("session_id") == sid:
                state.update(active=False, complete=True, status="ready", final=preview,
                             revision=state["revision"] + 1)
                # Keep the live state and persisted session manifest in sync.
                persist_state()
        # Upload in the background after the local preview and manifest are durable.
        latest = session_meta(sid) or saved
        if cloud_configured():
            apply_cloud_guest_link(sid, latest)
            with lock:
                if state.get("session_id") == sid:
                    state["final"] = latest.get("final")
                    persist_state()
    except Exception:
        app.logger.exception("Ready Gallery background processing failed")
        meta_path = STORE / "sessions" / sid / "session.json"
        try:
            saved = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
            saved.update(active=False, complete=True, status="ready_error",
                         revision=int(saved.get("revision", 0)) + 1)
            write_session_meta(sid, saved)
        except (OSError, ValueError, TypeError):
            app.logger.exception("Could not persist the photo processing error")
        with lock:
            if state.get("session_id") == sid:
                state.update(active=False, complete=True, status="ready_error",
                             revision=state["revision"] + 1)
                persist_state()
    finally:
        gif_generation_lock.release()


@app.post("/api/session/prepare")
def prepare_session_for_ready_gallery():
    require_local_operator()
    if not gif_generation_lock.acquire(blocking=False):
        return jsonify(ok=False, message="Photo processing is already running. Please wait."), 409
    with lock:
        if not state.get("active") or len(state.get("photos") or {}) != 3:
            gif_generation_lock.release()
            return jsonify(ok=False, message="Capture all 3 photos before opening SESSION COMPLETE."), 409
        sid = state["session_id"]
        photos = dict(state["photos"])
        request_host = request.host
        state.update(active=False, complete=True, status="processing", final=None,
                     revision=state["revision"] + 1)
        persist_state()
    try:
        worker = threading.Thread(target=_prepare_session_background, args=(sid, photos, request_host), daemon=True,
                                  name=f"ysl-export-{sid}")
        worker.start()
    except Exception:
        gif_generation_lock.release()
        with lock:
            state.update(status="ready_error", active=False)
            persist_state()
        app.logger.exception("Could not start photo processing worker")
        return jsonify(ok=False, message="Could not start photo processing."), 500
    return jsonify(ok=True, processing=True, message="SESSION COMPLETE. Preparing your previews in the background.",
                   session_id=sid, final=None, state=public_state())

@app.post("/api/ready-session/<sid>/finish")
def finish_ready_session(sid):
    require_local_operator()
    if not valid_session_id(sid): abort(404)
    if not gif_generation_lock.acquire(blocking=False):
        return jsonify(ok=False, message="Another photo export is being processed. Please wait."), 409
    try:
        folder = STORE / "sessions" / sid
        meta = folder / "session.json"
        if not meta.is_file(): abort(404)
        saved = json.loads(meta.read_text(encoding="utf-8"))
        photos = dict(saved.get("photos") or {})
        if set(photos) != {"1", "2", "3"}:
            return jsonify(ok=False, message="All 3 photo slots must be filled before Finish & Save."), 409
        final, size = _build_outputs_for_session(sid, photos, (saved.get("final") or {}).get("url", ""))
        saved.update(active=False, status="finished", ready=False, finished=True, complete=True,
                     final=final, revision=int(saved.get("revision", 0)) + 1)
        write_session_meta(sid, saved)
        if cloud_configured():
            apply_cloud_guest_link(sid, saved)
            final = saved.get("final") or final
        with lock:
            if state.get("session_id") == sid:
                state.update(active=False, status="finished", complete=True, final=final,
                             revision=state["revision"] + 1)
        return jsonify(ok=True, message="Session saved to Finished Photos.", final=final,
                       width=size[0], height=size[1], session_id=sid)
    except HTTPException:
        raise
    except Exception as exc:
        app.logger.exception("Finish & Save failed")
        return jsonify(ok=False, message=f"Could not save this session: {exc}"), 500
    finally:
        gif_generation_lock.release()

def valid_session_id(sid):
    # Session IDs are YYYYMMDD-HHMMSS- plus six lowercase hexadecimal chars.
    # The previous digit-only check rejected IDs containing a-f, breaking every QR gallery URL.
    return bool(re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", str(sid or "")))

@app.post("/api/strip/generate")
def generate_strip():
    require_local_operator()
    with lock:
        existing = state.get("final") or {}
        if existing.get("gif"):
            gif_file = STORE / "final" / Path(existing["gif"]).name
            if gif_file.is_file():
                return jsonify(ok=True, message="Finished photo already generated.", **existing,
                               width=Image.open(ROOT / "assets" / "PHOTOBOOTH.png").width,
                               height=Image.open(ROOT / "assets" / "PHOTOBOOTH.png").height)
    if not gif_generation_lock.acquire(blocking=False):
        return jsonify(ok=False, message="Your finished photos are still being saved. Please wait."), 409
    try:
        return _generate_strip_once()
    finally:
        gif_generation_lock.release()

def _build_outputs_for_session(sid, photos, existing_url="", request_host=None):
    """Create the strip, branded GIF and QR for a specific saved session."""
    cfg=load_config()
    template=Image.open(ROOT/"assets"/"PHOTOBOOTH.png").convert("RGBA")
    frames=cfg.get("photo_frames", [])
    if not frames:
        raise ValueError("config.json must define at least one photo frame.")
    for index, frame in enumerate(frames[:3], start=1):
        filename=photos.get(str(index))
        if not filename:
            raise ValueError(f"Photo {index} is missing.")
        with Image.open(STORE/"original"/Path(filename).name) as source:
            photo=source.convert("RGB")
        x,y,w,h=[int(frame[k]) for k in ("x","y","width","height")]
        if min(x,y,w,h)<0 or w<1 or h<1 or x+w>template.width or y+h>template.height:
            raise ValueError(f"Photo frame {index} is outside the artwork bounds.")
        template.paste(cover_resize(photo,(w,h)),(x,y),rounded_mask((w,h),int(frame.get("radius",24))))
        right=cfg.get("photo_frames_right", [])
        if len(right)==3:
            rf=right[index-1]
            rx,ry,rw,rh=[int(rf[k]) for k in ("x","y","width","height")]
            if rx>=0 and ry>=0 and rw>0 and rh>0 and rx+rw<=template.width and ry+rh<=template.height:
                template.paste(cover_resize(photo,(rw,rh)),(rx,ry),rounded_mask((rw,rh),int(rf.get("radius",24))))
    # Add the original transparent YSL wordmark to the quiet lower margin of both strips.
    # The supplied artwork has clear whitespace beneath the cross, so no photo or artwork is covered.
    strip_logo_path=ROOT/"static"/"assets"/"Logo.png"
    if strip_logo_path.is_file():
        with Image.open(strip_logo_path) as logo_source:
            strip_logo=logo_source.convert("RGBA")
            strip_logo.thumbnail((110,90),Image.Resampling.LANCZOS)
            logo_y=max(0,template.height-105)
            for center_x in (int(template.width*0.28),int(template.width*0.72)):
                template.alpha_composite(strip_logo,(max(0,center_x-strip_logo.width//2),logo_y))
    out_jpg=STORE/"final"/f"{sid}_strip.jpg"
    out_png=STORE/"final"/f"{sid}_strip.png"
    template.convert("RGB").save(out_jpg,"JPEG",quality=96,dpi=(300,300),optimize=True)
    template.save(out_png,"PNG",dpi=(300,300),optimize=True)
    gif_path=generate_flashback(photos, sid)

    qr_name=f"{sid}_finished_qr.png"
    saved_meta = session_meta(sid) or {}
    access_token = get_or_create_access_token(sid, saved_meta)
    if not access_token and state.get("session_id") == sid:
        access_token = str(state.get("access_token") or "")
    # Always create a LAN-only URL. Do not reuse cloud/public URLs from old configs.
    # In cloud mode, do not expose a QR until the Render service confirms the upload.
    public_url = ("" if cloud_configured() else (f"{local_gallery_url(request_host)}/g/{access_token}" if access_token else ""))
    qr_url = ""
    if public_url:
        qr_path = STORE / "qr" / qr_name
        if not qr_path.is_file():
            qrcode.make(public_url, box_size=8, border=2).save(qr_path)
        qr_url = f"/qr/{qr_name}"
    return {"jpg":f"/final/{out_jpg.name}","png":f"/final/{out_png.name}","gif":f"/final/{gif_path.name}","qr":qr_url,"url":public_url,"filename":out_png.name,"gif_filename":gif_path.name,"session_id":sid}, template.size


def _generate_strip_once():
    with lock:
        if not state["active"] or len(state["photos"]) != int(state.get("shot_count",3)):
            return jsonify(ok=False, message="Capture all selected shots before generating the finished photo."), 409
        photos = dict(state["photos"]); sid=state["session_id"]
    try:
        result, size = _build_outputs_for_session(sid, photos)
    except (OSError, ValueError, KeyError) as exc:
        return jsonify(ok=False, message=f"Could not generate the finished photos: {exc}"), 500
    with lock:
        state["final"]=result; state["revision"]+=1; persist_state()
    return jsonify(ok=True, message="Finished photo generated. Choose Print or Soft Copy.", **result,
                   width=size[0],height=size[1])


@app.post("/api/saved-session/<sid>/photo/<int:slot>")
def replace_saved_session_photo(sid, slot):
    """Replace one photo in an archived session and rebuild only that session's outputs."""
    require_local_operator()
    if not valid_session_id(sid) or slot not in (1,2,3): abort(404)
    if not gif_generation_lock.acquire(blocking=False):
        return jsonify(ok=False, message="Another photo export is being processed. Please wait."), 409
    try:
        with lock:
            if state.get("session_id")==sid and state.get("active"):
                return jsonify(ok=False,message="Finish the active capture before editing this session."),409
        folder=STORE/"sessions"/sid
        meta=folder/"session.json"
        if not meta.is_file(): abort(404)
        try:
            saved=json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError): abort(404)
        photos=dict(saved.get("photos") or {})
        upload=request.files.get("photo")
        if not upload:
            return jsonify(ok=False,message="Choose a replacement photo first."),400
        try:
            with Image.open(upload.stream) as source:
                source.verify()
            upload.stream.seek(0)
            with Image.open(upload.stream) as source:
                replacement=source.convert("RGB")
        except Exception:
            return jsonify(ok=False,message="The selected file is not a valid image."),400
        new_name=f"{sid}_photo_{slot}_{uuid.uuid4().hex[:8]}.jpg"
        new_path=STORE/"original"/new_name
        replacement.save(new_path,"JPEG",quality=95,optimize=True)
        shutil.copy2(new_path,folder/new_name)
        old_name=Path(str(photos.get(str(slot),""))).name
        photos[str(slot)]=new_name
        # Retaking one photo invalidates only its preparation clip; the other two clips remain intact.
        clip_dir=folder/f"prep_{slot}"
        if clip_dir.is_dir(): shutil.rmtree(clip_dir,ignore_errors=True)
        try:
            was_finished = (saved.get("status") == "finished" or bool(saved.get("finished")) or
                            (not saved.get("status") and bool((saved.get("final") or {}).get("jpg"))))
            if was_finished and set(photos) == {"1", "2", "3"}:
                final, _size = _build_outputs_for_session(sid, photos, (saved.get("final") or {}).get("url", ""))
            else:
                for directory in (STORE/"final", STORE/"qr"):
                    for item in directory.glob(f"{sid}_*"):
                        if item.is_file(): item.unlink(missing_ok=True)
                final = _build_ready_preview(sid, photos) if set(photos) == {"1", "2", "3"} else None
        except Exception:
            (STORE/"original"/new_name).unlink(missing_ok=True)
            (folder/new_name).unlink(missing_ok=True)
            raise
        remains_finished = was_finished and set(photos) == {"1", "2", "3"}
        saved.update(photos=photos, final=final, complete=(len(photos)==3),
                     status=("finished" if remains_finished else "ready"), ready=not remains_finished,
                     finished=remains_finished, revision=int(saved.get("revision",0))+1,
                     cloudSync={"status":"pending","attempts":0,"retryAfter":time.time()})
        write_session_meta(sid, saved)
        if cloud_configured():
            threading.Thread(target=apply_cloud_guest_link, args=(sid, saved), daemon=True).start()
        with lock:
            if state.get("session_id")==sid and not state.get("active"):
                state.update(photos=photos,final=final,complete=(len(photos)==3),status=saved["status"],revision=state["revision"]+1)
        if old_name and old_name.startswith(sid) and old_name != new_name:
            (STORE/"original"/old_name).unlink(missing_ok=True)
            (folder/old_name).unlink(missing_ok=True)
        message=(f"Photo {slot} replaced. The GIF was regenerated." if final else f"Photo {slot} replaced. Add the remaining photo(s) to regenerate the GIF.")
        return jsonify(ok=True,message=message,session_id=sid,final=final)
    except HTTPException:
        raise
    except Exception as exc:
        app.logger.exception("Saved-session photo replacement failed")
        return jsonify(ok=False,message=f"Could not update this photo: {exc}"),500
    finally:
        gif_generation_lock.release()


@app.delete("/api/saved-session/<sid>/photo/<int:slot>")
def delete_saved_session_photo(sid, slot):
    """Remove one image from a saved session without touching its other two photos."""
    require_local_operator()
    if not valid_session_id(sid) or slot not in (1,2,3): abort(404)
    if not gif_generation_lock.acquire(blocking=False):
        return jsonify(ok=False,message="A photo export is currently being updated. Please wait."),409
    try:
        with lock:
            if state.get("session_id")==sid and state.get("active"):
                return jsonify(ok=False,message="Finish the active capture before deleting a saved photo."),409
        folder=STORE/"sessions"/sid
        meta=folder/"session.json"
        if not meta.is_file(): abort(404)
        try:
            saved=json.loads(meta.read_text(encoding="utf-8"))
        except (OSError,ValueError): abort(404)
        photos=dict(saved.get("photos") or {})
        old_name=Path(str(photos.get(str(slot),""))).name
        if not old_name:
            return jsonify(ok=False,message=f"Photo {slot} is already empty."),404
        photos.pop(str(slot),None)
        if old_name.startswith(sid):
            (STORE/"original"/old_name).unlink(missing_ok=True)
            (folder/old_name).unlink(missing_ok=True)
        clip_dir=folder/f"prep_{slot}"
        if clip_dir.is_dir(): shutil.rmtree(clip_dir,ignore_errors=True)
        for directory in (STORE/"final",STORE/"qr"):
            for item in directory.glob(f"{sid}_*"):
                if item.is_file(): item.unlink(missing_ok=True)
        was_finished = (saved.get("status") == "finished" or bool(saved.get("finished")) or
                            (not saved.get("status") and bool((saved.get("final") or {}).get("jpg"))))
        preview = _build_ready_preview(sid, photos) if set(photos) == {"1", "2", "3"} else None
        if was_finished and set(photos) == {"1", "2", "3"}:
            preview, _size = _build_outputs_for_session(sid, photos, (saved.get("final") or {}).get("url", ""))
        # A finished session stays in Finished Photos when one slot is deleted;
        # the missing slot can be retaken there, and exports rebuild when all 3 return.
        remains_finished = was_finished
        saved.update(photos=photos, final=preview, complete=(len(photos)==3),
                     status=("finished" if remains_finished else "ready"), ready=not remains_finished,
                     finished=remains_finished, revision=int(saved.get("revision",0))+1,
                     cloudSync={"status":"pending","attempts":0,"retryAfter":time.time()})
        write_session_meta(sid, saved)
        if cloud_configured():
            threading.Thread(target=apply_cloud_guest_link, args=(sid, saved), daemon=True).start()
        with lock:
            if state.get("session_id")==sid and not state.get("active"):
                state.update(photos=photos,final=preview,complete=(len(photos)==3),status=saved["status"],revision=state["revision"]+1)
        message = (f"Photo {slot} deleted. The preview was refreshed." if preview else
                   f"Photo {slot} deleted. Retake the empty slot to regenerate the GIF and photo strip.")
        return jsonify(ok=True,message=message,session_id=sid,photo_count=len(photos),final=preview)
    finally:
        gif_generation_lock.release()


@app.delete("/api/saved-session/<sid>")
def delete_saved_session(sid):
    """Delete exactly one archived session and its generated files."""
    require_local_operator()
    if not valid_session_id(sid): abort(404)
    if not gif_generation_lock.acquire(blocking=False):
        return jsonify(ok=False,message="A photo export is currently being updated. Please wait."),409
    try:
        with lock:
            if state.get("session_id")==sid and state.get("active"):
                return jsonify(ok=False,message="End the active session before deleting it."),409
        folder=STORE/"sessions"/sid
        meta=folder/"session.json"
        if not meta.is_file(): abort(404)
        try:
            saved=json.loads(meta.read_text(encoding="utf-8"))
        except (OSError,ValueError): abort(404)
        for name in (saved.get("photos") or {}).values():
            safe=Path(str(name)).name
            if safe.startswith(sid): (STORE/"original"/safe).unlink(missing_ok=True)
        shutil.rmtree(folder,ignore_errors=True)
        if cloud_configured():
            threading.Thread(target=delete_cloud_session, args=(sid,), daemon=True).start()
        for directory in (STORE/"final",STORE/"qr"):
            for item in directory.glob(f"{sid}_*"):
                if item.is_file(): item.unlink(missing_ok=True)
        with lock:
            if state.get("session_id")==sid:
                state.update(active=False,session_id=None,started_at=None,photos={},complete=False,final=None,
                             status="idle",shot_count=3,access_token=None,access_token_expires_at=None,phone_upload_token=None,revision=state["revision"]+1)
        return jsonify(ok=True,message="The selected photo session was deleted.",session_id=sid)
    finally:
        gif_generation_lock.release()


@app.get("/finished/<filename>")
def finished_file(filename):
    require_local_operator()
    path=STORE/"final"/Path(filename).name
    if not path.is_file(): abort(404)
    return send_file(path, as_attachment=False)

@app.post("/api/saved-session/<sid>/qr")
def regenerate_saved_session_qr(sid):
    """Regenerate the QR for an existing session without recapturing or changing its photos."""
    require_local_operator()
    if not valid_session_id(sid):
        abort(404)
    meta_path = STORE / "sessions" / sid / "session.json"
    if not meta_path.is_file():
        abort(404)
    try:
        saved = json.loads(meta_path.read_text(encoding="utf-8"))
        photos = saved.get("photos") or {}
        final = saved.get("final") or {}
        if len(photos) != 3 or not final.get("jpg") or not final.get("gif"):
            return jsonify(ok=False, message="This session must have all three photos, its photo strip, and GIF before a gallery QR can be generated."), 409
        token = get_or_create_access_token(sid, saved)
        if cloud_configured():
            apply_cloud_guest_link(sid, saved)
            gallery_url = (saved.get("final") or {}).get("url", "")
            if not gallery_url:
                return jsonify(ok=False, message="Cloud upload is pending. Check internet and try again."), 503
        else:
            gallery_url = f"{local_gallery_url(request.host)}/g/{token}"
        qr_name = f"{sid}_finished_qr.png"
        qr_path = STORE / "qr" / qr_name
        # QR generation is local and cached by session; overwrite only this session's QR.
        qrcode.make(gallery_url, box_size=8, border=2).save(qr_path)
        saved["final"] = {**final, "qr": f"/qr/{qr_name}", "url": gallery_url}
        write_session_meta(sid, saved)
        return jsonify(ok=True, qr=f"/qr/{qr_name}", url=gallery_url, session_id=sid)
    except (OSError, ValueError, TypeError) as exc:
        app.logger.exception("Could not regenerate session QR")
        return jsonify(ok=False, message=f"Could not generate this session QR: {exc}"), 500


@app.get("/api/cloud/session/<sid>")
def cloud_session_lookup(sid):
    """Operator-only lookup; a session ID is never guest authorization."""
    require_local_operator()
    if not valid_session_id(sid): abort(404)
    if not cloud_configured():
        return jsonify(ok=False, message="Cloud service is not configured."), 503
    try:
        response = requests.get(cloud_public_url() + f"/internal/sessions/{sid}",
            headers={"Authorization": "Bearer " + os.getenv("CLOUD_SYNC_SECRET", "")}, timeout=20)
        if response.status_code == 404: return jsonify(ok=False, message="Session not found in cloud storage."), 404
        response.raise_for_status()
        data = response.json()
        return jsonify(ok=True, sessionId=data.get("sessionId"), status=data.get("status"),
            createdAt=data.get("createdAt"), updatedAt=data.get("updatedAt"),
            photoCount=len(data.get("photos") or {}), hasStrip=bool(data.get("stripPath")), hasGif=bool(data.get("gifPath")))
    except requests.RequestException as exc:
        return jsonify(ok=False, message=f"Cloud lookup failed: {exc}"), 502


@app.get("/api/cloud/session/<sid>/strip")
def cloud_session_strip(sid):
    require_local_operator()
    if not valid_session_id(sid) or not cloud_configured(): abort(404)
    try:
        response = requests.get(cloud_public_url() + f"/internal/sessions/{sid}/file/strip",
            headers={"Authorization": "Bearer " + os.getenv("CLOUD_SYNC_SECRET", "")}, timeout=(10, 120))
        if response.status_code == 404: abort(404)
        response.raise_for_status()
        mime = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
        return send_file(io.BytesIO(response.content), mimetype=mime, max_age=0)
    except requests.RequestException as exc:
        app.logger.warning("Cloud strip preview failed for %s: %s", sid, exc)
        abort(502)


@app.get("/ready-gallery")
def ready_gallery():
    require_local_operator()
    selected_session = request.args.get("session", "")
    sessions = []
    for folder in (STORE / "sessions").iterdir():
        meta = folder / "session.json"
        if not folder.is_dir() or not meta.is_file():
            continue
        try:
            saved = json.loads(meta.read_text(encoding="utf-8"))
            photos = saved.get("photos") or {}
            status = saved.get("status") or ("finished" if (saved.get("final") or {}).get("jpg") else "ready")
            if status == "finished":
                continue
            final = saved.get("final") or {}
            strip_name = Path(str(final.get("png") or final.get("jpg") or "")).name
            strip_path = STORE / "final" / strip_name
            gif_name = Path(str(final.get("gif", ""))).name
            gif_path = STORE / "final" / gif_name
            first_value = photos.get("1") or (next(iter(photos.values())) if photos else "")
            first_name = Path(str(first_value)).name if first_value else ""
            preview_url = f"/finished/{strip_name}?v={saved.get('revision', 0)}" if strip_name and strip_path.is_file() else (f"/photo/{folder.name}/{first_name}" if first_name else "/static/assets/Logo.png")
            sessions.append({"session_id": folder.name, "captured_at": saved.get("started_at") or folder.name,
                "preview": preview_url,
                "gif_download": f"/final/{gif_name}" if gif_name and gif_path.is_file() else "",
                "gallery": f"/ready/{folder.name}", "photo_count": len(photos),
                "selected": folder.name == selected_session, "complete": len(photos) == 3})
        except (OSError, ValueError, TypeError):
            continue
    sessions.sort(key=lambda item: item["captured_at"], reverse=True)
    return render_template("ready_gallery.html", sessions=sessions, selected_session=selected_session)

@app.get("/finished-photos")
def finished_photos():
    require_local_operator()
    selected_session = request.args.get("session", "")
    sessions = []
    for folder in (STORE / "sessions").iterdir():
        meta = folder / "session.json"
        if not folder.is_dir() or not meta.is_file():
            continue
        try:
            saved = json.loads(meta.read_text(encoding="utf-8"))
            final = saved.get("final") or {}
            photos = saved.get("photos") or {}
            status = saved.get("status") or ("finished" if final.get("jpg") else "ready")
            if status != "finished":
                continue
            strip_name = Path(str(final.get("png") or final.get("jpg") or "")).name
            strip_path = STORE / "final" / strip_name
            gif_name = Path(str(final.get("gif", ""))).name
            gif_path = STORE / "final" / gif_name
            first_value = photos.get("1") or (next(iter(photos.values())) if photos else "")
            first_name = Path(str(first_value)).name if first_value else ""
            preview_url = f"/finished/{strip_name}?v={saved.get('revision', 0)}" if strip_name and strip_path.is_file() else (f"/photo/{folder.name}/{first_name}" if first_name else "/static/assets/Logo.png")
            sessions.append({"session_id": folder.name, "captured_at": saved.get("started_at") or folder.name,
                "gif": preview_url, "gif_download": f"/final/{gif_name}" if gif_name and gif_path.is_file() else "",
                "gallery": f"/session/{folder.name}", "photo_count": len(photos),
                "selected": folder.name == selected_session, "complete": bool(final.get("jpg") and gif_path.is_file())})
        except (OSError, ValueError, TypeError):
            continue
    sessions.sort(key=lambda item: item["captured_at"], reverse=True)
    return render_template("finished_photos.html", sessions=sessions, selected_session=selected_session)

@app.get("/ready/<sid>")
def ready_session_gallery(sid):
    require_local_operator()
    return _render_session_gallery(sid, ready=True)

@app.get("/session/<sid>")
def session_gallery(sid):
    require_local_operator()
    return _render_session_gallery(sid, ready=False)

def _render_session_gallery(sid, ready=False):
    if not valid_session_id(sid): abort(404)
    folder=STORE/"sessions"/sid
    meta=folder/"session.json"
    if not meta.is_file(): abort(404)
    try:
        saved=json.loads(meta.read_text(encoding="utf-8"))
        photos=saved.get("photos", {})
        final=saved.get("final") or {}
        if ready and (saved.get("status") == "finished" or (not saved.get("status") and bool((saved.get("final") or {}).get("jpg")))):
            return redirect(f"/session/{sid}")
        # Both Ready and Finished operator views use the editable local session template.
        # guest_session.html is reserved for token-protected online guest galleries.
        return render_template("session_gallery.html", sid=sid, photos=photos, final=final,
                               ready=ready, status=saved.get("status", "ready"), revision=saved.get("revision", 0))
    except (ValueError, TypeError): abort(404)

@app.get("/download/session/<sid>.zip")
def download_session_zip(sid):
    require_local_operator()
    if not valid_session_id(sid): abort(404)
    meta=STORE/"sessions"/sid/"session.json"
    if not meta.is_file(): abort(404)
    try:
        saved=json.loads(meta.read_text(encoding="utf-8")); photos=saved.get("photos",{}); final=saved.get("final") or {}
        if len(photos)!=3: abort(404)
        memory=io.BytesIO()
        with zipfile.ZipFile(memory,"w",zipfile.ZIP_DEFLATED) as archive:
            for n in (1,2,3):
                name=photos.get(str(n)); safe_name=Path(str(name)).name; path=STORE/"sessions"/sid/safe_name
                if not path.is_file(): path=STORE/"original"/safe_name
                if not path.is_file() or not safe_name.startswith(sid): abort(404)
                archive.write(path, f"YSL-Photobooth-{sid}/Photo-{n}.jpg")
        memory.seek(0)
        return send_file(memory, mimetype="application/zip", as_attachment=True, download_name=f"YSL-Photobooth-{sid}.zip")
    except (ValueError, TypeError, KeyError): abort(404)

@app.get("/final/<filename>")
def final_file(filename):
    require_local_operator()
    path=STORE/"final"/Path(filename).name
    if not path.is_file(): abort(404)
    return send_file(path, as_attachment=True)

@app.post("/api/phone/qr")
def phone_qr():
    require_local_operator()
    with lock:
        token = state.get("phone_upload_token") if state.get("active") else None
    if not token:
        return jsonify(ok=False, message="Start a photo session before creating a phone upload QR code."), 409
    url=f"http://{current_ip()}:{load_config().get('port',5000)}/phone/{token}"
    img=qrcode.make(url)
    name=f"phone-{uuid.uuid4().hex[:8]}.png"
    path=STORE/"qr"/name
    img.save(path)
    return jsonify(ok=True,url=url,qr=f"/qr/{name}")
@app.get("/qr/<filename>")
def qr_file(filename):
    require_local_operator()
    path=STORE/"qr"/Path(filename).name
    if not path.is_file(): abort(404)
    return send_file(path)

@app.post("/api/phone/upload")
def phone_upload():
    token = request.form.get("token", "")
    with lock:
        if not state["active"] or not state.get("phone_upload_token") or not hmac.compare_digest(str(state["phone_upload_token"]), token):
            return jsonify(ok=False,message="This phone upload link is invalid or expired."),403
        file=request.files.get("photo")
        if not file: return jsonify(ok=False,message="Choose a photo first."),400
        sid=state["session_id"]
        try:
            img=Image.open(file.stream); img.verify()
            file.stream.seek(0); img=Image.open(file.stream).convert("RGB")
        except Exception: return jsonify(ok=False,message="Please upload a valid image."),400
        slot=next((n for n in range(1,int(state.get("shot_count",3))+1) if str(n) not in state["photos"]),None)
        if slot is None: return jsonify(ok=False,message="All photo slots are already filled."),409
        filename=f"{sid}_photo_{slot}_{uuid.uuid4().hex[:8]}.jpg"
        img.save(STORE/"original"/filename,"JPEG",quality=95)
        shutil.copy2(STORE/"original"/filename, STORE/"sessions"/sid/filename)
        state["photos"][str(slot)]=filename
        state["complete"]=len(state["photos"])==int(state.get("shot_count",3))
        state["revision"]+=1; persist_state()
    return jsonify(ok=True,message=f"Photo added to slot {slot}.",slot=slot)

@app.post("/api/live-frame")
def receive_live_camera_frame():
    """Accept the operator browser's latest preview frame for local guest displays."""
    require_local_camera_relay()
    if not request.mimetype or request.mimetype.lower() != "image/jpeg":
        abort(415)
    frame = request.get_data(cache=False)
    if not frame or len(frame) > 1_500_000:
        abort(413 if len(frame) > 1_500_000 else 400)
    # Verify that the payload is a JPEG before publishing it to the guest screens.
    if not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9"):
        abort(415)
    with live_frame_condition:
        live_camera_frame["data"] = frame
        live_camera_frame["updated"] = time.time()
        live_camera_frame["sequence"] += 1
        live_frame_condition.notify_all()
    return ("", 204)


@app.get("/api/live-frame-status")
def live_camera_frame_status():
    """Tiny health endpoint; guests poll metadata, never JPEG payloads."""
    require_local_camera_relay()
    with live_frame_lock:
        updated = live_camera_frame["updated"]
        sequence = live_camera_frame["sequence"]
    fresh = bool(updated and time.time() - updated <= 2.5)
    return jsonify(ok=True, fresh=fresh, sequence=sequence, updated=updated)


@app.get("/api/live-stream.mjpg")
def live_camera_mjpeg_stream():
    """Push only the newest JPEG to the browser's native MJPEG decoder."""
    require_local_camera_relay()

    def generate():
        last_sequence = -1
        while True:
            with live_frame_condition:
                live_frame_condition.wait_for(
                    lambda: live_camera_frame["sequence"] != last_sequence,
                    timeout=1.5,
                )
                frame = live_camera_frame["data"]
                updated = live_camera_frame["updated"]
                sequence = live_camera_frame["sequence"]
            if sequence == last_sequence:
                # Wait for the next genuinely new frame; never replay a stale one.
                continue
            last_sequence = sequence
            if not frame or time.time() - updated > 2.5:
                continue
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                + f"Content-Length: {len(frame)}\r\n".encode("ascii")
                + b"\r\n" + frame + b"\r\n"
            )

    response = app.response_class(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        direct_passthrough=True,
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


@app.get("/api/live-frame.jpg")
def get_live_camera_frame():
    """Return the most recent operator preview frame to local guest displays."""
    require_local_camera_relay()
    with live_frame_lock:
        frame = live_camera_frame["data"]
        updated = live_camera_frame["updated"]
    if not frame or time.time() - updated > 3.0:
        abort(503)
    response = make_response(frame)
    response.headers["Content-Type"] = "image/jpeg"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Camera-Frame-Time"] = str(updated)
    return response


@app.get("/api/cameras")
def cameras():
    require_local_operator()
    return jsonify(selected=selected_camera["device_id"], note="Camera enumeration is performed in the browser after permission is granted.")

@app.post("/api/camera")
def set_camera():
    require_local_operator()
    data=request.get_json(silent=True) or {}
    selected_camera["device_id"]=str(data.get("device_id",""))
    return jsonify(ok=True,device_id=selected_camera["device_id"])

@app.get("/api/config")
def get_config():
    require_local_operator()
    return jsonify(load_config())

@app.post("/api/config")
def update_config():
    require_local_operator()
    data=request.get_json(silent=True) or {}
    with lock:
        cfg=load_config()
        for key in ("countdown_seconds","guest_display_index","photo_frames","photo_frames_right","print","photo_count"):
            if key in data: cfg[key]=data[key]
        if "photo_count" in data and not state["photos"]:
            state["shot_count"]=3
        save_config(cfg)
    return jsonify(ok=True,message="Settings saved.")

@app.post("/api/launch-guest")
def launch_guest():
    require_local_operator()
    url=f"http://127.0.0.1:{load_config().get('port',5000)}/guest"
    try:
        import screeninfo
        monitors=screeninfo.get_monitors()
        cfg=load_config(); idx=int(cfg.get("guest_display_index",1))
        monitor=monitors[idx] if len(monitors)>idx else monitors[0]
        x,y=monitor.x,monitor.y
    except Exception:
        x,y=0,0
    candidates=["msedge","chrome","google-chrome"]
    launched=False
    for exe in candidates:
        try:
            subprocess.Popen([exe, f"--app={url}", "--start-fullscreen", f"--window-position={x},{y}", "--kiosk"])
            launched=True; break
        except Exception: pass
    if not launched:
        try:
            import webbrowser; webbrowser.open(url); launched=True
        except Exception: pass
    return jsonify(ok=launched,message="Guest screen launch requested." if launched else "Open /guest in a second browser window and press F11.")

@app.post("/api/print")
def print_strip():
    require_local_operator()
    data=request.get_json(silent=True) or {}
    filename=Path(str(data.get("filename",""))).name
    path=STORE/"final"/filename
    if not path.is_file(): return jsonify(ok=False,message="Generate a strip first."),404
    try:
        Image.open(path).convert("RGB").save(STORE/"final"/"print-job.jpg",quality=96)
        os.startfile(str(STORE/"final"/"print-job.jpg"),"print")
        return jsonify(ok=True,message="Print request sent to the Windows default printer.")
    except Exception as e:
        return jsonify(ok=False,message=f"Printing is unavailable on this system: {e}"),500

@app.get("/api/health")
def health():
    return jsonify(ok=True,offline=True,cloud_configured=cloud_configured(),python=os.sys.version.split()[0])

if __name__ == "__main__":
    cfg=load_config()
    app.run(host=os.getenv("HOST", cfg.get("host","0.0.0.0")),port=int(os.getenv("PORT", cfg.get("port",5000))),debug=False,threaded=True)
