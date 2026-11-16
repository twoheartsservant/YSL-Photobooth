/**
 * YSL Valencia Photobooth - Google Drive guest gallery backend
 *
 * Deploy as a Web App:
 *   Execute as: Me
 *   Who has access: Anyone
 *
 * Script Properties required:
 *   YSL_UPLOAD_SECRET = same random secret configured in config.json
 * Optional:
 *   YSL_ROOT_FOLDER_ID = existing Drive folder ID; omit to auto-create one.
 */

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function props_() {
  return PropertiesService.getScriptProperties();
}

function rootFolder_() {
  var p = props_();
  var id = p.getProperty('YSL_ROOT_FOLDER_ID');
  if (id) return DriveApp.getFolderById(id);
  var folder = DriveApp.createFolder('YSL Valencia Photobooth');
  p.setProperty('YSL_ROOT_FOLDER_ID', folder.getId());
  return folder;
}

function sessionFolder_(sessionId) {
  var root = rootFolder_();
  var folders = root.getFoldersByName(sessionId);
  if (folders.hasNext()) return folders.next();
  return root.createFolder(sessionId);
}

function validSession_(id) {
  return /^\d{8}-\d{6}-[0-9a-f]{6}$/.test(String(id || ''));
}

function validToken_(token) {
  return /^[A-Za-z0-9_-]{40,100}$/.test(String(token || ''));
}

function sessionKey_(token) {
  return 'YSL_SESSION_' + token;
}

function readSession_(token) {
  if (!validToken_(token)) return null;
  var raw = props_().getProperty(sessionKey_(token));
  if (!raw) return null;
  try { return JSON.parse(raw); } catch (e) { return null; }
}

function saveSession_(token, data) {
  props_().setProperty(sessionKey_(token), JSON.stringify(data));
}

function doPost(e) {
  try {
    var data = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    var expected = props_().getProperty('YSL_UPLOAD_SECRET') || '';
    if (!expected || !data.secret || data.secret !== expected) {
      return json_({ok:false, message:'Unauthorized upload request.'});
    }

    var action = String(data.action || '');
    if (action === 'ping') {
      var root = rootFolder_();
      return json_({ok:true, message:'Google Drive connection is ready.', rootFolderName:root.getName(),
        storageUsedBytes:DriveApp.getStorageUsed()});
    }
    var sid = String(data.sessionId || '');
    var token = String(data.accessToken || '');
    if (!validSession_(sid) || !validToken_(token)) {
      return json_({ok:false, message:'Invalid session identifier or access token.'});
    }

    var record = readSession_(token) || {
      sessionId: sid, accessToken: token, files: {}, createdAt: new Date().toISOString()
    };
    if (record.sessionId !== sid) {
      return json_({ok:false, message:'Session token does not match this session.'});
    }

    var folder = sessionFolder_(sid);

    if (action === 'upload') {
      var kind = String(data.kind || '');
      var allowedKinds = ['photo', 'strip', 'gif'];
      if (allowedKinds.indexOf(kind) < 0) {
        return json_({ok:false, message:'Unsupported file category.'});
      }
      var name = String(data.fileName || '');
      var allowedNames = /^Photo-[1-3]\.jpg$|^Photo-Strip\.jpg$|^YSL-Flashback\.gif$/;
      if (!allowedNames.test(name)) {
        return json_({ok:false, message:'Invalid upload filename.'});
      }
      var mime = String(data.mimeType || '');
      var expectedMime = name.toLowerCase().endsWith('.gif') ? 'image/gif' : 'image/jpeg';
      if (mime !== expectedMime) {
        return json_({ok:false, message:'File type does not match its extension.'});
      }
      var encoded = String(data.base64 || '');
      if (!encoded || encoded.length > 47 * 1024 * 1024) {
        return json_({ok:false, message:'File is empty or exceeds the upload size limit.'});
      }
      var fileKey = kind === 'photo' ? name : kind;
      var existing = record.files[fileKey];
      var checksum = String(data.sha256 || '');
      var file;
      if (existing && existing.id && existing.sha256 === checksum) {
        try {
          file = DriveApp.getFileById(existing.id);
          if (file.isTrashed()) file = null;
        } catch (ignored) { file = null; }
      }
      if (!file && existing && existing.id) {
        try { DriveApp.getFileById(existing.id).setTrashed(true); } catch (ignored2) {}
      }
      if (!file) {
        var matches = folder.getFilesByName(name);
        if (matches.hasNext() && !existing) {
          file = matches.next();
        } else {
          var bytes = Utilities.base64Decode(encoded);
          file = folder.createFile(Utilities.newBlob(bytes, mime, name));
        }
      }
      file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
      var item = {id:file.getId(), name:name, kind:kind, mimeType:mime, size:file.getSize(), sha256:checksum};
      record.files[fileKey] = item;
      record.updatedAt = new Date().toISOString();
      saveSession_(token, record);
      return json_({ok:true, file:item, storageUsedBytes:DriveApp.getStorageUsed()});
    }

    if (action === 'publish') {
      var values = Object.keys(record.files).map(function(k) { return record.files[k]; });
      var hasStrip = !!record.files.strip;
      var hasGif = !!record.files.gif;
      var photoCount = values.filter(function(f) { return f.kind === 'photo'; }).length;
      if (!hasStrip || !hasGif || photoCount !== 3) {
        return json_({ok:false, message:'The session is missing its photo strip, GIF, or original photos.'});
      }
      var url = ScriptApp.getService().getUrl() + '?t=' + encodeURIComponent(token);
      record.publishedUrl = url;
      record.publishedAt = new Date().toISOString();
      saveSession_(token, record);
      return json_({ok:true, url:url, fileCount:values.length,
        storageBytes:values.reduce(function(total, f) { return total + Number(f.size || 0); }, 0),
        driveStorageUsedBytes:DriveApp.getStorageUsed()});
    }

    return json_({ok:false, message:'Unknown action.'});
  } catch (err) {
    return json_({ok:false, message:String(err && err.message || err)});
  }
}

