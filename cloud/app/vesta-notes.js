/**
 * Vesta note autosave, offline recovery and sync.
 *
 * Extends VESTA_BACKEND with the note-editing behaviour the local app cannot have:
 * a debounced save, a Saving/Saved indicator, a local copy that survives losing
 * the network or closing the tab, and an automatic flush when the connection is
 * back.
 *
 * What this deliberately does NOT do is merge two people typing at once. A Vesta
 * note belongs to one account, so the only real conflict is the same person on two
 * devices. That is handled by comparing revisions and telling the user, rather than
 * by silently overwriting whichever save arrived second.
 *
 * Load after vesta-cloud.js.
 */
(function (global) {
  'use strict';

  var BE = global.VESTA_BACKEND;
  if (!BE) return;

  var SAVE_DEBOUNCE_MS = 700;
  var DB_NAME = 'vesta-notes';
  var STORE = 'drafts';

  // -------------------------------------------------------------------------
  // IndexedDB: the local copy of anything not yet accepted by the server
  // -------------------------------------------------------------------------

  var dbPromise = null;
  function db() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise(function (resolve, reject) {
      if (!global.indexedDB) { reject(new Error('This browser has no IndexedDB.')); return; }
      var req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = function () {
        var d = req.result;
        if (!d.objectStoreNames.contains(STORE)) {
          d.createObjectStore(STORE, { keyPath: 'noteId' });
        }
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    }).catch(function (err) {
      // A browser in private mode may refuse. Autosave to the server still works;
      // only crash recovery is lost, so this is a degradation and not a failure.
      console.warn('Vesta: local draft storage unavailable.', err);
      return null;
    });
    return dbPromise;
  }

  function idbPut(draft) {
    return db().then(function (d) {
      if (!d) return null;
      return new Promise(function (resolve, reject) {
        var tx = d.transaction(STORE, 'readwrite');
        tx.objectStore(STORE).put(draft);
        tx.oncomplete = function () { resolve(draft); };
        tx.onerror = function () { reject(tx.error); };
      });
    });
  }

  function idbGet(noteId) {
    return db().then(function (d) {
      if (!d) return null;
      return new Promise(function (resolve, reject) {
        var tx = d.transaction(STORE, 'readonly');
        var req = tx.objectStore(STORE).get(noteId);
        req.onsuccess = function () { resolve(req.result || null); };
        req.onerror = function () { reject(req.error); };
      });
    });
  }

  function idbAll() {
    return db().then(function (d) {
      if (!d) return [];
      return new Promise(function (resolve, reject) {
        var tx = d.transaction(STORE, 'readonly');
        var req = tx.objectStore(STORE).getAll();
        req.onsuccess = function () { resolve(req.result || []); };
        req.onerror = function () { reject(req.error); };
      });
    });
  }

  function idbDelete(noteId) {
    return db().then(function (d) {
      if (!d) return null;
      return new Promise(function (resolve) {
        var tx = d.transaction(STORE, 'readwrite');
        tx.objectStore(STORE).delete(noteId);
        tx.oncomplete = resolve;
        tx.onerror = resolve;
      });
    });
  }

  // -------------------------------------------------------------------------
  // save queue
  // -------------------------------------------------------------------------

  var timers = {};       // noteId -> debounce timer
  var inFlight = {};     // noteId -> true while a save is on the wire
  var statusFn = null;   // the UI's Saving/Saved indicator

  function setStatus(noteId, state, detail) {
    if (statusFn) statusFn(noteId, state, detail || null);
  }

  function online() {
    return typeof navigator === 'undefined' || navigator.onLine !== false;
  }

  /**
   * Record the edit locally first, then schedule the server save.
   *
   * Writing to IndexedDB before the network attempt is what makes a dropped
   * connection or a closed tab survivable: the text is already on disk, and the
   * queue below pushes it up when the network returns.
   */
  function queueSave(noteId, patch) {
    return idbGet(noteId).then(function (existing) {
      var draft = {
        noteId: noteId,
        patch: Object.assign({}, existing && existing.patch, patch),
        savedAt: Date.now(),
        dirty: true,
      };
      return idbPut(draft);
    }).then(function () {
      setStatus(noteId, 'saving');
      if (timers[noteId]) clearTimeout(timers[noteId]);
      timers[noteId] = setTimeout(function () { flushNote(noteId); }, SAVE_DEBOUNCE_MS);
    });
  }

  function flushNote(noteId) {
    if (inFlight[noteId]) {
      // A save is already going. Re-arm so the newest text is not left behind.
      if (timers[noteId]) clearTimeout(timers[noteId]);
      timers[noteId] = setTimeout(function () { flushNote(noteId); }, SAVE_DEBOUNCE_MS);
      return Promise.resolve();
    }
    if (!online()) { setStatus(noteId, 'offline'); return Promise.resolve(); }

    return idbGet(noteId).then(function (draft) {
      if (!draft || !draft.dirty) { setStatus(noteId, 'saved'); return null; }

      inFlight[noteId] = true;
      var patch = draft.patch;

      return BE.updateNote(noteId, patch)
        .then(function (saved) {
          inFlight[noteId] = false;
          // Only drop the local copy if nothing was typed while it was in flight.
          return idbGet(noteId).then(function (current) {
            var changedSince = current && current.savedAt > draft.savedAt;
            if (!changedSince) {
              return idbDelete(noteId).then(function () {
                setStatus(noteId, 'saved', saved);
                return saved;
              });
            }
            setStatus(noteId, 'saving');
            return flushNote(noteId);
          });
        })
        .catch(function (err) {
          inFlight[noteId] = false;
          if (!online()) { setStatus(noteId, 'offline'); return null; }
          setStatus(noteId, 'error', err.message || String(err));
          // Keep the draft. The retry below or the next edit will try again.
          return null;
        });
    });
  }

  /** Push everything still pending. Called on reconnect and at startup. */
  function flushAll() {
    if (!online()) return Promise.resolve([]);
    return idbAll().then(function (drafts) {
      return drafts.reduce(function (chain, d) {
        return chain.then(function () { return flushNote(d.noteId); });
      }, Promise.resolve());
    });
  }

  /**
   * Anything still on disk at startup was typed but never accepted by the server,
   * because the tab closed or the network went away. The UI should offer to
   * restore these rather than silently discarding what the user wrote.
   */
  function unsyncedDrafts() {
    return idbAll().then(function (drafts) {
      return drafts.filter(function (d) { return d.dirty; }).map(function (d) {
        return {
          noteId: d.noteId,
          savedAt: d.savedAt,
          title: d.patch && d.patch.title,
          text: d.patch && d.patch.text,
        };
      });
    });
  }

  /**
   * Compare the local draft against what the server holds. Used when opening a
   * note, so a copy typed offline on this device is never quietly overwritten by
   * an older copy from the server.
   */
  function reconcile(noteId, serverNote) {
    return idbGet(noteId).then(function (draft) {
      if (!draft || !draft.dirty) return { note: serverNote, recovered: false };
      var localText = draft.patch && draft.patch.text;
      if (localText === undefined || localText === serverNote.text) {
        return { note: serverNote, recovered: false };
      }
      return {
        note: Object.assign({}, serverNote, draft.patch),
        recovered: true,
        serverVersion: serverNote,
        localSavedAt: draft.savedAt,
      };
    });
  }

  if (typeof global.addEventListener === 'function') {
    global.addEventListener('online', function () { flushAll(); });
    global.addEventListener('offline', function () {
      Object.keys(timers).forEach(function (id) { setStatus(id, 'offline'); });
    });
    // A closing tab gets one last synchronous chance. The draft is already on
    // disk by this point, so this is a best effort, not the safety net.
    global.addEventListener('pagehide', function () {
      Object.keys(timers).forEach(function (id) {
        if (timers[id]) { clearTimeout(timers[id]); flushNote(id); }
      });
    });
  }

  BE.notes = {
    save: queueSave,
    flush: flushNote,
    flushAll: flushAll,
    unsyncedDrafts: unsyncedDrafts,
    reconcile: reconcile,
    discardDraft: idbDelete,
    onStatus: function (fn) { statusFn = fn; },
    debounceMs: SAVE_DEBOUNCE_MS,
  };
})(typeof window !== 'undefined' ? window : this);
