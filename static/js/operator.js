const $=id=>document.getElementById(id);let cameraStream=null,lastStrip=null,phoneLink="",busyCapture=false;
const operatorMirror=$("operatorMirrorCamera");
function mirrorCameraEnabled(){return operatorMirror?.checked!==false}
function applyOperatorMirror(){const v=$("captureVideo");if(v)v.style.transform=mirrorCameraEnabled()?"scaleX(-1)":"none"}
if(operatorMirror){try{const saved=localStorage.getItem("ysl_mirror_camera");operatorMirror.checked=saved===null?true:saved==="true"}catch{operatorMirror.checked=true}operatorMirror.addEventListener("change",()=>{try{localStorage.setItem("ysl_mirror_camera",String(operatorMirror.checked))}catch{}applyOperatorMirror()})}
window.addEventListener("storage",event=>{if(event.key==="ysl_mirror_camera"&&operatorMirror){operatorMirror.checked=event.newValue!=="false";applyOperatorMirror()}});
// Create a hidden camera source if the operator page markup is stale or missing it.
function getCaptureVideo(){
  let video=$("captureVideo");
  if(!video){
    video=document.createElement("video"); video.id="captureVideo";
    video.autoplay=true; video.playsInline=true; video.muted=true;
    video.setAttribute("aria-hidden","true");
    video.style.cssText="position:fixed;width:1px;height:1px;opacity:0;pointer-events:none;left:-10px;top:-10px";
    document.body.appendChild(video);
  }
  return video;
}
async function api(url,opts={}){const r=await fetch(url,opts);let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.message||`Request failed (${r.status})`);return d}
function note(id,msg,error=false){const el=$(id);if(!el)return;el.textContent=msg;el.classList.toggle("error",error)}
function setOperatorCameraState(state, message){
  document.body.dataset.cameraState = state;
  const status = $("cameraStatus");
  if (status && message) { status.textContent = message; status.classList.toggle("error", state === "ERROR"); }
}
// Preview-only relay. The Main camera and full-resolution photo capture remain untouched.
// One animation loop samples the newest decoded camera frame; a busy upload means
// the intermediate frames are skipped rather than queued.
let frameRelayLoopId = 0, frameRelayBusy = false, frameRelayCanvas = null;
let frameRelayContext = null, lastFrameRelaySuccess = 0, lastFrameRelayAt = 0;
let frameRelayGeneration = 0, frameRelayMaxWidth = 1280;
const RELAY_MAX_BYTES = 1_400_000;
const RELAY_FPS = 18;
function stopFrameRelay() {
  frameRelayGeneration++;
  if (frameRelayLoopId) cancelAnimationFrame(frameRelayLoopId);
  frameRelayLoopId = 0;
}
function canvasBlob(canvas, quality) {
  return new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", quality));
}
function startFrameRelay() {
  stopFrameRelay();
  if (!cameraStream) return;
  const video = getCaptureVideo();
  if (!frameRelayCanvas) {
    frameRelayCanvas = document.createElement("canvas");
    frameRelayContext = frameRelayCanvas.getContext("2d", { alpha: false, desynchronized: true });
  }
  if (!frameRelayContext) return;
  const generation = ++frameRelayGeneration;
  const schedule = () => {
    if (generation === frameRelayGeneration) frameRelayLoopId = requestAnimationFrame(tick);
  };
  const tick = async now => {
    if (!cameraStream || video.srcObject !== cameraStream || video.readyState < 2 || !video.videoWidth || !video.videoHeight) {
      schedule(); return;
    }
    if (frameRelayBusy || now - lastFrameRelayAt < 1000 / RELAY_FPS) { schedule(); return; }
    frameRelayBusy = true;
    lastFrameRelayAt = now;
    try {
      // Keep the preview encoder canvas dimensions stable to avoid reallocating
      // its backing store on every frame. This does not affect photo capture.
      const scale = Math.min(1, frameRelayMaxWidth / video.videoWidth);
      const width = Math.max(1, Math.round(video.videoWidth * scale));
      const height = Math.max(1, Math.round(video.videoHeight * scale));
      if (frameRelayCanvas.width !== width || frameRelayCanvas.height !== height) {
        frameRelayCanvas.width = width; frameRelayCanvas.height = height;
      }
      frameRelayContext.drawImage(video, 0, 0, width, height);
      let blob = await canvasBlob(frameRelayCanvas, 0.72);
      if (!blob) throw new Error("Could not encode the camera preview frame.");
      if (blob.size > RELAY_MAX_BYTES) {
        // A single smaller retry is preview-only; never alter captured originals.
        frameRelayMaxWidth = Math.max(640, Math.round(frameRelayMaxWidth * 0.78));
        const smallerScale = Math.min(1, frameRelayMaxWidth / video.videoWidth);
        const smallerWidth = Math.max(1, Math.round(video.videoWidth * smallerScale));
        const smallerHeight = Math.max(1, Math.round(video.videoHeight * smallerScale));
        frameRelayCanvas.width = smallerWidth; frameRelayCanvas.height = smallerHeight;
        frameRelayContext.drawImage(video, 0, 0, smallerWidth, smallerHeight);
        blob = await canvasBlob(frameRelayCanvas, 0.62);
      }
      if (!blob || blob.size > RELAY_MAX_BYTES) throw new Error("Preview frame exceeds the local relay limit.");
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 1800);
      let response;
      try {
        response = await fetch("/api/live-frame", {
          method: "POST", headers: { "Content-Type": "image/jpeg" },
          body: blob, cache: "no-store", signal: controller.signal
        });
      } finally { clearTimeout(timeout); }
      if (response.ok) {
        lastFrameRelaySuccess = Date.now();
        const relayLatency = Math.round(performance.now() - lastFrameRelayAt);
        const status = $("guestPreviewStatus");
        if (status) status.textContent = `Live preview · relay ${relayLatency} ms`;
      } else {
        const status = $("guestPreviewStatus");
        if (status) status.textContent = `Camera preview unavailable · HTTP ${response.status}`;
      }
    } catch (error) {
      const status = $("guestPreviewStatus");
      if (status) status.textContent = "Reconnecting camera preview…";
      console.warn("Live camera preview relay:", error);
    } finally {
      frameRelayBusy = false;
      schedule();
    }
  };
  frameRelayLoopId = requestAnimationFrame(tick);
}
function payload(obj){return {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(obj)}}
async function refreshState(){try{const s=await api("/api/state");const count=s.shot_count||3;$ ("sessionStatus").textContent=s.active?"● Session active":"No active session";$ ("photoCount").textContent=`${s.photo_count} / ${count}`;$ ("sessionShort").textContent=s.session_id?s.session_id.slice(-6):"—";$ ("sessionProgress").style.width=`${s.photo_count/count*100}%`;$ ("startSession").disabled=s.active;$ ("endSession").disabled=!s.active||s.photo_count>=count;note("sessionMessage",s.active?"Session is live. Both screens are synchronized.":"Ready for the next guest.");renderGallery(s.photos,count);if(s.final)lastStrip=s.final;if(s.photo_count===count)note("stripResult",s.status==="ready"?"All shots are ready. Finish & Save to move this session into Finished Photos.":"All shots captured. Preparing the Ready Gallery…");const finishButton=$("generateStrip");if(finishButton)finishButton.disabled=!(s.status==="ready"&&s.photo_count===3);const sel=$("shotCount");if(sel&&document.activeElement!==sel)sel.value=count;const c=$("mainPreviewCapture");if(c)c.disabled=!s.active||s.photo_count>=count||s.capture_requested||busyCapture||!cameraStream;const status=$("mainPreviewStatus");if(status)status.textContent=cameraStream?`Live camera · ${s.photo_count}/${count} shots`:`Camera disconnected · ${s.photo_count}/${count} shots`;const g=$("guestPreviewStatus");if(g&&Date.now()-lastFrameRelaySuccess<2500)g.textContent=`Live camera preview · ${s.photo_count}/${count} shots`;else if(g)g.textContent=`Waiting for camera preview · ${s.photo_count}/${count} shots`;if(s.final){showDelivery(s.final)}}catch(e){note("systemStatus","● Backend unavailable",true)}}
function renderGallery(photos,count){const g=$("gallery");if(!g)return;g.innerHTML="";for(let n=1;n<=count;n++){const p=photos.find(x=>x.slot===n);const card=document.createElement("div");card.className="photo-card";card.innerHTML=p?`<img src="${p.url}?v=${Date.now()}" alt="Photo ${n}"><strong>Shot ${n}</strong><div class="actions"><button class="outline retake">Retake</button><button class="danger delete">Delete</button></div>`:`<div class="empty">Waiting for shot</div><strong>Shot ${n}</strong><div class="muted">Empty slot</div>`;if(p){card.querySelector(".retake").onclick=()=>removePhoto(n,"Retake selected shot");card.querySelector(".delete").onclick=()=>removePhoto(n,"Delete selected shot");}g.append(card)}}
async function removePhoto(n,label){if(!confirm(`${label} ${n}?`))return;try{const d=await api(`/api/photo/${n}`,{method:"DELETE"});note("sessionMessage",d.message);await refreshState()}catch(e){note("sessionMessage",e.message,true)}}
$("startSession").onclick=async()=>{try{const d=await api("/api/session/start",payload({shot_count:Number($("shotCount").value)}));note("sessionMessage",d.message);await refreshState()}catch(e){note("sessionMessage",e.message,true)}};
$("endSession").onclick=async()=>{if(!confirm("End the current session?"))return;try{const d=await api("/api/session/end",{method:"POST"});note("sessionMessage",d.message);await refreshState()}catch(e){note("sessionMessage",e.message,true)}};
$("launchGuest").onclick=()=>{
 const secondaryUrl=new URL("/guest?secondary=1",location.origin).href;
 const win=window.open(secondaryUrl,"ysl-secondary-photobooth","popup=yes,width=1280,height=900,resizable=yes");
 if(!win){note("sessionMessage","The secondary window was blocked. Allow pop-ups for this local photobooth, then click Launch Secondary Screen again.",true);return;}
 try{win.focus()}catch{}
 note("sessionMessage","Secondary screen opened. Move it to the second monitor and maximize it. Connecting the shared camera…");
 if(cameraStream)startFrameRelay();
};
async function enumerateCameras(){
  try{
    if(!navigator.mediaDevices?.enumerateDevices||!navigator.mediaDevices?.getUserMedia)throw new Error("This browser does not support camera access.");
    const sel=$("cameraSelect");
    if(!sel)return;
    const previous=sel.value;
    let devices=await navigator.mediaDevices.enumerateDevices();
    let cams=devices.filter(d=>d.kind==="videoinput");
    const saved=await api("/api/cameras").catch(()=>({selected:""}));
    sel.innerHTML='<option value="">Choose camera</option>';
    cams.forEach((c,i)=>{const o=document.createElement("option");o.value=c.deviceId;o.textContent=c.label||`Camera ${i+1}`;sel.append(o)});
    if(previous&&cams.some(c=>c.deviceId===previous))sel.value=previous;
    else if(saved.selected&&cams.some(c=>c.deviceId===saved.selected))sel.value=saved.selected;
    else if(cams[0])sel.value=cams[0].deviceId;
    if(!cameraStream)await connectCamera(false);
    // Permission has now been granted by the single active camera request;
    // refresh labels without opening and immediately stopping a second stream.
    devices=await navigator.mediaDevices.enumerateDevices();
    cams=devices.filter(d=>d.kind==="videoinput");
    const selected=sel.value;
    sel.innerHTML='<option value="">Choose camera</option>';
    cams.forEach((c,i)=>{const o=document.createElement("option");o.value=c.deviceId;o.textContent=c.label||`Camera ${i+1}`;sel.append(o)});
    if(cams.some(c=>c.deviceId===selected))sel.value=selected;
    if(!cams.length)note("cameraStatus","No camera was detected. Connect a camera and click Refresh.",true);
  }catch(e){note("cameraStatus","Camera permission required: "+e.message,true);note("sessionMessage","Unable to access camera: "+e.message,true)}
}
async function connectCamera(showMessage=true){
  setOperatorCameraState("INITIALIZING", "Initializing camera…");
  try{
    if(!navigator.mediaDevices?.getUserMedia)throw new Error("Camera access is unavailable in this browser.");
    const id=$("cameraSelect")?.value||"";
    const video=getCaptureVideo();
    video.autoplay=true;video.playsInline=true;video.muted=true;
    const existingTrack=cameraStream?.getVideoTracks?.()[0];
    const existingId=existingTrack?.getSettings?.().deviceId||"";
    if(cameraStream&&existingTrack?.readyState==="live"&&(!id||!existingId||id===existingId)){
      if(video.srcObject!==cameraStream)video.srcObject=cameraStream;
      await video.play();
      startFrameRelay();
      setOperatorCameraState("READY", "Camera already connected. Reusing the active camera stream.");
      return true;
    }
    // Acquire the replacement first; do not interrupt the working preview if
    // selecting a different camera fails.
    setOperatorCameraState("REQUESTING_PERMISSION", "Waiting for camera permission…");
    const nextStream=await navigator.mediaDevices.getUserMedia({video:id?{deviceId:{exact:id}}:true,audio:false});
    const previousStream=cameraStream;
    cameraStream=nextStream;
    video.srcObject=nextStream;
    await video.play();
    if(previousStream&&previousStream!==nextStream)previousStream.getTracks().forEach(t=>t.stop());
    applyOperatorMirror();
    const placeholder=$("mainCameraPlaceholder");if(placeholder)placeholder.classList.add("hidden");
    const track=nextStream.getVideoTracks()[0];
    if(track){
      track.onended=()=>{
        stopFrameRelay();cameraStream=null;
        setOperatorCameraState("ERROR", "Camera disconnected. Reconnect the camera to restore the live feed.");
        const p=$("mainCameraPlaceholder");if(p){p.textContent="Camera disconnected. Reconnect to continue.";p.classList.remove("hidden")}
        note("mainPreviewStatus","Camera disconnected");
      };
    }
    // Start relaying immediately after the local camera is live. Persisting the
    // selected device is helpful metadata, but must not block the live preview.
    startFrameRelay();
    try{await api("/api/camera",payload({device_id:id||track?.getSettings?.().deviceId||""}));}
    catch(error){console.warn("Could not persist selected camera metadata",error);}
    setOperatorCameraState("READY", "Camera connected. Live preview is active on both screens.");
    note("mainPreviewStatus","Live camera connected");
    if(showMessage)note("sessionMessage","Camera connected. Both screens now show the live camera.");
    return true;
  }catch(e){
    setOperatorCameraState("ERROR", "Unable to start camera preview: "+e.message);
    note("sessionMessage","Unable to access camera: "+e.message,true);
    throw e;
  }
}
$("refreshCameras").onclick=enumerateCameras;
$("startPreview").onclick=()=>connectCamera(true).catch(()=>{});
$("stopPreview").onclick=()=>{stopFrameRelay();if(cameraStream)cameraStream.getTracks().forEach(t=>t.stop());cameraStream=null;const video=getCaptureVideo();if(video)video.srcObject=null;const p=$("mainCameraPlaceholder");if(p){p.textContent="Camera disconnected. Click Connect Camera.";p.classList.remove("hidden")}setOperatorCameraState("PAUSED", "Camera disconnected by operator.");note("mainPreviewStatus","Camera disconnected")};
async function captureFromMain(){
  if(busyCapture||!cameraStream)return;
  busyCapture=true;
  const mainButton=$("mainPreviewCapture");
  if(mainButton)mainButton.disabled=true;
  try{
    const s=await api("/api/state");
    if(!s.active)throw new Error("Start a session first.");
    if(s.capture_requested||s.photo_count)throw new Error("The capture sequence has already started.");
    const result=await api("/api/capture/start",{method:"POST"});
    note("sessionMessage",result.message||"Capture sequence sent to the guest screen.");
    await refreshState();
  }catch(e){note("sessionMessage",e.message,true)}
  finally{busyCapture=false;await refreshState()}
}
$("mainPreviewCapture").onclick=captureFromMain;
$("generateStrip").onclick=async()=>{try{const s=await api("/api/state");if(!s.session_id||s.status!=="ready"||s.photo_count!==3)throw new Error("Wait until all three photos appear in Ready Gallery.");const d=await api(`/api/ready-session/${encodeURIComponent(s.session_id)}/finish`,{method:"POST"});lastStrip=d.final;showDelivery(d.final);note("stripResult","Session saved to Finished Photos. The photo strip and GIF are ready.");await refreshState()}catch(e){note("stripResult",e.message,true)}};
function showDelivery(d){if(!d||!d.qr)return;const box=$("deliveryBox");if(!box)return;box.classList.remove("hidden");$("finalQr").src=d.qr;$("finalPreview").src=`/finished/${d.filename}`;$("softCopy").href=d.png;$("softCopy").download=d.filename||"YSL-Photobooth.png";$("printStrip").disabled=false;}
$("printStrip").onclick=async()=>{if(!lastStrip)return;try{const d=await api("/api/print",payload({filename:lastStrip.jpg.split("/").pop()}));note("deliveryMessage",d.message,!d.ok)}catch(e){note("deliveryMessage",e.message,true)}};
$("showQr").onclick=async()=>{try{const d=await api("/api/phone/qr",{method:"POST"});$("phoneQr").src=d.qr;$("phoneQr").classList.remove("hidden");phoneLink=d.url;$("phoneUrl").textContent=d.url;$("copyPhoneUrl").disabled=false}catch(e){note("phoneUrl",e.message,true)}};
$("copyPhoneUrl").onclick=()=>navigator.clipboard.writeText(phoneLink).then(()=>note("phoneUrl","Local phone URL copied.")).catch(()=>note("phoneUrl",phoneLink));
$("saveSettings").onclick=async()=>{try{const d=await api("/api/config",payload({countdown_seconds:Number($("countdown").value),guest_display_index:Number($("monitorIndex").value),photo_count:Number($("shotCount").value)}));note("sessionMessage",d.message);await refreshState()}catch(e){note("sessionMessage",e.message,true)}};
$("shotCount").onchange=async()=>{try{await api("/api/session/shot-count",payload({shot_count:Number($("shotCount").value)}));await refreshState()}catch(e){note("sessionMessage",e.message,true)}};
(async()=>{setOperatorCameraState("INITIALIZING", "Initializing camera…");try{const c=await api("/api/config");$("countdown").value=c.countdown_seconds;$("monitorIndex").value=c.guest_display_index;$("shotCount").value=c.photo_count||3}catch{}await refreshState();applyOperatorMirror();setInterval(refreshState,1000);if(navigator.mediaDevices?.getUserMedia)enumerateCameras()})();
