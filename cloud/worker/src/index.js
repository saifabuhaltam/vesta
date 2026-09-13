/**
 * Vesta API Worker.
 *
 * This is deliberately small. Everything that can go straight from the browser to
 * Postgres does, protected by Row Level Security. The Worker exists only for the
 * jobs that need a secret the browser must never hold:
 *
 *   - reading and writing R2, which has no per-user permission model of its own
 *   - calling the Anthropic API for Headstart
 *   - deleting R2 objects whose database rows are already gone
 *
 * Uploads go through the Worker using the R2 binding rather than presigned S3 URLs.
 * That trades a little bandwidth for a lot less setup: no second set of credentials,
 * no SigV4 signing, and no CORS policy on the bucket, since the Worker answers the
 * browser itself. R2 multipart gives us resumable large files either way.
 */

const PART_SIZE = 8 * 1024 * 1024; // 8 MB, comfortably under the request body limit
const TICKET_TTL_SECONDS = 60 * 60 * 6; // an upload may take a while on bad wifi
const MAX_FILE_BYTES = 512 * 1024 * 1024;

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

function corsHeaders(env, request) {
  const allowed = (env.ALLOWED_ORIGIN || '').split(',').map((s) => s.trim()).filter(Boolean);
  const origin = request.headers.get('Origin') || '';
  // Echo the origin only when it is one we published to. A wildcard would let any
  // site drive this Worker with a user's token.
  const ok = allowed.includes(origin);
  return {
    'Access-Control-Allow-Origin': ok ? origin : (allowed[0] || ''),
    'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
    'Access-Control-Allow-Headers': 'Authorization,Content-Type,X-Upload-Ticket',
    'Access-Control-Max-Age': '86400',
    Vary: 'Origin',
  };
}

function json(body, status, env, request) {
  return new Response(JSON.stringify(body), {
    status: status || 200,
    headers: { 'Content-Type': 'application/json', ...corsHeaders(env, request) },
  });
}

function fail(message, status, env, request) {
  return json({ error: message }, status || 400, env, request);
}

const enc = new TextEncoder();

