YSL VALENCIA OFFLINE PHOTOBOOTH — WINDOWS SETUP
================================================

PROJECT CONTENTS
- app.py: Flask backend, session state, local photo storage, GIF/strip generation, QR gallery, monitor launch and printing.
- templates/operator.html: operator dashboard and main/secondary screen previews.
- templates/guest.html: fullscreen guest capture interface.
- templates/ready_gallery.html: review queue for newly captured sessions.
- templates/finished_photos.html: archive of sessions explicitly saved by Finish & Save.
- templates/session_gallery.html: photo review, retake, delete, GIF preview and session management.
- templates/phone_camera.html: local smartphone upload page.
- static/css/style.css and static/css/gallery.css: local responsive styles.
- static/js/operator.js, guest.js and phone_camera.js: local browser behavior.
- assets/Logo.png: official transparent YSL logo.
- assets/facebook.png, instagram.png, website.png and websiste.png: local platform icons (also copied under static/assets/).
- assets/PHOTOBOOTH.png: original photo strip artwork, preserved unchanged.
- config.json: local settings and photo frame coordinates.
- storage/: original photos, generated files, QR images and session JSON.

ASSET FILENAME NOTE
The original `website.png` is preserved. A matching `websiste.png` compatibility copy is also included and used by website links, preserving the exact filename requested. Keep the exact `Logo.png` capitalization.

INSTALL ON WINDOWS
1. Install Python 3.10 or newer and enable “Add Python to PATH”.
2. Extract the complete ZIP to a writable folder.
3. Double-click start.bat. It creates a virtual environment, installs requirements and starts the local Flask server.
4. The operator dashboard opens at http://127.0.0.1:5000.
5. Keep the project folder and its storage/ directory in place. Do not remove storage/ if you want to keep existing sessions.

OFFLINE OPERATION
The application uses local HTML, CSS, JavaScript and image assets. Once Python dependencies are installed, the photobooth's essential capture, review, GIF and archive features work without internet. Facebook/Instagram links and the website link naturally require a network when opened; they are not required for capture or GIF generation.

DUAL MONITOR SETUP
1. Connect and enable the second monitor in Windows Display Settings.
2. Keep the operator dashboard on the primary display.
3. Click Launch Secondary Screen. The app attempts to open the guest interface in kiosk/fullscreen mode on the configured guest display.
4. If automatic placement fails, open http://127.0.0.1:5000/guest on the second monitor and press F11.
5. Set guest_display_index in config.json (0 = primary, 1 = second) or use the operator settings.

CAMERA
- Allow camera permission in the operator browser and select the camera.
- Click Connect Camera. The selected live feed is used by the main and guest displays.
- Camera access works on localhost in supported browsers. A phone accessing the PC over LAN may need HTTPS for live camera APIs; the phone upload page also offers a file/camera picker fallback.
- Test the actual camera, display and browser combination before the event.

THREE-PHOTO WORKFLOW
1. Start a session. Each session has exactly three numbered photo slots.
2. The guest screen runs a 3-2-1 countdown for each photo and records preparation frames for the animated flashback.
3. The Mirror Camera setting can be changed without restarting the camera. The chosen orientation is applied once to the live preview, captured photo and preparation frames; the GIF is not flipped a second time.
4. As soon as the third photo is successfully saved, SESSION COMPLETE appears. The camera stream is released and the photo strip/GIF/QR are generated in the background without redirecting the guest.
5. SESSION COMPLETE stays visible until the operator/guest chooses an available action. It includes the three captured photos, preview outputs, a private QR gallery, and the three download options. There is no timed redirect to Ready Gallery.
6. The operator can open Ready Gallery locally to review all three photos and the GIF, replace a photo from a file, retake with the camera, delete an individual photo, or delete the entire session.
7. Select Finish & Save in the local operator Ready Gallery to move the session into Finished Photos. All three photo slots must be filled.

READY GALLERY & FINISHED PHOTOS
- Ready Gallery: /ready-gallery
- Finished Photos: /finished-photos
- Session detail: /ready/<session-id> for Ready sessions and /session/<session-id> for Finished sessions (localhost/operator only). Guest QR pages use the token route /g/<random-token>.
- Both archives persist using storage/sessions/<session-id>/session.json and local image/GIF files. Refreshing the browser or restarting the server does not remove saved sessions.
- Finished sessions can still be edited. Replacing a photo regenerates the GIF and, when all three slots are filled, the strip. Deleting a photo invalidates the strip/QR and regenerates the GIF from the remaining photos. A session is not deleted when an individual photo is edited.
- Permanent session deletion requires confirmation and removes only the selected session's own originals, session folder, generated files and QR image.

GIF BRANDING
- The GIF is generated locally at 640 x 360 from the three preparation recordings (or still-photo fallback frames) and loops continuously.
- Every frame includes the official transparent Logo.png at top-left and a small transparent footer at bottom center with the local Facebook and Instagram icons and their YSL Valencia identities.
- No solid footer banner or external icon library is used. The bundled local PNGs are used offline.

