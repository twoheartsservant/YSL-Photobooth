const $ = id => document.getElementById(id);
const guestParams = new URLSearchParams(location.search);
const previewMode = guestParams.get("preview") === "1";
const secondaryMode = guestParams.get("secondary") === "1";
// Identify guest display modes for the fullscreen camera layout.
document.body.classList.add("camera-fullscreen-mode");
if (previewMode) document.body.classList.add("camera-preview-mode");
if (secondaryMode) document.body.classList.add("camera-secondary-mode");
let stream = false, busy = false, cameraStarting = false, preparingSession = "", cameraState = "INITIALIZING";
let frameHealthTimer = null, frameHealthBusy = false, frameStreamRetryTimer = null, lastFrameReceivedAt = 0, lastStreamSequence = -1;
const frameWaiters = new Set();
let lastSession = "", sequenceSession = "", currentState = null, finalRenderedSession = "", lastHistoryRevision = -1;
const api = async (url, opts = {}) => {
  const response = await fetch(url, opts);
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw new Error(data.message || `Request failed (${response.status})`);
  return data;
};
const payload = value => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) });
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
function noteCamera(message, error = false) {
  const node = $("guestCameraStatus");
  if (node) { node.textContent = message; node.classList.toggle("error", error); }
}
function setCameraState(state, message, error = false) {
  cameraState = state;
  const stage = $("cameraStage");
  if (stage) stage.dataset.cameraState = state;
  if (message) noteCamera(message, error);
  document.body.dataset.cameraState = state;
}
function frameReady(image = $("guestCameraFrame")) {
  return !!(image && image.naturalWidth > 0 && image.naturalHeight > 0 && Date.now() - lastFrameReceivedAt < 1800);
}
function notifyFrameReady() {
  stream = true;
  if (!["PREPARING", "COUNTDOWN", "CAPTURING", "PAUSED"].includes(cameraState)) {
    setCameraState("READY", "● READY · LIVE CAMERA CONNECTED");
    const welcomeMessage = $("guestWelcomeMessage");
    if (welcomeMessage && !$("guestStart")?.disabled) {
      welcomeMessage.textContent = "Position yourself in front of the camera. Press START when you’re ready!";
    }
  }
  for (const resolve of [...frameWaiters]) { frameWaiters.delete(resolve); resolve(true); }
}
function waitForLiveFrame(timeoutMs = 15000) {
  const image = $("guestCameraFrame");
  if (frameReady(image)) { notifyFrameReady(); return Promise.resolve(true); }
  return new Promise((resolve, reject) => {
    const finish = value => { clearTimeout(timer); frameWaiters.delete(onReady); value ? resolve(true) : reject(new Error("No live camera frames received. Check that the operator camera is connected.")); };
    const onReady = () => finish(true);
    const timer = setTimeout(() => finish(false), timeoutMs);
    frameWaiters.add(onReady);
  });
}
async function checkLiveStreamHealth() {
  if (frameHealthBusy) return;
  frameHealthBusy = true;
  try {
    const response = await fetch("/api/live-frame-status", { cache: "no-store", headers: { "Cache-Control": "no-cache" } });
    if (!response.ok) throw new Error(`Preview status HTTP ${response.status}`);
    const status = await response.json();
    if (status.fresh && Number(status.sequence) !== lastStreamSequence) {
      lastStreamSequence = Number(status.sequence);
      lastFrameReceivedAt = Date.now();
      if ($("guestCameraFrame")?.naturalWidth) notifyFrameReady();
    } else if (!status.fresh && Date.now() - lastFrameReceivedAt > 1800) {
      stream = false;
      setCameraState("INITIALIZING", "WAITING FOR OPERATOR CAMERA…");
    }
  } catch (error) {
    if (Date.now() - lastFrameReceivedAt > 1800) {
      stream = false;
      setCameraState("ERROR", "RECONNECTING TO CAMERA…", true);
    }
  } finally { frameHealthBusy = false; }
}
function connectLiveMjpeg() {
  const image = $("guestCameraFrame");
  if (!image || image.dataset.streamConnected === "1") return;
  image.dataset.streamConnected = "1";
  image.onload = () => {
    if (image.naturalWidth > 0) {
      lastFrameReceivedAt = Date.now();
      applyMirrorSetting();
      notifyFrameReady();
    }
  };
  image.onerror = () => {
    image.dataset.streamConnected = "0";
    stream = false;
    setCameraState("ERROR", "CAMERA FEED INTERRUPTED · RECONNECTING…", true);
    if (!frameStreamRetryTimer) frameStreamRetryTimer = setTimeout(() => {
      frameStreamRetryTimer = null;
      connectLiveMjpeg();
    }, 900);
  };
  image.src = `/api/live-stream.mjpg?client=${secondaryMode ? "secondary" : "guest"}&retry=${Date.now()}`;
}
function startFramePolling() {
  // Kept under the legacy function name so the rest of the session workflow
  // remains unchanged. MJPEG streams frames continuously; only tiny status
  // metadata is polled to detect a disconnected/stale operator source.
  connectLiveMjpeg();
  if (!frameHealthTimer) {
    checkLiveStreamHealth();
    frameHealthTimer = setInterval(checkLiveStreamHealth, 750);
  }
}
function stopCameraStream() {
  if (frameHealthTimer) { clearInterval(frameHealthTimer); frameHealthTimer = null; }
  if (frameStreamRetryTimer) { clearTimeout(frameStreamRetryTimer); frameStreamRetryTimer = null; }
  stream = false;
  setCameraState("PAUSED", "CAMERA PREVIEW PAUSED");
  lastFrameReceivedAt = 0;
  lastStreamSequence = -1;
  const image = $("guestCameraFrame");
  if (image) {
    image.onload = null; image.onerror = null;
    image.dataset.streamConnected = "0";
    image.removeAttribute("src");
  }
  noteCamera("CAMERA PREVIEW DISCONNECTED");
}
function mirrorEnabled() { return $("mirrorCamera")?.checked !== false; }
function applyMirrorSetting() {
  const image = $("guestCameraFrame");
  if (image) image.style.transform = mirrorEnabled() ? "scaleX(-1)" : "none";
}
function drawCameraFrame(context, image, width, height, mirror = mirrorEnabled()) {
  context.save();
  if (mirror) { context.translate(width, 0); context.scale(-1, 1); }
  context.drawImage(image, 0, 0, width, height);
  context.restore();
}
function show(id) {
  ["welcome", "captureScreen", "completeScreen"].forEach(name => $(name)?.classList.toggle("hidden", name !== id));
  $("guestHistoryPanel")?.classList.toggle("hidden", id === "completeScreen");
  $("yslCredits")?.classList.toggle("hidden", id === "completeScreen");
}
async function startCamera(force = false) {
  if (cameraStarting) return false;
  setCameraState("INITIALIZING", "INITIALIZING CAMERA PREVIEW…");
  if (force) { stream = false; }
  cameraStarting = true;
  try {
    if (frameReady()) { notifyFrameReady(); return true; }
    setCameraState("INITIALIZING", "WAITING FOR OPERATOR CAMERA…");
    startFramePolling();
    return await waitForLiveFrame(20000);
  } catch (error) {
    setCameraState("ERROR", "CAMERA PREVIEW NOT RECEIVED · " + error.message, true);
    if ($("guestWelcomeMessage")) $("guestWelcomeMessage").textContent = error.message;
    throw error;
  } finally { cameraStarting = false; }
}
function renderHistory(state) {
  const host = $("historySlots");
  if (!host || state.revision === lastHistoryRevision) return;
  lastHistoryRevision = state.revision;
  host.innerHTML = "";
  for (let number = 1; number <= 3; number++) {
    const photo = state.photos?.find(item => item.slot === number);
    const slot = document.createElement("div"); slot.className = "history-slot" + (photo ? " filled" : "");
    const label = document.createElement("b"); label.textContent = String(number).padStart(2, "0"); slot.append(label);
    if (photo) {
      const image = document.createElement("img"); image.src = photo.url + "?v=" + encodeURIComponent(state.revision || 0); image.alt = `Photo ${number}`; slot.append(image);
    } else {
      const icon = document.createElement("span"); icon.className = "history-icon"; icon.textContent = "＋"; slot.append(icon);
      const text = document.createElement("small"); text.textContent = `PHOTO ${number}`; slot.append(text);
    }
    host.append(slot);
  }
  $("historyCount").textContent = `${state.photo_count || 0} OF 3 CAPTURED`;
  $("progressText").textContent = `${state.photo_count || 0} / 3 photos captured`;
  $("photoCounter").textContent = `PHOTO ${Math.min((state.photo_count || 0) + 1, 3)} OF 3`;
  $("steps").innerHTML = "";
  for (let number = 1; number <= 3; number++) {
    const dot = document.createElement("i"); dot.className = "step" + (number <= (state.photo_count || 0) ? " done" : ""); $("steps").append(dot);
  }
}
function flash() {
  const node = $("captureFlash"); if (!node) return;
  node.classList.remove("fire"); void node.offsetWidth; node.classList.add("fire");
  setTimeout(() => node.classList.remove("fire"), 420);
}
function encodePreparationFrame(canvas) {
  // toBlob performs JPEG encoding asynchronously instead of blocking the
  // Guest page's main thread while its live MJPEG image is being decoded.
  return new Promise(resolve => {
    canvas.toBlob(blob => {
      if (!blob) return resolve("");
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || "").split(",")[1] || "");
      reader.onerror = () => resolve("");
      reader.readAsDataURL(blob);
    }, "image/jpeg", .5);
  });
}
async function countdownFor(slot) {
  setCameraState("COUNTDOWN", "COUNTDOWN · CAMERA LIVE");
  const countdown = $("countdown"), video = $("guestCameraFrame"), frames = [], mirror = mirrorEnabled();
  const canvas = document.createElement("canvas"); canvas.width = 640; canvas.height = 360;
  const context = canvas.getContext("2d", { alpha: false, desynchronized: true });
  if (mirrorControl) mirrorControl.disabled = true;
  $("guestInstruction").textContent = `Get ready · Photo ${slot} of 3`;
  $("photoCounter").textContent = `PHOTO ${slot} OF 3`;
  try {
    // A five-second countdown gives guests more time to pose. Three samples
    // per second are sufficient for the preparation clip; asynchronous JPEG
    // encoding avoids stalling the live camera preview. Final photo capture
    // below still uses the full available preview dimensions and high quality.
    for (let number = 5; number >= 1; number--) {
      countdown.textContent = number; countdown.classList.remove("hidden", "pop"); void countdown.offsetWidth; countdown.classList.add("pop");
      for (let frame = 0; frame < 3; frame++) {
        if (frameReady(video)) {
          drawCameraFrame(context, video, canvas.width, canvas.height, mirror);
          const encoded = await encodePreparationFrame(canvas);
          if (encoded) frames.push(encoded);
        }
        await wait(333);
      }
    }
    countdown.classList.add("hidden");
    setCameraState("READY", "LIVE CAMERA CONNECTED");
    if (frames.length >= 12) await api("/api/prep-frames", payload({ slot, frames, mirrored: mirror }));
    return mirror;
  } finally { if (mirrorControl) mirrorControl.disabled = false; }
}
async function captureCurrent(slot, sessionId, mirror = mirrorEnabled()) {
  setCameraState("CAPTURING", "CAPTURING PHOTO · CAMERA LIVE");
  const video = $("guestCameraFrame");
  if (!frameReady(video)) await waitForLiveFrame(7000);
  if (!stream || !frameReady(video)) throw new Error("Camera preview is not connected.");
  const canvas = document.createElement("canvas"); canvas.width = video.naturalWidth || 1280; canvas.height = video.naturalHeight || 720;
  drawCameraFrame(canvas.getContext("2d"), video, canvas.width, canvas.height, mirror);
  const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", .95));
  if (!blob) throw new Error("Could not create the photo image.");
  flash();
  const form = new FormData(); form.append("photo", blob, `YSL-${sessionId}-Photo-${slot}.jpg`); form.append("slot", String(slot));
  const saved = await api("/api/photo", { method: "POST", body: form });
  // The server response is returned only after the image has been validated,
  // written to disk, and added to the session manifest.
  currentState = saved.state || await api("/api/state");
  renderHistory(currentState);
  setCameraState("READY", "LIVE CAMERA CONNECTED");
}
function renderSessionComplete(sessionId, result) {
  const photos = currentState?.photos || [];
  const final = result?.final || currentState?.final || {};
  const photoHost = $("resultPhotos");
  if (photoHost) {
    photoHost.replaceChildren();
    for (let slot = 1; slot <= 3; slot++) {
      const photo = photos.find(item => Number(item.slot) === slot);
      const image = document.createElement("img");
      image.alt = `YSL Valencia photo ${slot}`;
      image.src = photo ? `${photo.url}?v=${encodeURIComponent(currentState?.revision || 0)}` : "";
      if (!photo) image.classList.add("hidden");
      photoHost.append(image);
    }
  }
  const token = (final.url || "").match(/\/g\/([A-Za-z0-9_-]{40,100})/)?.[1] || "";
  if (final.gif_filename) finalRenderedSession = sessionId;
  const privateBase = token ? `/g/${token}` : "";
  const gif = $("flashbackGif");
  if (gif) {
    if (final.gif_filename) { gif.src = privateBase ? `${privateBase}/view/gif?v=${encodeURIComponent(currentState?.revision || Date.now())}` : `/finished/${encodeURIComponent(final.gif_filename)}?v=${Date.now()}`; gif.classList.remove("hidden"); }
    else { gif.removeAttribute("src"); gif.classList.add("hidden"); }
  }
  // The PNG is the full-resolution strip export. Older saved sessions may only
  // expose `png` or `jpg`, while current sessions expose `filename` as well.
  // Resolve all supported fields so the strip never disappears just because
  // one optional response key is absent.
  const strip = $("guestFinalPreview");
  const stripPanel = strip?.closest(".strip-preview");
  const stripName = final.filename || (final.png ? final.png.split("/").pop() : "") ||
    (final.jpg ? final.jpg.split("/").pop() : "");
  if (stripPanel) stripPanel.classList.toggle("hidden", !stripName);
  if (strip && stripName) {
    const stripUrl = privateBase ? `${privateBase}/view/strip-png?v=${encodeURIComponent(currentState?.revision || Date.now())}` : `/finished/${encodeURIComponent(stripName)}?v=${Date.now()}`;
    strip.onerror = () => {
      // Fall back to the JPEG copy if the PNG is unavailable or an older
      // manifest points at a stale filename.
      if (!strip.dataset.fallback) {
        strip.dataset.fallback = "jpg";
        const jpgName = final.jpg ? final.jpg.split("/").pop() : "";
        if (jpgName) strip.src = privateBase ? `${privateBase}/view/strip?v=${Date.now()}` : `/finished/${encodeURIComponent(jpgName)}?v=${Date.now()}`;
      } else {
        strip.alt = "Photo strip preview is unavailable. Use SAVE PHOTO STRIP to download it.";
      }
    };
    strip.src = stripUrl;
  }
  const qr = $("guestFinalQr");
  if (qr) { if (final.qr) { qr.src = final.qr; qr.classList.remove("hidden"); } else { qr.removeAttribute("src"); qr.classList.add("hidden"); } }
  const qrMessage = $("qrMessage");
  if (qrMessage) qrMessage.textContent = final.url ? "Scan with your phone camera to view and save your photos." : "Your photos are saved on this photobooth computer. Open the gallery here to view them.";
  const gallery = $("galleryLink");
  const galleryPath = currentState?.status === "finished" ? `/session/${encodeURIComponent(sessionId)}` : `/ready/${encodeURIComponent(sessionId)}`;
  if (gallery) { gallery.href = final.url || "#"; gallery.hidden = !final.url; }
  const saveGif = $("saveGif");
  if (saveGif) { saveGif.hidden = !final.gif_filename; if (final.gif_filename) saveGif.href = privateBase ? `${privateBase}/final/gif` : `/final/${encodeURIComponent(final.gif_filename)}`; }
  const savePhotos = $("savePhotos");
  if (savePhotos) { savePhotos.disabled = !sessionId; savePhotos.onclick = () => { window.location.href = privateBase ? `${privateBase}/download/all.zip` : `/download/session/${encodeURIComponent(sessionId)}.zip`; }; }
  const saveGifButton = $("saveGifButton");
  if (saveGifButton) { saveGifButton.disabled = !final.gif_filename; saveGifButton.onclick = () => { if (final.gif_filename) window.location.href = privateBase ? `${privateBase}/final/gif` : `/final/${encodeURIComponent(final.gif_filename)}`; }; }
  const saveStrip = $("saveStrip");
  if (saveStrip) {
    const downloadableStrip = final.filename || (final.png ? final.png.split("/").pop() : "") ||
      (final.jpg ? final.jpg.split("/").pop() : "");
    saveStrip.disabled = !downloadableStrip;
    if (downloadableStrip) saveStrip.onclick = () => { window.location.href = privateBase ? `${privateBase}/final/strip` : `/final/${encodeURIComponent(final.jpg ? final.jpg.split("/").pop() : downloadableStrip.replace(/\.png$/i, ".jpg"))}`; };
  }
  const newSession = $("newSession");
  if (newSession) newSession.onclick = async () => {
    if (!confirm("Start a new guest session? This completed session will remain saved in Ready Gallery.")) return;
    newSession.disabled = true;
    try {
      const reset = await api("/api/session/reset", { method: "POST" });
      currentState = reset.state; preparingSession = ""; sequenceSession = ""; lastSession = ""; finalRenderedSession = "";
      renderHistory(currentState); show("welcome");
      $("guestStart").disabled = false;
      $("guestWelcomeMessage").textContent = "Ready for the next guest.";
      startCamera(true).catch(() => {});
    } catch (error) { $("saveMessage").textContent = error.message; }
    finally { newSession.disabled = false; }
  };
  show("completeScreen");
  $("autoCloseOverlay")?.classList.add("hidden");
  const message = $("saveMessage");
  if (message) message.textContent = "Your three photos are saved. Your private QR gallery is ready.";
}
async function prepareReadyGallery(sessionId) {
  if (preparingSession === sessionId) return;
  preparingSession = sessionId;
  // Keep the MJPEG preview connected while the server builds the strip/GIF.
  // Stopping it here caused the guest/secondary display to go blank during preparation.
  setCameraState("PREPARING", "PREPARING YOUR PHOTOS… CAMERA PREVIEW REMAINS LIVE");
  $("cameraPreparing")?.classList.remove("hidden");
  try {
    const result = await api("/api/session/prepare", { method: "POST" });
    currentState = result.state || await api("/api/state");
    if (currentState.session_id !== sessionId || currentState.photo_count !== 3) {
      throw new Error("Your three photos could not be confirmed as saved. Please try again.");
    }
    // Put SESSION COMPLETE on screen immediately; the GIF/strip render in the background.
    $("cameraPreparing")?.classList.add("hidden");
    renderSessionComplete(sessionId, { final: currentState.final || {} });
    if (result.processing || currentState.status === "processing") {
      $("saveMessage").textContent = "SESSION COMPLETE · Preparing your photo strip and GIF…";
      for (let attempt = 0; attempt < 180; attempt++) {
        await wait(350);
        const latest = await api("/api/state");
        if (latest.session_id !== sessionId) throw new Error("This session changed while its previews were being prepared.");
        currentState = latest;
        if (latest.status === "ready") { renderSessionComplete(sessionId, { final: latest.final || {} }); break; }
        if (latest.status === "ready_error") throw new Error("Your photos were saved, but the strip or GIF could not be generated. Please ask the operator to retry the export.");
      }
      if (currentState.status === "processing") throw new Error("Photo previews are taking longer than expected. Your original photos are safely saved.");
      $("cameraPreparing")?.classList.add("hidden");
      if (stream && frameReady()) setCameraState("READY", "LIVE CAMERA CONNECTED");
    } else {
      if (currentState.status !== "ready") throw new Error("Your photo previews are not ready yet.");
      renderSessionComplete(sessionId, result);
      $("cameraPreparing")?.classList.add("hidden");
      if (stream && frameReady()) setCameraState("READY", "LIVE CAMERA CONNECTED");
    }
  } catch (error) {
    preparingSession = "";
    $("cameraPreparing")?.classList.add("hidden");
    setCameraState(stream && frameReady() ? "READY" : "ERROR", stream && frameReady() ? "LIVE CAMERA CONNECTED" : "CAMERA PREVIEW UNAVAILABLE", !(stream && frameReady()));
    $("guestInstruction").textContent = error.message; show("captureScreen");
    $("captureStart")?.classList.remove("hidden"); $("activeTapLabel")?.classList.remove("hidden");
    startCamera(true).catch(() => {});
  }
}
async function runAutomaticSequence(sessionId) {
  if (previewMode || busy || sequenceSession === sessionId) return;
  busy = true; sequenceSession = sessionId; setCameraState("READY", "LIVE CAMERA CONNECTED"); show("captureScreen");
  $("captureStart")?.classList.add("hidden"); $("activeTapLabel")?.classList.add("hidden");
  try {
    if (!stream || !frameReady()) await startCamera();
    for (let slot = 1; slot <= 3; slot++) {
      let state = await api("/api/state");
      if (state.session_id !== sessionId || !state.active) break;
      if (state.photos?.some(photo => photo.slot === slot)) continue;
      const mirror = await countdownFor(slot);
      state = await api("/api/state");
      if (state.session_id !== sessionId || !state.active) break;
      if (state.photos?.some(photo => photo.slot === slot)) continue;
      await captureCurrent(slot, sessionId, mirror);
      // Move directly to the next countdown after the previous image is saved.
    }
    const latest = currentState?.session_id === sessionId ? currentState : await api("/api/state");
    currentState = latest; renderHistory(latest);
    if (latest.session_id === sessionId && latest.photo_count === 3 && latest.status !== "ready" && latest.status !== "finished") {
      await prepareReadyGallery(sessionId);
    } else if (latest.session_id === sessionId && latest.photo_count < 3) {
      $("guestInstruction").textContent = "A photo was not saved. Please try again."; show("captureScreen");
    }
  } catch (error) {
    $("guestInstruction").textContent = error.message; $("guestDeliveryMessage").textContent = error.message;
    $("captureStart")?.classList.remove("hidden"); $("activeTapLabel")?.classList.remove("hidden"); show("captureScreen");
    startCamera(true).catch(() => {});
  } finally { busy = false; if (stream && frameReady()) setCameraState("READY", "LIVE CAMERA CONNECTED"); }
}
async function refreshState() {
  try {
    const state = await api("/api/state"); currentState = state; renderHistory(state);
    // Never reopen the webcam after all three shots have been captured.
    if (!stream && !preparingSession && (previewMode || secondaryMode || state.photo_count < 3)) startCamera().catch(() => {});
    if (state.session_id !== lastSession) { lastSession = state.session_id; sequenceSession = ""; finalRenderedSession = ""; }
    if (previewMode) { show("captureScreen"); $("guestHistoryPanel")?.classList.add("hidden"); $("yslCredits")?.classList.remove("hidden"); return; }
    // Keep SESSION COMPLETE visible while derived exports are prepared asynchronously.
    if (state.status === "processing" && state.session_id && state.photo_count === 3) {
      preparingSession = state.session_id;
      setCameraState("PREPARING", "PREPARING YOUR PHOTOS… CAMERA PREVIEW REMAINS LIVE");
      $("cameraPreparing")?.classList.remove("hidden");
      show("completeScreen");
      if ($("saveMessage")) $("saveMessage").textContent = "SESSION COMPLETE · Preparing your photo strip and GIF…";
      return;
    }
    // Keep the completed session visible while the server retains its saved gallery.
    if (state.status === "ready" && state.session_id && state.photo_count) {
      // Keep SESSION COMPLETE visible until the guest manually opens the gallery.
      if (preparingSession !== state.session_id && state.photo_count === 3) {
        preparingSession = state.session_id;
        renderSessionComplete(state.session_id, { final: state.final });
      } else if (preparingSession === state.session_id) {
        if (state.final?.gif_filename && finalRenderedSession !== state.session_id) renderSessionComplete(state.session_id, { final: state.final });
        else show("completeScreen");
      }
      return;
    }
    if (state.status === "finished" && state.session_id) {
      if (preparingSession !== state.session_id) { preparingSession = state.session_id; renderSessionComplete(state.session_id, { final: state.final }); }
      else if (state.final?.gif_filename && finalRenderedSession !== state.session_id) renderSessionComplete(state.session_id, { final: state.final });
      else show("completeScreen");
      return;
    }
    if (!state.active && !state.photo_count) { show("welcome"); $("guestStart").disabled = false; return; }
    if (state.photo_count === 3 && state.active && !busy) {
      busy = true; prepareReadyGallery(state.session_id).finally(() => { busy = false; }); return;
    }
    show("captureScreen");
    if (state.active && state.capture_requested && !busy && state.session_id && sequenceSession !== state.session_id) {
      $("captureStart")?.classList.add("hidden"); $("activeTapLabel")?.classList.add("hidden");
      runAutomaticSequence(state.session_id);
    } else if (state.active && !busy && state.session_id && sequenceSession !== state.session_id) {
      $("captureStart")?.classList.remove("hidden"); $("activeTapLabel")?.classList.remove("hidden");
      $("guestInstruction").textContent = "Your session is ready. Waiting for the operator to start capture…";
    } else if (!state.active) { $("guestInstruction").textContent = "Waiting for the next photo session…"; }
  } catch { if ($("guestWelcomeMessage")) $("guestWelcomeMessage").textContent = "Connecting to the photobooth…"; }
}
async function beginSession() {
  if (previewMode || busy) return;
  try {
    await document.documentElement.requestFullscreen?.().catch(() => {});
    $("guestStart").disabled = true; $("guestWelcomeMessage").textContent = "Starting your photo session…";
    const result = await api("/api/session/start", payload({ shot_count: 3 }));
    currentState = result.state; lastSession = result.state.session_id; preparingSession = ""; finalRenderedSession = "";
    show("captureScreen"); runAutomaticSequence(result.state.session_id);
  } catch (error) { $("guestStart").disabled = false; $("guestWelcomeMessage").textContent = error.message; }
}
const mirrorControl = $("mirrorCamera");
if (mirrorControl) {
  try { const saved = localStorage.getItem("ysl_mirror_camera"); mirrorControl.checked = saved === null ? true : saved === "true"; }
  catch { mirrorControl.checked = true; }
  mirrorControl.addEventListener("change", () => {
    try { localStorage.setItem("ysl_mirror_camera", String(mirrorControl.checked)); } catch {}
    applyMirrorSetting();
  });
}
applyMirrorSetting();
window.addEventListener("pagehide", stopCameraStream);
window.addEventListener("storage", event => {
  if (event.key === "ysl_mirror_camera" && mirrorControl) { mirrorControl.checked = event.newValue !== "false"; applyMirrorSetting(); }
});
$("guestStart")?.addEventListener("click", beginSession);
$("captureStart")?.addEventListener("click", () => {
  if (busy || !currentState?.active || !currentState.session_id) return;
  $("captureStart").classList.add("hidden"); $("activeTapLabel").classList.add("hidden"); runAutomaticSequence(currentState.session_id);
});
(async () => {
  try { const config = await api("/api/config"); if (config.website_url && $("websiteSocial")) $("websiteSocial").href = config.website_url; } catch {}
  if (previewMode || secondaryMode) { $("welcome")?.classList.add("hidden"); $("guestHistoryPanel")?.classList.add("hidden"); $("yslCredits")?.classList.remove("hidden"); }
  setCameraState("INITIALIZING", "CONNECTING TO CAMERA…");
  try { await startCamera(); } catch {}
  await refreshState(); setInterval(refreshState, 250);
})();
