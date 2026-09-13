/**
 * Vesta cloud backend adapter.
 *
 * The frontend already funnels every read and write through a thin data layer
 * (addClass, updateItem, loadState, and so on). This file provides the same
 * functions against Supabase instead of the local Flask server, and hands back
 * state in exactly the shape the UI already expects. No view code changes.
 *
 * Load order in the page:
 *   <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
 *   <script src="config.js"></script>          // your project URL and keys
 *   <script src="vesta-cloud.js"></script>
 *
 * If config.js is absent, VESTA_BACKEND stays null and the app keeps using Flask.
 * That is what lets the local app go on working untouched during the migration.
 */
(function (global) {
  'use strict';

  var cfg = global.VESTA_CONFIG;
  if (!cfg || !cfg.supabaseUrl || !cfg.supabaseAnonKey) {
    global.VESTA_BACKEND = null;
    return;
  }
  if (!global.supabase || !global.supabase.createClient) {
    console.error('Vesta: supabase-js did not load. Falling back to the local server.');
    global.VESTA_BACKEND = null;
    return;
  }

  var sb = global.supabase.createClient(cfg.supabaseUrl, cfg.supabaseAnonKey, {
    auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
  });

  var WORKER = (cfg.workerUrl || '').replace(/\/+$/, '');

  // Signed, short-lived, and appended to every file URL so <img> and <iframe> can
  // load a private file without an Authorization header they are unable to send.
  var downloadToken = null;
  var downloadTokenExpires = 0;

  // -------------------------------------------------------------------------
  // helpers
  // -------------------------------------------------------------------------

  /** Throw on error so callers can rely on a plain promise rejection. */
  function unwrap(res) {
    if (res.error) throw new Error(res.error.message || String(res.error));
    return res.data;
  }

  /** Strip keys whose value is undefined, so a partial update stays partial. */
  function defined(obj) {
    var out = {};
    Object.keys(obj).forEach(function (k) {
      if (obj[k] !== undefined) out[k] = obj[k];
    });
    return out;
  }

  function emptyToNull(v) {
    return v === '' || v === undefined ? null : v;
  }

  // -------------------------------------------------------------------------
  // row shape translation
  //
  // Postgres columns are snake_case and a few were renamed during the move
  // (notes.text became notes.body, materials became files plus attachments).
  // Everything below converts at the boundary so the UI never sees a column name.
  // -------------------------------------------------------------------------

  function classOut(row, kids) {
    return {
      id: row.id,
      code: row.code || '',
      name: row.name || '',
      professor: row.professor || '',
      color: row.color || '',
      notes: row.notes || '',
      gradeScale: row.grade_scale || null,
      website: row.website || '',
      createdAt: row.created_at,
      schedule: kids.schedule,
      materials: kids.materials,
      notesList: kids.notesList,
      noteFolders: kids.noteFolders,
      syllabus: kids.syllabus,
    };
  }

  function classIn(d) {
    return defined({
      code: d.code,
      name: d.name,
      professor: d.professor,
      color: d.color,
      notes: d.notes,
      website: d.website,
      grade_scale: d.gradeScale,
    });
  }

  function scheduleOut(r) {
    // the id matters: a note can be linked to a specific lecture
    return { id: r.id, day: r.day, start: r.start_time || '', end: r.end_time || '',
             location: r.location || '' };
  }

  function itemOut(row, subtasks, headstarts, rubric) {
    return {
      id: row.id,
      classId: row.class_id,
      title: row.title || '',
      type: row.type || 'other',
      dueDate: row.due_date,
      dueTime: row.due_time,
      status: row.status || 'todo',
      completedAt: row.completed_at,
      weight: row.weight,
      score: row.score,
      notes: row.notes || '',
      focusSeconds: row.focus_seconds || 0,
      createdAt: row.created_at,
      subtasks: subtasks || [],
      headstarts: headstarts || [],
      rubric: rubric || null,
    };
  }

  function itemIn(d) {
    return defined({
      class_id: emptyToNull(d.classId),
      title: d.title,
      type: d.type,
      due_date: emptyToNull(d.dueDate),
      due_time: emptyToNull(d.dueTime),
      status: d.status,
      completed_at: emptyToNull(d.completedAt),
      weight: d.weight === '' ? null : d.weight,
      score: d.score === '' ? null : d.score,
      notes: d.notes,
      focus_seconds: d.focusSeconds,
    });
  }

  function eventOut(r) {
    return {
      id: r.id,
      classId: r.class_id,
      title: r.title || '',
      kind: r.kind || 'other',
      date: r.date,
      start: r.start_time || '',
      end: r.end_time || '',
      allDay: !!r.all_day,
      location: r.location || '',
      notes: r.notes || '',
      createdAt: r.created_at,
    };
  }

  function eventIn(d) {
    return defined({
      class_id: emptyToNull(d.classId),
      title: d.title,
      kind: d.kind,
      date: emptyToNull(d.date),
      start_time: emptyToNull(d.start),
      end_time: emptyToNull(d.end),
      all_day: d.allDay,
      location: d.location,
      notes: d.notes,
    });
  }

  function noteOut(r) {
    return {
      id: r.id,
      classId: r.class_id,
      folderId: r.folder_id,
      linkedItemId: r.linked_item_id,
      title: r.title || '',
      // The UI still calls this `text`; only the column was renamed.
      text: r.body || '',
      revision: r.revision,
      createdAt: r.created_at,
      updatedAt: r.updated_at,
    };
  }

  function noteIn(d) {
    return defined({
      class_id: emptyToNull(d.classId),
      folder_id: emptyToNull(d.folderId),
      linked_item_id: emptyToNull(d.linkedItemId),
      title: d.title,
      body: d.text,
    });
  }

  /**
   * An attachment plus, when it points at one, its file. The UI's old `material`
   * was one row that held both the link and the bytes, so this flattens them back
   * into that shape.
   */
  function fileUrl(fileId) {
    return WORKER + '/files/' + fileId + (downloadToken ? '?t=' + encodeURIComponent(downloadToken) : '');
  }

  function materialOut(a) {
    var f = a.files || null;
    return {
      id: a.id,
      classId: a.class_id,
      category: a.category || 'other',
      title: a.title || (f && f.filename) || 'Untitled',
      kind: f ? 'file' : 'link',
      // The UI reads `url` for both kinds: it is the href, the iframe src and the
      // image src. For a stored file that is the Worker, carrying a read token.
      url: f ? fileUrl(f.id) : (a.url || null),
      hasText: f ? !!f.has_text : false,
      fileId: f ? f.id : null,
      filename: f ? f.filename : null,
      mimetype: f ? f.mimetype : null,
      size: f ? f.size_bytes : null,
      status: f ? f.status : 'complete',
      starred: !!a.starred,
      createdAt: a.created_at,
    };
  }

  function rubricOut(r) {
    if (!r) return null;
    return {
      id: r.id,
      materialId: r.file_id,
      itemId: r.item_id,
      totalPoints: r.total_points,
      criteria: r.criteria || [],
      createdAt: r.created_at,
    };
  }

  // -------------------------------------------------------------------------
  // auth
  // -------------------------------------------------------------------------

  var auth = {
    session: null,

    getUser: function () {
      return auth.session && auth.session.user ? auth.session.user : null;
    },

    init: function (onChange) {
      return sb.auth.getSession().then(function (res) {
        auth.session = res.data ? res.data.session : null;
        sb.auth.onAuthStateChange(function (_event, session) {
          auth.session = session;
          if (onChange) onChange(session);
        });
        return auth.session;
      });
    },

    signInWithGoogle: function () {
      return sb.auth.signInWithOAuth({
        provider: 'google',
        options: { redirectTo: global.location.origin + global.location.pathname },
      }).then(unwrap);
    },

    signInWithPassword: function (email, password) {
      return sb.auth.signInWithPassword({ email: email, password: password }).then(function (res) {
        if (res.error) throw new Error(friendlyAuthError(res.error));
        return res.data;
      });
    },

    signUp: function (email, password) {
      return sb.auth.signUp({ email: email, password: password }).then(function (res) {
        if (res.error) throw new Error(friendlyAuthError(res.error));
        return res.data;
      });
    },

    signOut: function () {
      return sb.auth.signOut().then(function () { auth.session = null; });
    },

    /**
     * Send a reset link. Supabase deliberately answers the same way whether or not
     * the address has an account, so this cannot be used to find out who is
     * registered. Say "check your email" either way.
     */
    resetPassword: function (email) {
      return sb.auth.resetPasswordForEmail(email, {
        redirectTo: global.location.origin + global.location.pathname,
      }).then(function (res) {
        if (res.error) throw new Error(friendlyAuthError(res.error));
        return true;
      });
    },

    /** Set a new password. Works from a recovery link, or when already signed in. */
    updatePassword: function (password) {
      return sb.auth.updateUser({ password: password }).then(function (res) {
        if (res.error) throw new Error(friendlyAuthError(res.error));
        return res.data;
      });
    },

    /** Fires when the user arrives from a reset link, so the UI can ask for a new one. */
    onPasswordRecovery: function (fn) {
      sb.auth.onAuthStateChange(function (event) {
        if (event === 'PASSWORD_RECOVERY') fn();
      });
    },

    /** The row in `profiles` for the signed-in user. */
    getProfile: function () {
      return sb.from('profiles').select('*').limit(1).then(unwrap)
        .then(function (rows) { return rows && rows[0] ? rows[0] : null; });
    },

    updateProfile: function (patch) {
      var user = auth.getUser();
      if (!user) return Promise.reject(new Error('Sign in first.'));
      return sb.from('profiles').update(defined({
        display_name: patch.displayName,
        avatar_url: patch.avatarUrl,
      })).eq('id', user.id).select().then(unwrap)
        .then(function (rows) { return rows && rows[0]; });
    },

    /** The bearer token the Worker needs. Refreshed by supabase-js as required. */
    accessToken: function () {
      return sb.auth.getSession().then(function (res) {
        return res.data && res.data.session ? res.data.session.access_token : null;
      });
    },
  };

  /**
   * The signup gate raises a database error when an email is not on the allowlist.
   * Supabase surfaces that as an opaque 500, so translate it into something a
   * person can act on.
   */
  function friendlyAuthError(error) {
    var msg = String(error.message || error);
    if (/not been invited/i.test(msg) || /Database error saving new user/i.test(msg)) {
      return 'That email has not been invited to Vesta. Ask Saif to add you.';
    }
    if (/Invalid login credentials/i.test(msg)) {
      return 'That email and password did not match.';
    }
    if (/Email not confirmed/i.test(msg)) {
      return 'Check your email and confirm the address first.';
    }
    if (/should be at least/i.test(msg) || /Password.*(short|6|characters)/i.test(msg)) {
      return 'That password is too short. Use at least six characters.';
    }
    if (/New password should be different/i.test(msg)) {
      return 'That is the password you already have. Choose a different one.';
    }
    if (/For security purposes|rate limit|too many/i.test(msg)) {
      return 'Too many attempts just now. Wait a minute and try again.';
    }
    return msg;
  }

  // -------------------------------------------------------------------------
  // state
  //
  // Nine parallel queries, assembled into the single object the UI expects. Row
  // Level Security scopes every one of them to the signed-in user, so none of
  // these queries mentions a user id.
  // -------------------------------------------------------------------------

  /**
   * Fetch a read token if we do not hold a live one. Done before the state load so
   * every file URL built below is immediately usable.
   */
  function ensureDownloadToken() {
    if (!WORKER) return Promise.resolve(null);
    if (downloadToken && Date.now() < downloadTokenExpires - 60000) return Promise.resolve(downloadToken);
    return backend.callWorker('/session/download-token', { method: 'GET' })
      .then(function (res) {
        downloadToken = res.token;
        downloadTokenExpires = Date.now() + (res.expiresIn || 3600) * 1000;
        return downloadToken;
      })
      .catch(function () {
        // A missing token is not fatal: everything except opening a file still works.
        downloadToken = null;
        return null;
      });
  }

  function loadState() {
    return ensureDownloadToken().then(function () { return Promise.all([
      sb.from('classes').select('*').order('created_at').then(unwrap),
      sb.from('schedule_entries').select('*').then(unwrap),
      sb.from('items').select('*').order('created_at').then(unwrap),
      sb.from('subtasks').select('*').order('sort_order').then(unwrap),
      sb.from('events').select('*').order('date').then(unwrap),
      sb.from('notes').select('*').is('deleted_at', null).order('created_at').then(unwrap),
      sb.from('note_folders').select('*').order('created_at').then(unwrap),
      sb.from('syllabus_topics').select('*').order('sort_order').then(unwrap),
      sb.from('attachments')
        .select('*, files(id,filename,mimetype,size_bytes,status,has_text)')
        .order('created_at').then(unwrap),
      sb.from('headstarts').select('item_id, kind, status').then(unwrap),
      sb.from('rubrics').select('*').then(unwrap),
      sb.from('semesters').select('*').eq('is_active', true).limit(1).then(unwrap),
    ]).then(function (r) {
      var classes = r[0], schedule = r[1], items = r[2], subtasks = r[3],
          events = r[4], notes = r[5], folders = r[6], topics = r[7],
          attachments = r[8], headstarts = r[9], rubrics = r[10], semesters = r[11];

      var by = function (rows, key) {
        var m = {};
        rows.forEach(function (row) {
          var k = row[key];
          (m[k] = m[k] || []).push(row);
        });
        return m;
      };

      var schedById = by(schedule, 'class_id');
      var notesById = by(notes, 'class_id');
      var foldersById = by(folders, 'class_id');
      var topicsById = by(topics, 'class_id');
      var attachById = by(attachments, 'class_id');
      var subsById = by(subtasks, 'item_id');
      var hsById = by(headstarts, 'item_id');
      var rubricByItem = {};
      rubrics.forEach(function (rb) { if (rb.item_id) rubricByItem[rb.item_id] = rb; });

      return {
        classes: classes.map(function (c) {
          return classOut(c, {
            schedule: (schedById[c.id] || []).map(scheduleOut),
            materials: (attachById[c.id] || []).map(materialOut),
            notesList: (notesById[c.id] || []).map(noteOut),
            noteFolders: (foldersById[c.id] || []).map(function (f) {
              return { id: f.id, name: f.name };
            }),
            syllabus: (topicsById[c.id] || []).map(function (t) {
              return { id: t.id, title: t.title, done: !!t.done };
            }),
          });
        }),
        items: items.map(function (it) {
          return itemOut(
            it,
            (subsById[it.id] || []).map(function (s) {
              return { id: s.id, title: s.title, done: !!s.done };
            }),
            (hsById[it.id] || []).map(function (h) {
              return { kind: h.kind, status: h.status };
            }),
            rubricOut(rubricByItem[it.id])
          );
        }),
        events: events.map(eventOut),
        term: semesters && semesters[0]
          ? {
              name: semesters[0].name || '',
              startDate: semesters[0].start_date || '',
              endDate: semesters[0].end_date || '',
            }
          : { name: '', startDate: '', endDate: '' },
      };
    }); });
  }

  // -------------------------------------------------------------------------
  // writes
  // -------------------------------------------------------------------------

  var ins = function (table, row) {
    return sb.from(table).insert(row).select().then(unwrap).then(function (d) { return d[0]; });
  };
  var upd = function (table, id, row) {
    return sb.from(table).update(row).eq('id', id).select().then(unwrap).then(function (d) { return d[0]; });
  };
  var del = function (table, id) {
    return sb.from(table).delete().eq('id', id).then(unwrap);
  };

  /** Schedule rows have no stable ids in the UI, so a save replaces the set. */
  function replaceSchedule(classId, schedule) {
    return sb.from('schedule_entries').delete().eq('class_id', classId).then(unwrap).then(function () {
      if (!schedule || !schedule.length) return null;
      return sb.from('schedule_entries').insert(schedule.map(function (s) {
        return {
          class_id: classId,
          day: Number(s.day) || 0,
          start_time: emptyToNull(s.start),
          end_time: emptyToNull(s.end),
          location: s.location || '',
        };
      })).then(unwrap);
    });
  }

  function replaceSubtasks(itemId, subtasks) {
    return sb.from('subtasks').delete().eq('item_id', itemId).then(unwrap).then(function () {
      if (!subtasks || !subtasks.length) return null;
      return sb.from('subtasks').insert(subtasks.map(function (s, i) {
        return { item_id: itemId, title: s.title || '', done: !!s.done, sort_order: i };
      })).then(unwrap);
    });
  }

  /** The active semester stands in for the old single term row. */
  function activeSemesterId() {
    return sb.from('semesters').select('id').eq('is_active', true).limit(1).then(unwrap)
      .then(function (rows) { return rows && rows[0] ? rows[0].id : null; });
  }

  // -------------------------------------------------------------------------
  // the backend surface, matching the local one function for function
  // -------------------------------------------------------------------------

  var backend = {
    kind: 'supabase',
    client: sb,
    auth: auth,
    workerUrl: WORKER,

    loadState: loadState,

    addClass: function (d) {
      return activeSemesterId().then(function (sid) {
        var row = classIn(d);
        row.semester_id = sid;
        return ins('classes', row);
      }).then(function (c) {
        return replaceSchedule(c.id, d.schedule).then(function () { return c; });
      });
    },
    updateClass: function (id, d) {
      return upd('classes', id, classIn(d)).then(function (c) {
        return d.schedule ? replaceSchedule(id, d.schedule).then(function () { return c; }) : c;
      });
    },
    deleteClass: function (id) { return del('classes', id); },

    addItem: function (d) {
      return ins('items', itemIn(d)).then(function (it) {
        return d.subtasks ? replaceSubtasks(it.id, d.subtasks).then(function () { return it; }) : it;
      });
    },
    updateItem: function (id, d) {
      return upd('items', id, itemIn(d)).then(function (it) {
        return d.subtasks ? replaceSubtasks(id, d.subtasks).then(function () { return it; }) : it;
      });
    },
    deleteItem: function (id) { return del('items', id); },

    addEvent: function (d) { return ins('events', eventIn(d)); },
    updateEvent: function (id, d) { return upd('events', id, eventIn(d)); },
    deleteEvent: function (id) { return del('events', id); },

    addNote: function (classId, d) {
      var row = noteIn(d); row.class_id = classId;
      return ins('notes', row).then(noteOut);
    },
    updateNote: function (id, d) { return upd('notes', id, noteIn(d)).then(noteOut); },
    /**
     * Move a note to the Trash instead of destroying it. The nightly retention
     * job removes it for good after 30 days.
     */
    removeNote: function (id) {
      return upd('notes', id, { deleted_at: new Date().toISOString() });
    },
    restoreNote: function (id) { return upd('notes', id, { deleted_at: null }); },
    listTrashedNotes: function () {
      return sb.from('notes').select('*').not('deleted_at', 'is', null)
        .order('deleted_at', { ascending: false }).then(unwrap).then(function (rows) {
          return rows.map(noteOut);
        });
    },
    listNoteVersions: function (noteId) {
      return sb.from('note_versions').select('*').eq('note_id', noteId)
        .order('created_at', { ascending: false }).then(unwrap);
    },
    restoreNoteVersion: function (noteId, versionId) {
      return sb.from('note_versions').select('*').eq('id', versionId).limit(1).then(unwrap)
        .then(function (rows) {
          if (!rows || !rows.length) throw new Error('That version is no longer available.');
          return upd('notes', noteId, { title: rows[0].title, body: rows[0].body });
        }).then(noteOut);
    },

    addNoteFolder: function (classId, name) {
      return ins('note_folders', { class_id: classId, name: name || 'Untitled folder' });
    },
    renameNoteFolder: function (id, name) { return upd('note_folders', id, { name: name }); },
    deleteNoteFolder: function (id) { return del('note_folders', id); },

    addTopic: function (classId, title) {
      return ins('syllabus_topics', { class_id: classId, title: title || '' });
    },
    toggleTopic: function (id, done) { return upd('syllabus_topics', id, { done: !!done }); },
    removeTopic: function (id) { return del('syllabus_topics', id); },

    addMaterialLink: function (classId, d) {
      return ins('attachments', {
        class_id: classId,
        url: d.url,
        title: d.title || d.url,
        category: d.category || 'other',
      }).then(materialOut);
    },
    updateMaterial: function (id, d) {
      return upd('attachments', id, defined({
        title: d.title, category: d.category, starred: d.starred,
      })).then(materialOut);
    },
    /**
     * Unlink from this place. The underlying file survives if it is still linked
     * somewhere else, which is the point of storing it once.
     */
    removeMaterial: function (id) { return del('attachments', id); },

    /** Link a file that already exists to somewhere new. No bytes move. */
    linkFile: function (fileId, target, opts) {
      var row = { file_id: fileId, category: (opts && opts.category) || 'other',
                  title: (opts && opts.title) || '' };
      row[target.type + '_id'] = target.id;   // class_id | item_id | note_id | headstart_id
      return ins('attachments', row).then(materialOut);
    },

    /** Everything the user has stored, for the global Files view. */
    listAllFiles: function () {
      return sb.from('files').select('*').is('deleted_at', null).eq('status', 'complete')
        .order('created_at', { ascending: false }).then(unwrap);
    },
    trashFile: function (fileId) {
      return upd('files', fileId, { deleted_at: new Date().toISOString() });
    },
    restoreFile: function (fileId) { return upd('files', fileId, { deleted_at: null }); },

    saveTermSettings: function (d) {
      return activeSemesterId().then(function (sid) {
        var row = { name: d.name || '', start_date: emptyToNull(d.startDate), end_date: emptyToNull(d.endDate) };
        if (sid) return upd('semesters', sid, row);
        row.is_active = true;
        return ins('semesters', row);
      });
    },

    /**
     * Focus time is appended, never overwritten, so two devices logging study
     * time in the same minute both count. The running total on the item is kept
     * in step for the parts of the UI that read it directly.
     */
    logFocusTime: function (itemId, seconds) {
      return ins('focus_sessions', { item_id: itemId, seconds: seconds })
        .then(function () {
          return sb.from('items').select('focus_seconds').eq('id', itemId).limit(1).then(unwrap);
        })
        .then(function (rows) {
          var current = rows && rows[0] ? rows[0].focus_seconds || 0 : 0;
          return upd('items', itemId, { focus_seconds: current + seconds });
        });
    },

    fetchHeadstarts: function (itemId) {
      return sb.from('headstarts').select('*').eq('item_id', itemId).then(unwrap);
    },
    updateHeadstart: function (id, d) {
      return upd('headstarts', id, defined({ content: d.content, status: d.status, instructions: d.instructions }));
    },
    deleteHeadstart: function (id) { return del('headstarts', id); },
    generateHeadstart: function (itemId, kind, mode, instructions) {
      return backend.callWorker('/ai/headstart', {
        method: 'POST',
        body: JSON.stringify({ itemId: itemId, kind: kind, instructions: instructions || '' }),
      });
    },

    fetchRubric: function (fileId) {
      return sb.from('rubrics').select('*').eq('file_id', fileId).limit(1).then(unwrap)
        .then(function (rows) { return rubricOut(rows && rows[0]); });
    },
    updateRubric: function (id, d) {
      return upd('rubrics', id, defined({ criteria: d.criteria, total_points: d.totalPoints }));
    },
    deleteRubricRecord: function (id) { return del('rubrics', id); },

    /** Authenticated call to the Cloudflare Worker. */
    callWorker: function (path, options) {
      if (!WORKER) return Promise.reject(new Error('No Worker URL configured.'));
      return auth.accessToken().then(function (token) {
        if (!token) throw new Error('Sign in first.');
        var opts = options || {};
        var headers = Object.assign(
          { Authorization: 'Bearer ' + token },
          opts.body && typeof opts.body === 'string' ? { 'Content-Type': 'application/json' } : {},
          opts.headers || {}
        );
        return fetch(WORKER + path, Object.assign({}, opts, { headers: headers }));
      }).then(function (res) {
        return res.text().then(function (text) {
          var data = null;
          try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
          if (!res.ok) throw new Error((data && data.error) || ('Request failed: ' + res.status));
          return data;
        });
      });
    },

    /**
     * Live updates across devices. Postgres publishes row changes, RLS applies to
     * the stream too, so a subscription only ever delivers this user's rows.
     */
    subscribe: function (onChange) {
      var channel = sb.channel('vesta-sync');
      ['classes', 'items', 'events', 'notes', 'note_folders',
       'syllabus_topics', 'attachments', 'files', 'schedule_entries', 'subtasks']
        .forEach(function (table) {
          channel.on('postgres_changes', { event: '*', schema: 'public', table: table }, function (payload) {
            onChange(table, payload);
          });
        });
      channel.subscribe();
      return function () { sb.removeChannel(channel); };
    },
  };

  global.VESTA_BACKEND = backend;
})(typeof window !== 'undefined' ? window : this);