PHOTO STRIP ALIGNMENT
The original supplied artwork remains unchanged at assets/PHOTOBOOTH.png. config.json defines photo_frames and photo_frames_right. Each frame uses x, y, width, height and radius in pixels against the original artwork. The current coordinates may need visual calibration with a test capture before the event.

SMARTPHONE UPLOAD & QR
1. Connect the phone and photobooth PC to the same Wi-Fi network.
2. Start a session from the operator or guest screen.
3. Select Show Phone QR on the operator dashboard and scan it.
4. Choose or capture a photo on the phone; it is sent to the next empty slot on the local PC.
5. For a public gallery link, set photo_share_base_url in config.json only to the actual host serving this Flask app and its persistent storage. Do not use a separate community website URL unless this app is deployed there.

LOCAL STORAGE
- storage/original/: original captured photos.
- storage/sessions/: per-session photo copies, preparation frames and session.json metadata.
- storage/final/: generated photo strips and GIFs.
- storage/qr/: generated QR images.
Starting a new session does not erase previous saved sessions. Keep the storage/ folder to retain them.

TROUBLESHOOTING
- Camera not found: grant camera permission, click Refresh in the operator dashboard, select the webcam and reconnect.
- Guest screen not detected: open /guest manually on the second monitor and use F11.
- Phone cannot open the QR page: allow Python through Windows Firewall on the private network and ensure both devices use the same Wi-Fi.
- Camera access on a phone over local HTTP may be restricted by the browser. Use the file/camera picker fallback or configure HTTPS for LAN camera access.
- If the app fails to start, run start.bat again and review the terminal error. Confirm Python is installed and requirements.txt dependencies are available.

LATEST REVISION — 13 NOVEMBER 2026
=================================

SESSION COMPLETE
- Exactly three successfully saved photos trigger SESSION COMPLETE immediately.
- The camera stream is stopped and detached after capture.
- Photo-strip and GIF processing runs in a background worker. The completion screen
  remains visible while previews are prepared and is updated when they are ready.
- There is no automatic redirect to session_gallery.html. View My Photos, Retake
  Photos, and Continue to Gallery are not shown on the completion screen.
- Guests can explicitly start a new session after the current photos are saved.

OPERATOR PHOTO MANAGEMENT
- Ready Gallery and Finished Photos remain local operator pages.
- Operators can replace, retake, or delete individual photos and delete a session.
- Replacements and deletions update the original files and session.json manifest;
  derived strip/GIF exports are rebuilt only when all three slots are present.
- A partial session remains available for the operator to fill its missing slot.

PRIVATE GUEST QR ACCESS
- Every session receives a cryptographically random URL-safe guest access token.
- The guest QR uses /g/<token>, not the predictable timestamp-based session ID.
- Token-protected endpoints provide read-only access to that session's photos,
  animated GIF, photo strip, and ZIP of the three original photos.
- Tokens expire after 30 days. Invalid/expired tokens return 404.
- Session-ID-only gallery and file routes, archive pages, destructive endpoints,
  and kiosk control APIs are restricted to localhost.
- Private guest responses use no-store, no-referrer, and noindex headers.
- Phone upload uses a separate session-scoped token and its old link expires when
  the live session is reset or ends.

DOWNLOADS & BRANDING
- Download All Photos creates a ZIP containing the three original captures only.
- Download GIF serves the actual animated GIF; Download Photo Strip serves the
  high-quality JPEG strip. The PNG remains available for in-page preview.
- Every GIF frame includes the transparent official Logo.png at top-left and
  Facebook / Instagram icons with “Facebook: YSL Valencia” and
  “Instagram: YSL Valencia” at bottom-center, with no solid banner.
- Mirror Camera applies once to the captured canvas so saved photos match preview.
- The main and gallery styles use pastel blue #A7D8F0 / #D9EFF9, pastel yellow
  #FFF1A8 / #FFF9D9, white, and dark neutral text #34495E.

VALIDATION NOTES
- Python source compilation and JavaScript syntax checks passed.
- Full Flask integration tests could not be executed in this isolated build
  environment because Flask and screeninfo are not installed and package downloads
  are unavailable. On the Windows photobooth computer, run `pip install -r
  requirements.txt` and then `python -m unittest -v test_app.py`.
- Test the camera, dual-monitor launch, printer, and QR scanning on the actual
  event hardware and same Wi-Fi network before public use.

CAMERA PREVIEW SMOOTHNESS UPDATE
- Guest and Secondary screens now receive a native MJPEG push stream from /api/live-stream.mjpg. They no longer fetch and decode a new JPEG Blob every 110 ms.
- The server stores only the newest frame and signals connected screens when a new sequence arrives; stale frames are not queued.
- A small /api/live-frame-status endpoint reports whether the operator camera feed is current.
- The operator preview relay uses a single requestAnimationFrame loop and skips frames while a previous upload is still active. The relay reuses one preview canvas and does not open another camera stream.
- Main capture still uses the original camera video source. Guest photo capture continues using the existing relayed preview resolution and quality, so this update does not lower its existing preview-derived capture dimensions.
- See CAMERA_SMOOTHNESS_UPDATE_NOTES.txt for implementation and validation details.