function doGet(e) {
  var token = String((e && e.parameter && e.parameter.t) || '');
  var record = readSession_(token);
  if (!record || !record.publishedUrl) {
    return HtmlService.createHtmlOutput('<main style="font-family:Arial;padding:32px;text-align:center"><h2>YSL Valencia Photobooth</h2><p>This gallery link is invalid or unavailable.</p></main>')
      .setTitle('YSL Valencia · Gallery');
  }

  var files = record.files || {};
  var photos = Object.keys(files).map(function(k) { return files[k]; })
    .filter(function(f) { return f.kind === 'photo'; })
    .sort(function(a,b) { return a.name.localeCompare(b.name); });
  var strip = files.strip;
  var gif = files.gif;
  var html = [];
  html.push('<!doctype html><html><head><meta charset="utf-8">');
  html.push('<meta name="viewport" content="width=device-width,initial-scale=1">');
  html.push('<meta name="theme-color" content="#f5f0e8"><title>YSL Valencia · Your Photos</title>');
  html.push('<style>');
  html.push('body{margin:0;background:#f7f4ef;color:#332b2b;font-family:Arial,sans-serif}main{max-width:900px;margin:auto;padding:22px}');
  html.push('header{text-align:center;padding:12px 0 20px}h1{font-size:26px;color:#5b1020;margin:8px 0}h2{font-size:17px;color:#5b1020;margin:28px 0 14px}');
  html.push('.note{background:#fff;border:1px solid #e5d9cb;padding:14px;border-radius:12px;line-height:1.5}');
  html.push('.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}');
  html.push('figure{margin:0;background:white;padding:10px;border:1px solid #e8dfd5;border-radius:12px}img{max-width:100%;display:block;border-radius:8px}figure img{width:100%;aspect-ratio:4/3;object-fit:cover}');
  html.push('.hero{max-width:520px;margin:12px auto}.btn{display:inline-block;background:#5b1020;color:#fff;text-decoration:none;padding:12px 16px;border-radius:8px;font-weight:bold;margin:5px 5px 5px 0}');
  html.push('.secondary{background:#a77b32}.downloads{display:flex;flex-wrap:wrap;gap:7px;margin:14px 0}footer{text-align:center;color:#766b64;font-size:12px;margin:30px 0}');
  html.push('</style></head><body><main><header><h1>Young Servants of the Lord</h1><p>YSL Valencia · Your Photobooth Photos</p></header>');
  html.push('<div class="note">Your private session gallery is ready. You can preview and download your original photos, finished photo strip, and GIF. No Google sign-in is required.</div>');
  html.push('<h2>YOUR FINISHED PHOTO STRIP</h2>');
  html.push('<img class="hero" src="https://drive.google.com/thumbnail?id=' + encodeURIComponent(strip.id) + '&sz=w1600" alt="Finished photo strip">');
  html.push('<div class="downloads"><a class="btn" href="https://drive.google.com/uc?export=download&id=' + encodeURIComponent(strip.id) + '">Download Photo Strip</a>');
  html.push('<a class="btn secondary" href="https://drive.google.com/uc?export=download&id=' + encodeURIComponent(gif.id) + '">Download GIF</a></div>');
  html.push('<h2>YOUR ORIGINAL PHOTOS</h2><div class="grid">');
  photos.forEach(function(f, i) {
    html.push('<figure><img loading="lazy" src="https://drive.google.com/thumbnail?id=' + encodeURIComponent(f.id) + '&sz=w900" alt="Photo ' + (i+1) + '"><p><a href="https://drive.google.com/uc?export=download&id=' + encodeURIComponent(f.id) + '">Download Photo ' + (i+1) + '</a></p></figure>');
  });
  html.push('</div><footer>YSL Valencia · Thank you for joining us</footer></main></body></html>');
  return HtmlService.createHtmlOutput(html.join('')).setTitle('YSL Valencia · Your Photos')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}


/**
 * Optional retention cleanup.
 * Disabled unless Script Property YSL_RETENTION_DAYS is a positive integer.
 * Add a time-driven trigger for this function only if automatic cleanup is desired.
 */
function cleanupExpiredSessions() {
  var days = parseInt(props_().getProperty('YSL_RETENTION_DAYS') || '0', 10);
  if (!days || days < 1) return;
  var cutoff = new Date(Date.now() - days * 24 * 60 * 60 * 1000);
  var folders = rootFolder_().getFolders();
  while (folders.hasNext()) {
    var folder = folders.next();
    var match = folder.getName().match(/^(\d{8})-(\d{6})-[0-9a-f]{6}$/);
    if (!match) continue;
    var stamp = match[1] + match[2];
    var created = new Date(
      Number(stamp.slice(0,4)), Number(stamp.slice(4,6))-1, Number(stamp.slice(6,8)),
      Number(stamp.slice(8,10)), Number(stamp.slice(10,12)), Number(stamp.slice(12,14))
    );
    if (created < cutoff) {
      var files = folder.getFiles();
      while (files.hasNext()) files.next().setTrashed(true);
      folder.setTrashed(true);
    }
  }
}
