/**
 * Vesta sign-in gate.
 *
 * Without a session, every query is refused by Postgres, so the app must not boot
 * at all until someone is signed in. This puts a panel over the page until that
 * happens and then gets out of the way.
 *
 * It deliberately reuses the app's own classes (card, btn, field) rather than
 * inventing a second visual language for the login screen.
 *
 * Load after vesta-cloud.js and before the app's own script.
 */
(function (global) {
  'use strict';

  var BE = global.VESTA_BACKEND;
  if (!BE) return;

  var STYLE = [
    '.vs-gate{position:fixed;inset:0;z-index:400;display:flex;align-items:center;justify-content:center;',
    'padding:24px;background:var(--bg,#F4F5F7);}',
    '.vs-card{width:100%;max-width:380px;background:var(--surface,#fff);border:1px solid var(--border,#EBEEF4);',
    'border-radius:var(--r-card,18px);box-shadow:0 12px 40px rgba(16,24,40,.10);padding:28px;}',
    '.vs-brand{display:flex;align-items:center;gap:9px;font-size:19px;font-weight:800;margin-bottom:6px;}',
    '.vs-sub{font-size:13px;color:var(--text-muted,#6B7280);margin-bottom:20px;line-height:1.5;}',
    '.vs-field{display:flex;flex-direction:column;gap:5px;font-size:12px;font-weight:600;',
    'color:var(--text-muted,#6B7280);margin-bottom:12px;}',
    '.vs-field input{background:var(--surface,#fff);border:1px solid var(--border,#EBEEF4);border-radius:10px;',
    'padding:9px 11px;font-size:13px;font-family:inherit;color:var(--text,#131722);}',
    '.vs-field input:focus{outline:none;border-color:var(--accent,#005FFB);}',
    '.vs-btn{width:100%;border-radius:10px;padding:10px 14px;font-size:13.5px;font-weight:700;',
    'font-family:inherit;cursor:pointer;border:1px solid transparent;}',
    '.vs-google{background:var(--surface,#fff);border-color:var(--border,#EBEEF4);color:var(--text,#131722);',
    'display:flex;align-items:center;justify-content:center;gap:9px;margin-bottom:14px;}',
    '.vs-google:hover{background:var(--surface-2,#EDEFF3);}',
    '.vs-primary{background:var(--accent-solid,#243145);color:#fff;}',
    '.vs-primary:disabled{opacity:.55;cursor:default;}',
    '.vs-or{display:flex;align-items:center;gap:10px;margin:16px 0;font-size:11px;',
    'color:var(--text-faint,#9CA3AF);text-transform:uppercase;letter-spacing:.06em;}',
    '.vs-or::before,.vs-or::after{content:"";flex:1;height:1px;background:var(--border,#EBEEF4);}',
    '.vs-msg{font-size:12.5px;border-radius:10px;padding:9px 11px;margin-bottom:12px;line-height:1.45;}',
    '.vs-err{background:#FDE8E8;color:#C0342E;}',
    '.vs-ok{background:#E7F7EE;color:#18794E;}',
    '.vs-alt{margin-top:14px;font-size:12.5px;color:var(--text-muted,#6B7280);text-align:center;}',
    '.vs-alt a{color:var(--accent,#005FFB);cursor:pointer;text-decoration:none;font-weight:600;}',
  ].join('');

  var GOOGLE_MARK =
    '<svg width="16" height="16" viewBox="0 0 48 48" aria-hidden="true">' +
    '<path fill="#4285F4" d="M45 24c0-1.6-.1-2.7-.4-4H24v7.5h12c-.2 2-1.6 5-4.5 7l6.9 5.3C42.5 36.2 45 30.6 45 24z"/>' +
    '<path fill="#34A853" d="M24 46c6 0 11-2 14.4-5.4l-6.9-5.3c-1.9 1.3-4.4 2.2-7.5 2.2-5.8 0-10.7-3.9-12.4-9.1l-7.1 5.5C8 41.1 15.4 46 24 46z"/>' +
    '<path fill="#FBBC05" d="M11.6 28.4A13.4 13.4 0 0 1 10.9 24c0-1.5.3-3 .7-4.4l-7.1-5.5A22 22 0 0 0 2 24c0 3.5.8 6.9 2.5 9.9z"/>' +
    '<path fill="#EA4335" d="M24 10.5c3.3 0 6.2 1.1 8.5 3.3l6.1-6.1C34.9 4.2 30 2 24 2 15.4 2 8 6.9 4.5 14.1l7.1 5.5C13.3 14.4 18.2 10.5 24 10.5z"/>' +
    '</svg>';

  var mode = 'signin';   // signin | signup | forgot | recover
  var busy = false;
  var message = null;    // { kind:'err'|'ok', text }

  function el(id) { return document.getElementById(id); }

  function render() {
    var host = el('vesta-gate');
    if (!host) return;
    host.innerHTML =
      '<div class="vs-gate"><div class="vs-card">' +
      '<div class="vs-brand"><img src="/vesta-mark.png" alt="" style="width:26px;height:26px;object-fit:contain;"> Vesta</div>' +
      '<div class="vs-sub">' + SUBTITLE[mode] + '</div>' +
      (message ? '<div class="vs-msg ' + (message.kind === 'err' ? 'vs-err' : 'vs-ok') + '">' +
        escape_(message.text) + '</div>' : '') +
      (mode === 'signin' || mode === 'signup'
        ? '<button type="button" class="vs-btn vs-google" id="vs-google">' + GOOGLE_MARK + ' Continue with Google</button>' +
          '<div class="vs-or">or</div>'
        : '') +
      (mode === 'recover'
        ? ''
        : '<label class="vs-field">Email<input type="email" id="vs-email" autocomplete="email" placeholder="you@example.com"></label>') +
      (mode === 'forgot'
        ? ''
        : '<label class="vs-field">' + (mode === 'recover' ? 'New password' : 'Password') +
          '<input type="password" id="vs-password" autocomplete="' +
          (mode === 'signup' || mode === 'recover' ? 'new-password' : 'current-password') +
          '" placeholder="••••••••"></label>') +
      '<button type="button" class="vs-btn vs-primary" id="vs-submit"' + (busy ? ' disabled' : '') + '>' +
        (busy ? 'Working…' : SUBMIT[mode]) + '</button>' +
      '<div class="vs-alt">' + ALT[mode] + '</div>' +
      '</div></div>';

    var g = el('vs-google');
    if (g) {
      g.onclick = function () {
        busy = true; message = null; render();
        BE.auth.signInWithGoogle().catch(function (err) {
          busy = false; message = { kind: 'err', text: err.message }; render();
        });
      };
    }
    Array.prototype.forEach.call(host.querySelectorAll('[data-go]'), function (link) {
      link.onclick = function () {
        mode = link.getAttribute('data-go');
        message = null;
        render();
      };
    });
    el('vs-submit').onclick = submit;
    [el('vs-email'), el('vs-password')].forEach(function (input) {
      if (input) input.onkeydown = function (e) { if (e.key === 'Enter') submit(); };
    });
    var first = el('vs-email') || el('vs-password');
    if (first) first.focus();
  }

  var SUBTITLE = {
    signin: 'Sign in to reach your classes, notes and files on any device.',
    signup: 'Create your account. Your email has to be on the invite list first.',
    forgot: 'Enter your email and we will send you a link to set a new password.',
    recover: 'Choose a new password for your account.',
  };
  var SUBMIT = {
    signin: 'Sign in', signup: 'Create account',
    forgot: 'Send reset link', recover: 'Save new password',
  };
  var ALT = {
    signin: 'Been invited but have no account yet? <a data-go="signup">Create one</a>' +
            ' · <a data-go="forgot">Forgot password</a>',
    signup: 'Already have an account? <a data-go="signin">Sign in</a>',
    forgot: '<a data-go="signin">Back to sign in</a>',
    recover: '',
  };

  function submit() {
    if (busy) return;
    var emailEl = el('vs-email');
    var passEl = el('vs-password');
    var email = emailEl ? (emailEl.value || '').trim() : '';
    var password = passEl ? passEl.value || '' : '';

    if (mode === 'forgot') {
      if (!email) { message = { kind: 'err', text: 'Enter your email.' }; render(); return; }
      busy = true; message = null; render();
      BE.auth.resetPassword(email).then(function () {
        busy = false;
        mode = 'signin';
        // Deliberately the same answer whether or not the address has an account.
        message = { kind: 'ok', text: 'If that email has an account, a reset link is on its way.' };
        render();
      }).catch(function (err) {
        busy = false; message = { kind: 'err', text: err.message }; render();
      });
      return;
    }

    if (mode === 'recover') {
      if (password.length < 6) {
        message = { kind: 'err', text: 'Use at least six characters.' }; render(); return;
      }
      busy = true; message = null; render();
      BE.auth.updatePassword(password).then(function () {
        busy = false;
        message = { kind: 'ok', text: 'Password changed. You are signed in.' };
        render();
        setTimeout(function () { global.location.reload(); }, 1200);
      }).catch(function (err) {
        busy = false; message = { kind: 'err', text: err.message }; render();
      });
      return;
    }

    if (!email || !password) {
      message = { kind: 'err', text: 'Enter your email and password.' };
      render();
      return;
    }
    busy = true; message = null; render();

    var op = mode === 'signup'
      ? BE.auth.signUp(email, password)
      : BE.auth.signInWithPassword(email, password);

    op.then(function (res) {
      busy = false;
      // A signup that needs email confirmation returns a user but no session.
      if (mode === 'signup' && res && res.user && !res.session) {
        message = { kind: 'ok', text: 'Check your email to confirm the address, then sign in.' };
        mode = 'signin';
        render();
      }
      // Otherwise onAuthStateChange takes it from here and the gate closes.
    }).catch(function (err) {
      busy = false;
      message = { kind: 'err', text: err.message };
      render();
    });
  }

  function escape_(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function show() {
    if (!el('vesta-gate')) {
      var style = document.createElement('style');
      style.textContent = STYLE;
      document.head.appendChild(style);
      var host = document.createElement('div');
      host.id = 'vesta-gate';
      document.body.appendChild(host);
    }
    render();
  }

  function hide() {
    var host = el('vesta-gate');
    if (host) host.innerHTML = '';
  }

  /**
   * Resolves once there is a session. The app's own init() waits on this, so no
   * query is ever attempted signed out.
   */
  BE.requireAuth = function () {
    // Arriving from a reset link must show the new-password form, not the app,
    // even though the link does technically sign the user in.
    BE.auth.onPasswordRecovery(function () {
      mode = 'recover';
      message = null;
      show();
    });
    return new Promise(function (resolve) {
      var settle = function (session) {
        if (mode === 'recover') { show(); return; }   // finish the reset first
        if (session) { hide(); resolve(session); } else { show(); }
      };
      BE.auth.init(settle).then(settle);
    });
  };

  BE.signOutAndLock = function () {
    return BE.auth.signOut().then(function () {
      show();
      global.location.reload();
    });
  };
})(typeof window !== 'undefined' ? window : this);