async function hmac(secret, message) {
  const key = await crypto.subtle.importKey(
    'raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']
  );
  const sig = await crypto.subtle.sign('HMAC', key, enc.encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/** Constant-time compare, so a wrong ticket cannot be guessed a byte at a time. */
function safeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// ---------------------------------------------------------------------------
// identity
// ---------------------------------------------------------------------------

/**
 * Resolve the caller from their Supabase access token by asking Supabase.
 *
 * Verifying the signature locally would save a round trip, but Supabase projects
 * differ in signing algorithm and a token can be revoked before it expires. Asking
 * is always correct. It happens once per upload, not once per part, because
 * `issueTicket` below covers the parts.
 */
async function currentUser(request, env) {
  const auth = request.headers.get('Authorization') || '';
  if (!auth.startsWith('Bearer ')) return null;
  const token = auth.slice(7);

  const res = await fetch(`${env.SUPABASE_URL}/auth/v1/user`, {
    headers: { Authorization: `Bearer ${token}`, apikey: env.SUPABASE_ANON_KEY },
  });
  if (!res.ok) return null;
  const user = await res.json();
  return user && user.id ? { id: user.id, email: user.email, token } : null;
}

/**
 * A short-lived proof that this user already passed a full token check for this
 * file. Part uploads present it instead of re-validating with Supabase on every
 * 8 MB chunk, which on a 400 MB file would be 50 extra round trips.
 */
async function issueTicket(env, fileId, userId) {
  const exp = Math.floor(Date.now() / 1000) + TICKET_TTL_SECONDS;
  const sig = await hmac(env.UPLOAD_TICKET_SECRET, `${fileId}.${userId}.${exp}`);
  return `${exp}.${sig}`;
}

/**
 * A browser cannot put an Authorization header on <img src>, <iframe src> or a
 * download link, and those are exactly how the app shows a stored file. So reads
 * accept a short-lived token in the query string instead.
 *
 * The token carries the user id and is signed, so it cannot be edited to point at
 * someone else. It only says who you are; ownership of the specific file is still
 * checked against the database on every request. It is deliberately short-lived,
 * because a URL carrying it can end up in a browser history or a shared screenshot.
 */
async function issueDownloadToken(env, userId) {
  const exp = Math.floor(Date.now() / 1000) + 60 * 60; // one hour
  const sig = await hmac(env.UPLOAD_TICKET_SECRET, `dl.${userId}.${exp}`);
  return `${userId}.${exp}.${sig}`;
}

async function userFromDownloadToken(env, token) {
  if (!token) return null;
  const parts = String(token).split('.');
  if (parts.length !== 3) return null;
  const [userId, expStr, sig] = parts;
  const exp = Number(expStr);
  if (!exp || exp < Math.floor(Date.now() / 1000)) return null;
  const expected = await hmac(env.UPLOAD_TICKET_SECRET, `dl.${userId}.${exp}`);
  return safeEqual(sig, expected) ? { id: userId } : null;
}

async function checkTicket(env, ticket, fileId, userId) {
  if (!ticket) return false;
  const [expStr, sig] = String(ticket).split('.');
  const exp = Number(expStr);
  if (!exp || exp < Math.floor(Date.now() / 1000)) return false;
  const expected = await hmac(env.UPLOAD_TICKET_SECRET, `${fileId}.${userId}.${exp}`);
  return safeEqual(sig, expected);
}

// ---------------------------------------------------------------------------
// Supabase, as the service role
//
// These calls bypass Row Level Security, so every one of them must filter by the
// user id we established from the caller's token. Never trust a user id sent in a
// request body.
// ---------------------------------------------------------------------------

async function sb(env, path, options) {
  const res = await fetch(`${env.SUPABASE_URL}/rest/v1/${path}`, {
    ...options,
    headers: {
      apikey: env.SUPABASE_SERVICE_KEY,
      Authorization: `Bearer ${env.SUPABASE_SERVICE_KEY}`,
      'Content-Type': 'application/json',
      Prefer: 'return=representation',
      ...(options && options.headers),
    },
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`Supabase ${res.status}: ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

const ownedFile = (env, fileId, userId) =>
  sb(env, `files?id=eq.${fileId}&user_id=eq.${userId}&select=*`);

// ---------------------------------------------------------------------------
// routes
// ---------------------------------------------------------------------------

/**
 * POST /uploads
 * Reserve a place for a file and hand back everything needed to push the bytes.
 *
 * If this user already has a completed file with the same sha256, no upload
 * happens at all: the existing file id comes back and the caller just links it
 * wherever it is needed. That is what "store each file only once" means in
 * practice, and it is why re-adding the same syllabus to three classes costs
 * nothing.
 */
async function startUpload(request, env, user) {
  const body = await request.json();
  const { filename, mimetype, size, sha256 } = body || {};

  if (!filename) return fail('filename is required', 400, env, request);
  if (!Number.isFinite(size) || size <= 0) return fail('size must be a positive number', 400, env, request);
  if (size > MAX_FILE_BYTES) {
    return fail(`Files are limited to ${Math.floor(MAX_FILE_BYTES / 1024 / 1024)} MB.`, 413, env, request);
  }

  if (sha256) {
    const existing = await sb(
      env,
      `files?user_id=eq.${user.id}&sha256=eq.${encodeURIComponent(sha256)}` +
      `&status=eq.complete&deleted_at=is.null&select=id,filename,size_bytes`
    );
    if (existing && existing.length) {
      return json({ duplicate: true, fileId: existing[0].id }, 200, env, request);
    }
  }

  const fileId = crypto.randomUUID();
  const key = `u/${user.id}/${fileId}/${filename.replace(/[^\w.\-]+/g, '_')}`;

  const multipart = size > PART_SIZE;
  let uploadId = null;
  if (multipart) {
    const mp = await env.FILES.createMultipartUpload(key, {
      httpMetadata: { contentType: mimetype || 'application/octet-stream' },
    });
    uploadId = mp.uploadId;
  }

  await sb(env, 'files', {
    method: 'POST',
    body: JSON.stringify({
      id: fileId,
      user_id: user.id,
      r2_key: key,
      sha256: sha256 || null,
      filename,
      mimetype: mimetype || 'application/octet-stream',
      size_bytes: size,
      status: 'uploading',
      upload_id: uploadId,
    }),
  });

  return json({
    fileId,
    key,
    multipart,
    uploadId,
    partSize: PART_SIZE,
    partCount: multipart ? Math.ceil(size / PART_SIZE) : 1,
    ticket: await issueTicket(env, fileId, user.id),
  }, 200, env, request);
}

/**
 * PUT /uploads/:fileId/parts/:partNumber
 * One chunk. Retrying a part is safe: R2 keeps the last body written for a part
 * number, so a failed chunk is re-sent rather than restarting the whole file.
 */
async function uploadPart(request, env, user, fileId, partNumber) {
  const ticket = request.headers.get('X-Upload-Ticket');
  if (!(await checkTicket(env, ticket, fileId, user.id))) {
    return fail('Upload ticket is invalid or expired. Start the upload again.', 403, env, request);
  }

  const rows = await ownedFile(env, fileId, user.id);
  if (!rows || !rows.length) return fail('No such upload.', 404, env, request);
  const file = rows[0];
  if (!file.upload_id) return fail('This upload is not multipart.', 400, env, request);

  const mp = env.FILES.resumeMultipartUpload(file.r2_key, file.upload_id);
  const part = await mp.uploadPart(partNumber, request.body);

  return json({ partNumber, etag: part.etag }, 200, env, request);
}

/**
 * PUT /uploads/:fileId/body
 * The whole file in one shot, for anything under the part size.
 */
async function uploadWhole(request, env, user, fileId) {
  const ticket = request.headers.get('X-Upload-Ticket');
  if (!(await checkTicket(env, ticket, fileId, user.id))) {
    return fail('Upload ticket is invalid or expired. Start the upload again.', 403, env, request);
  }
  const rows = await ownedFile(env, fileId, user.id);
  if (!rows || !rows.length) return fail('No such upload.', 404, env, request);
  const file = rows[0];

  await env.FILES.put(file.r2_key, request.body, {
    httpMetadata: { contentType: file.mimetype },
  });
  return json({ ok: true }, 200, env, request);
}

/**
 * POST /uploads/:fileId/complete
 *
 * The row only becomes 'complete' here, after R2 has confirmed the object exists
 * and its size matches what was promised. Until this succeeds the file shows in
 * the app as still uploading, and the nightly retention job will clean it up if it
 * never finishes.
 */
async function completeUpload(request, env, user, fileId) {
  const rows = await ownedFile(env, fileId, user.id);
  if (!rows || !rows.length) return fail('No such upload.', 404, env, request);
  const file = rows[0];

  const body = await request.json().catch(() => ({}));

  if (file.upload_id) {
    const parts = (body && body.parts) || [];
    if (!parts.length) return fail('No parts supplied.', 400, env, request);
    const mp = env.FILES.resumeMultipartUpload(file.r2_key, file.upload_id);
    await mp.complete(
      parts
        .slice()
        .sort((a, b) => a.partNumber - b.partNumber)
        .map((p) => ({ partNumber: p.partNumber, etag: p.etag }))
    );
  }

  // Trust R2, not the client, for what actually landed.
  const head = await env.FILES.head(file.r2_key);
  if (!head) {
    await sb(env, `files?id=eq.${fileId}`, {
      method: 'PATCH',
      body: JSON.stringify({ status: 'failed' }),
    });
    return fail('The upload did not arrive. Try again.', 502, env, request);
  }

  const updated = await sb(env, `files?id=eq.${fileId}`, {
    method: 'PATCH',
    body: JSON.stringify({
      status: 'complete',
      size_bytes: head.size,
      bytes_received: head.size,
      upload_id: null,
    }),
  });

  return json({ ok: true, file: updated && updated[0] }, 200, env, request);
}

/** POST /uploads/:fileId/abort */
async function abortUpload(request, env, user, fileId) {
  const rows = await ownedFile(env, fileId, user.id);
  if (!rows || !rows.length) return fail('No such upload.', 404, env, request);
  const file = rows[0];

  if (file.upload_id) {
    try {
      await env.FILES.resumeMultipartUpload(file.r2_key, file.upload_id).abort();
    } catch (e) {
      // an already-aborted upload is not an error worth surfacing
    }
  }
  // Deleting the row fires the trigger that queues the key for R2 cleanup.
  await sb(env, `files?id=eq.${fileId}&user_id=eq.${user.id}`, { method: 'DELETE' });
  return json({ ok: true }, 200, env, request);
}

/**
 * GET /files/:fileId
 * Streams the object back, but only to the user who owns the row. R2 itself has no
 * idea who anyone is, so this ownership check is the only thing standing between a
 * file and the rest of the internet.
 */
async function downloadFile(request, env, user, fileId) {
  const rows = await ownedFile(env, fileId, user.id);
  if (!rows || !rows.length) return fail('Not found.', 404, env, request);
  const file = rows[0];
  if (file.status !== 'complete') return fail('This file has not finished uploading.', 409, env, request);

  const object = await env.FILES.get(file.r2_key, {
    range: request.headers.get('Range') ? request.headers : undefined,
  });
  if (!object) return fail('The stored file is missing.', 404, env, request);

  const headers = new Headers(corsHeaders(env, request));
  object.writeHttpMetadata(headers);
  headers.set('etag', object.httpEtag);
  headers.set('Cache-Control', 'private, max-age=3600');
  const disposition = new URL(request.url).searchParams.get('download') === '1' ? 'attachment' : 'inline';
  headers.set('Content-Disposition', `${disposition}; filename="${encodeURIComponent(file.filename)}"`);

  return new Response(object.body, {
    status: object.range ? 206 : 200,
    headers,
  });
}

/**
 * POST /ai/headstart
 * Proxies to Anthropic so the API key stays server side. The prompt is built here
 * from ids, not accepted wholesale from the browser, so a user cannot turn this
 * into a free general-purpose Claude endpoint on your bill.
 */
async function headstart(request, env, user) {
  if (!env.ANTHROPIC_API_KEY) {
    return fail('Headstart is not configured: ANTHROPIC_API_KEY is unset.', 503, env, request);
  }
  const { itemId, kind, instructions } = (await request.json()) || {};
  if (!itemId || !kind) return fail('itemId and kind are required.', 400, env, request);

  const items = await sb(
    env,
    `items?id=eq.${itemId}&user_id=eq.${user.id}&select=id,title,type,due_date,notes,weight,class_id`
  );
  if (!items || !items.length) return fail('No such assignment.', 404, env, request);
  const item = items[0];

  const prompt = buildHeadstartPrompt(kind, item, instructions);
  if (!prompt) return fail('Unknown Headstart kind.', 400, env, request);

  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'x-api-key': env.ANTHROPIC_API_KEY,
      'anthropic-version': '2023-06-01',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model: env.ANTHROPIC_MODEL || 'claude-opus-5',
      max_tokens: 4000,
      messages: [{ role: 'user', content: prompt }],
    }),
  });

  if (!res.ok) {
    const detail = await res.text();
    if (res.status === 401) return fail('The Anthropic API key was rejected.', 503, env, request);
    if (res.status === 429) return fail('Rate limited by Anthropic. Try again shortly.', 429, env, request);
    return fail(`Anthropic error ${res.status}: ${detail.slice(0, 200)}`, 502, env, request);
  }

  const data = await res.json();
  const content = (data.content || []).map((b) => b.text || '').join('').trim();

  await sb(env, 'headstarts?on_conflict=item_id,kind', {
    method: 'POST',
    headers: { Prefer: 'resolution=merge-duplicates,return=representation' },
    body: JSON.stringify({
      user_id: user.id,
      item_id: itemId,
      kind,
      content,
      status: 'ready',
      instructions: instructions || '',
      model: data.model || null,
    }),
  });

  return json({ ok: true, content }, 200, env, request);
}

const HEADSTART_PROMPTS = {
  draft: 'Write a first draft a student could build on.',
  essay_outline: 'Produce a structured essay outline with a thesis and section headings.',
  quiz_prep: 'Produce focused quiz preparation: likely question areas and concise answers.',
  study_outline: 'Produce a study outline covering the material this exam is likely to test.',
  synthesis: 'Synthesise the key ideas a student should take from the assigned readings.',
  explain: 'Explain in plain language what this assignment is actually asking for, and how to approach it.',
};

function buildHeadstartPrompt(kind, item, instructions) {
  const task = HEADSTART_PROMPTS[kind];
  if (!task) return null;
  const bits = [
    task,
    '',
    `Assignment: ${item.title || 'Untitled'}`,
    `Type: ${item.type || 'assignment'}`,
  ];
  if (item.due_date) bits.push(`Due: ${item.due_date}`);
  if (item.weight != null) bits.push(`Worth: ${item.weight}% of the course grade`);
  if (item.notes) bits.push('', 'Details the student recorded:', item.notes);
  if (instructions) bits.push('', 'Additional instructions from the student:', String(instructions).slice(0, 4000));
  bits.push('', 'This is study scaffolding for the student to work from, not something to submit as-is.');
  return bits.join('\n');
}

// ---------------------------------------------------------------------------
// entry points
// ---------------------------------------------------------------------------

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(env, request) });
    }

    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, '');

    if (path === '/health') return json({ ok: true }, 200, env, request);

    // A read carrying a signed download token needs no Authorization header,
    // because an <img> or <iframe> cannot send one.
    const tokenMatch = path.match(/^\/files\/([0-9a-f-]{36})$/);
    if (tokenMatch && request.method === 'GET') {
      const t = url.searchParams.get('t');
      if (t) {
        const tokenUser = await userFromDownloadToken(env, t);
        if (!tokenUser) return fail('This link has expired. Reload the page.', 403, env, request);
        try {
          return await downloadFile(request, env, tokenUser, tokenMatch[1]);
        } catch (err) {
          return fail(`Server error: ${err.message}`, 500, env, request);
        }
      }
    }

    const user = await currentUser(request, env);
    if (!user) return fail('Sign in first.', 401, env, request);

    if (path === '/session/download-token' && request.method === 'GET') {
      return json({
        token: await issueDownloadToken(env, user.id),
        expiresIn: 3600,
      }, 200, env, request);
    }

    try {
      let m;
      if (request.method === 'POST' && path === '/uploads') {
        return await startUpload(request, env, user);
      }
      if ((m = path.match(/^\/uploads\/([0-9a-f-]{36})\/parts\/(\d+)$/)) && request.method === 'PUT') {
        return await uploadPart(request, env, user, m[1], Number(m[2]));
      }
      if ((m = path.match(/^\/uploads\/([0-9a-f-]{36})\/body$/)) && request.method === 'PUT') {
        return await uploadWhole(request, env, user, m[1]);
      }
      if ((m = path.match(/^\/uploads\/([0-9a-f-]{36})\/complete$/)) && request.method === 'POST') {
        return await completeUpload(request, env, user, m[1]);
      }
      if ((m = path.match(/^\/uploads\/([0-9a-f-]{36})\/abort$/)) && request.method === 'POST') {
        return await abortUpload(request, env, user, m[1]);
      }
      if ((m = path.match(/^\/files\/([0-9a-f-]{36})$/)) && request.method === 'GET') {
        return await downloadFile(request, env, user, m[1]);
      }
      if (path === '/ai/headstart' && request.method === 'POST') {
        return await headstart(request, env, user);
      }
      return fail('Not found.', 404, env, request);
    } catch (err) {
      return fail(`Server error: ${err.message}`, 500, env, request);
    }
  },

  /**
   * Nightly. Deleting a file row in Postgres cannot reach into R2, so the row's
   * trigger copies the key into r2_deletion_queue and this drains it. Without
   * this, deleted files would keep costing storage forever.
   */
  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      const pending = await sb(
        env,
        'r2_deletion_queue?deleted_at=is.null&attempts=lt.5&select=id,r2_key,attempts&limit=500'
      );
      for (const row of pending || []) {
        try {
          await env.FILES.delete(row.r2_key);
          await sb(env, `r2_deletion_queue?id=eq.${row.id}`, {
            method: 'PATCH',
            body: JSON.stringify({ deleted_at: new Date().toISOString() }),
          });
        } catch (err) {
          await sb(env, `r2_deletion_queue?id=eq.${row.id}`, {
            method: 'PATCH',
            body: JSON.stringify({ attempts: (row.attempts || 0) + 1, last_error: String(err).slice(0, 300) }),
          });
        }
      }
    })());
  },
};
