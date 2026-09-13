/**
 * Vesta upload manager.
 *
 * Extends VESTA_BACKEND with uploadFile(). Handles the four things the plain
 * "POST a FormData" approach cannot:
 *
 *   progress   XHR reports bytes sent, so the UI can show a real bar
 *   dedupe     the file is hashed first; if those exact bytes are already stored
 *              nothing is uploaded and the existing file is linked instead
 *   resume     large files go up in 8 MB parts, each retried independently, and
 *              the completed parts are remembered so a reload continues rather
 *              than restarting
 *   integrity  the row is only marked complete after R2 confirms the object
 *
 * Load after vesta-cloud.js.
 */
(function (global) {
  'use strict';

  var BE = global.VESTA_BACKEND;
  if (!BE) return;

  var RESUME_KEY = 'vesta-uploads';
  var MAX_PART_ATTEMPTS = 4;
  var HASH_LIMIT = 64 * 1024 * 1024; // above this, hashing costs more than dedupe saves

  // -------------------------------------------------------------------------
  // resume bookkeeping
  //
  // Kept in localStorage so a refresh mid-upload does not lose the parts already
  // accepted. Keyed by a fingerprint of the file itself, not its name, so picking
  // the same file again resumes and a different file with the same name does not.
  // -------------------------------------------------------------------------

  function fingerprint(file) {
    return [file.name, file.size, file.lastModified || 0].join(':');
  }

  function loadResume() {
    try { return JSON.parse(localStorage.getItem(RESUME_KEY) || '{}'); } catch (e) { return {}; }
  }
  function saveResume(all) {
    try { localStorage.setItem(RESUME_KEY, JSON.stringify(all)); } catch (e) { /* full or blocked */ }
  }
  function getResume(file) {
    var all = loadResume();
    var entry = all[fingerprint(file)];
    // A stale entry points at an upload the nightly sweep has already discarded.
    if (entry && Date.now() - entry.startedAt > 20 * 60 * 60 * 1000) {
      delete all[fingerprint(file)];
      saveResume(all);
      return null;
    }
    return entry || null;
  }
  function setResume(file, entry) {
    var all = loadResume();
    all[fingerprint(file)] = entry;
    saveResume(all);
  }
  function clearResume(file) {
    var all = loadResume();
    delete all[fingerprint(file)];
    saveResume(all);
  }

  // -------------------------------------------------------------------------
  // hashing, for dedupe
  // -------------------------------------------------------------------------

  function sha256Hex(file) {
    if (file.size > HASH_LIMIT || !global.crypto || !global.crypto.subtle) {
      return Promise.resolve(null);
    }
    return file.arrayBuffer()
      .then(function (buf) { return crypto.subtle.digest('SHA-256', buf); })
      .then(function (digest) {
        return Array.prototype.map.call(new Uint8Array(digest), function (b) {
          return b.toString(16).padStart(2, '0');
        }).join('');
      })
      .catch(function () { return null; });
  }

  // -------------------------------------------------------------------------
  // one authenticated PUT with progress
  // -------------------------------------------------------------------------

  function putWithProgress(url, blob, token, ticket, onProgress) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open('PUT', url, true);
      xhr.setRequestHeader('Authorization', 'Bearer ' + token);
      if (ticket) xhr.setRequestHeader('X-Upload-Ticket', ticket);
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total);
      };
      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 300) {
          var body = null;
          try { body = JSON.parse(xhr.responseText); } catch (e) { body = {}; }
          resolve(body);
        } else {
          var msg = xhr.responseText;
          try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) { /* keep raw */ }
          reject(new Error(msg || ('Upload failed: ' + xhr.status)));
        }
      };
      xhr.onerror = function () { reject(new Error('The network dropped during upload.')); };
      xhr.onabort = function () { reject(new Error('Upload cancelled.')); };
      xhr.send(blob);
    });
  }

  function delay(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }

  /** Retry a part a few times with backoff. A part is independent, so this is safe. */
  function sendPart(attempt, doSend) {
    return doSend().catch(function (err) {
      if (attempt >= MAX_PART_ATTEMPTS) throw err;
      return delay(Math.min(8000, 500 * Math.pow(2, attempt))).then(function () {
        return sendPart(attempt + 1, doSend);
      });
    });
  }

  // -------------------------------------------------------------------------
  // the upload
  // -------------------------------------------------------------------------

  /**
   * @param file      a File from an <input> or a drop
   * @param target    { type:'class'|'item'|'note'|'headstart', id:uuid }
   * @param opts      { category, title }
   * @param onEvent   called with { phase, loaded, total, percent, message }
   */
  function uploadFile(file, target, opts, onEvent) {
    opts = opts || {};
    var emit = function (phase, extra) {
      if (onEvent) onEvent(Object.assign({ phase: phase, file: file }, extra || {}));
    };

    var token, plan, sha;

    emit('hashing', { percent: 0 });

    return sha256Hex(file)
      .then(function (h) {
        sha = h;
        return BE.auth.accessToken();
      })
      .then(function (t) {
        token = t;
        if (!token) throw new Error('Sign in first.');

        var resumed = getResume(file);
        if (resumed && resumed.fileId) {
          emit('resuming', { message: 'Continuing where the last attempt stopped.' });
          return { resumed: true, plan: resumed };
        }
        return BE.callWorker('/uploads', {
          method: 'POST',
          body: JSON.stringify({
            filename: file.name,
            mimetype: file.type || 'application/octet-stream',
            size: file.size,
            sha256: sha,
          }),
        }).then(function (p) { return { resumed: false, plan: p }; });
      })
      .then(function (res) {
        plan = res.plan;

        // Those exact bytes are already in R2. Link them and move on.
        if (plan.duplicate) {
          emit('deduped', { percent: 100, message: 'Already stored. Linked without re-uploading.' });
          return { fileId: plan.fileId, skipped: true };
        }

        if (!res.resumed) {
          setResume(file, {
            fileId: plan.fileId, uploadId: plan.uploadId, multipart: plan.multipart,
            partSize: plan.partSize, partCount: plan.partCount, ticket: plan.ticket,
            parts: [], startedAt: Date.now(),
          });
        }

        emit('uploading', { percent: 0, loaded: 0, total: file.size });

        if (!plan.multipart) {
          return putWithProgress(
            BE.workerUrl + '/uploads/' + plan.fileId + '/body',
            file, token, plan.ticket,
            function (loaded, total) {
              emit('uploading', { loaded: loaded, total: total, percent: Math.round((loaded / total) * 100) });
            }
          ).then(function () { return { fileId: plan.fileId, parts: [] }; });
        }

        // ---- multipart ----
        var state = getResume(file) || { parts: [] };
        var done = {};
        (state.parts || []).forEach(function (p) { done[p.partNumber] = p; });

        var partSize = plan.partSize;
        var count = plan.partCount;
        var baseLoaded = Object.keys(done).length * partSize;

        var chain = Promise.resolve();
        for (var i = 1; i <= count; i++) {
          (function (partNumber) {
            chain = chain.then(function () {
              if (done[partNumber]) return null; // already accepted on an earlier attempt

              var start = (partNumber - 1) * partSize;
              var blob = file.slice(start, Math.min(start + partSize, file.size));

              return sendPart(1, function () {
                return putWithProgress(
                  BE.workerUrl + '/uploads/' + plan.fileId + '/parts/' + partNumber,
                  blob, token, plan.ticket,
                  function (loaded) {
                    var total = file.size;
                    var soFar = baseLoaded + (partNumber - 1 - Object.keys(done).length) * partSize + loaded;
                    emit('uploading', {
                      loaded: Math.min(soFar, total), total: total,
                      percent: Math.min(99, Math.round((soFar / total) * 100)),
                    });
                  }
                );
              }).then(function (body) {
                done[partNumber] = { partNumber: partNumber, etag: body.etag };
                var entry = getResume(file) || {};
                entry.parts = Object.keys(done).map(function (k) { return done[k]; });
                setResume(file, entry);
              });
            });
          })(i);
        }

        return chain.then(function () {
          return { fileId: plan.fileId, parts: Object.keys(done).map(function (k) { return done[k]; }) };
        });
      })
      .then(function (res) {
        if (res.skipped) return res.fileId;

        emit('finalising', { percent: 99, message: 'Confirming the upload.' });
        return BE.callWorker('/uploads/' + res.fileId + '/complete', {
          method: 'POST',
          body: JSON.stringify({ parts: res.parts || [] }),
        }).then(function () {
          clearResume(file);
          return res.fileId;
        });
      })
      .then(function (fileId) {
        // Only now does the file appear anywhere in the app.
        return BE.linkFile(fileId, target, {
          category: opts.category || guessCategory(file.name),
          title: opts.title || file.name,
        }).then(function (material) {
          emit('done', { percent: 100, fileId: fileId, material: material });
          return material;
        });
      })
      .catch(function (err) {
        emit('error', { message: err.message || String(err) });
        throw err;
      });
  }

  /** Same buckets the local app already uses, so imported files land where expected. */
  function guessCategory(name) {
    var n = (name || '').toLowerCase();
    if (/syllabus|outline/.test(n)) return 'syllabus';
    if (/slide|lecture|deck|\.pptx?$/.test(n)) return 'slides';
    if (/rubric|criteria|marking/.test(n)) return 'rubrics';
    if (/exam|midterm|final|past.?paper/.test(n)) return 'exams';
    if (/reading|chapter|article|paper/.test(n)) return 'readings';
    if (/project|proposal/.test(n)) return 'projects';
    return 'other';
  }

  /** Abandon an in-flight upload and release whatever R2 already holds. */
  function cancelUpload(file) {
    var entry = getResume(file);
    clearResume(file);
    if (!entry || !entry.fileId) return Promise.resolve();
    return BE.callWorker('/uploads/' + entry.fileId + '/abort', { method: 'POST' })
      .catch(function () { /* already gone is fine */ });
  }

  /** Uploads interrupted by a crash or a closed tab, so the UI can offer to resume. */
  function pendingUploads() {
    var all = loadResume();
    return Object.keys(all).map(function (k) {
      var parts = k.split(':');
      return {
        name: parts[0],
        size: Number(parts[1]),
        fileId: all[k].fileId,
        partsDone: (all[k].parts || []).length,
        partsTotal: all[k].partCount || 1,
        startedAt: all[k].startedAt,
      };
    });
  }

  BE.uploadFile = uploadFile;
  BE.cancelUpload = cancelUpload;
  BE.pendingUploads = pendingUploads;
  BE.guessCategory = guessCategory;
})(typeof window !== 'undefined' ? window : this);
