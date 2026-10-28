/* BThinkX website chat assistant ("Bee").
 * Builds its own DOM, talks to /chat/api/*, keeps the conversation in localStorage so it survives
 * page changes, and never throws into the host page. */
(function () {
  'use strict';
  if (window.__btxChat) return;
  window.__btxChat = true;

  var API = '/chat/api/';
  var KEY = 'btx_chat_token';
  var HELLO_KEY = 'btx_chat_hello_seen';
  var WA_DEFAULT = 'https://wa.me/917736094292';
  var state = { token: null, open: false, busy: false, started: false, leadCaptured: false, userMsgs: 0, whatsapp: WA_DEFAULT, lastPdf: '' };
  var els = {};

  function store(get, k, v) {
    try { if (get) return window.localStorage.getItem(k); window.localStorage.setItem(k, v); } catch (e) { /* private mode */ }
    return null;
  }
  function seen(set) {
    try { if (set) window.sessionStorage.setItem(HELLO_KEY, '1'); return window.sessionStorage.getItem(HELLO_KEY); } catch (e) { return null; }
  }
  function track(name) { try { if (window.gtag) window.gtag('event', name); } catch (e) { /* analytics is optional */ } }

  function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

  /* Safe mini-markdown: escape first, then **bold**, "- " bullets, https links, newlines. */
  function fmt(text) {
    var lines = esc(text).split(/\n/), out = [], inList = false;
    lines.forEach(function (ln) {
      var m = ln.match(/^\s*[-•*]\s+(.*)$/);
      if (m) { if (!inList) { out.push('<ul>'); inList = true; } out.push('<li>' + inline(m[1]) + '</li>'); return; }
      if (inList) { out.push('</ul>'); inList = false; }
      out.push(ln.trim() === '' ? '<br>' : inline(ln) + '<br>');
    });
    if (inList) out.push('</ul>');
    return out.join('').replace(/(<br>)+$/, '');
  }
  function inline(s) {
    return s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/(https:\/\/[^\s<)]+)/g, function (u) { return '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + u.replace(/^https:\/\//, '') + '</a>'; });
  }

  var ROBOT = '<svg viewBox="0 0 48 48" aria-hidden="true" focusable="false">' +
    '<g class="bee-antenna"><line x1="24" y1="9" x2="24" y2="4.5" stroke="#fff" stroke-width="2" stroke-linecap="round"/><circle cx="24" cy="3.6" r="2.4" fill="#5eead4"/></g>' +
    '<rect x="5" y="22" width="4" height="9" rx="2" fill="#fff" opacity=".85"/><rect x="39" y="22" width="4" height="9" rx="2" fill="#fff" opacity=".85"/>' +
    '<rect x="8" y="9" width="32" height="30" rx="11" fill="#fff"/>' +
    '<rect x="12" y="15" width="24" height="17" rx="7" fill="#1d1d33"/>' +
    '<g class="bee-look"><ellipse class="bee-eye" cx="19" cy="23" rx="3.2" ry="3.8" fill="#5eead4"/><ellipse class="bee-eye" cx="29" cy="23" rx="3.2" ry="3.8" fill="#5eead4"/></g>' +
    '<path d="M20.5 29.2c1.9 1.5 5.1 1.5 7 0" stroke="#5eead4" stroke-width="1.6" stroke-linecap="round" fill="none"/>' +
    '</svg>';
  var ICON_SEND = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M3.4 20.4 21 12 3.4 3.6l-.1 6.5L15 12 3.3 13.9z"/></svg>';
  var ICON_WA = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M17.47 14.38c-.3-.15-1.76-.87-2.03-.97-.27-.1-.47-.15-.67.15-.2.3-.77.97-.94 1.16-.17.2-.35.22-.65.07-.3-.15-1.26-.46-2.39-1.48-.88-.79-1.48-1.76-1.65-2.06-.17-.3-.02-.46.13-.6.13-.14.3-.35.45-.52.15-.17.2-.3.3-.5.1-.2.05-.37-.02-.52-.08-.15-.67-1.61-.92-2.2-.24-.58-.49-.5-.67-.51h-.57c-.2 0-.52.07-.8.37-.27.3-1.04 1.02-1.04 2.48 0 1.46 1.07 2.88 1.21 3.07.15.2 2.1 3.2 5.08 4.49.71.3 1.26.49 1.69.63.71.23 1.36.2 1.87.12.57-.08 1.76-.72 2-1.41.25-.7.25-1.29.17-1.41-.07-.12-.27-.2-.57-.35M12.05 21.8a9.9 9.9 0 0 1-5.03-1.38l-.36-.21-3.74.98 1-3.65-.24-.37a9.86 9.86 0 0 1-1.51-5.26c0-5.45 4.44-9.88 9.89-9.88 2.64 0 5.12 1.03 6.99 2.9a9.83 9.83 0 0 1 2.89 6.99c0 5.45-4.44 9.88-9.89 9.88M20.46 3.49A11.8 11.8 0 0 0 12.05 0C5.5 0 .16 5.34.16 11.89c0 2.1.55 4.14 1.59 5.95L.06 24l6.3-1.65a11.9 11.9 0 0 0 5.69 1.45h.01c6.55 0 11.89-5.34 11.89-11.89 0-3.18-1.24-6.17-3.49-8.42"/></svg>';
  var ICON_X = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>';

  function build() {
    var root = document.createElement('div');
    root.className = 'btx-chat';
    root.innerHTML =
      '<button type="button" class="btx-chat-launcher" aria-label="Chat with BThinkX assistant" aria-haspopup="dialog" aria-expanded="false">' + ROBOT + '<span class="btx-chat-dot" aria-hidden="true"></span></button>' +
      '<div class="btx-chat-hello" role="status"><button type="button" class="btx-chat-hello-x" aria-label="Dismiss">&times;</button>Hi <span aria-hidden="true">&#128075;</span> I\'m <b>Bee</b>. Need help choosing a package? <b>Ask me!</b></div>' +
      '<div class="btx-chat-panel" role="dialog" aria-label="Chat with BThinkX assistant" aria-modal="false">' +
        '<div class="btx-chat-head"><div class="btx-chat-avatar">' + ROBOT + '</div>' +
          '<div><div class="btx-chat-title">BThinkX Assistant</div><div class="btx-chat-sub">Online &middot; replies instantly</div></div><span class="btx-chat-head-spacer"></span>' +
          '<a class="btx-chat-iconbtn btx-wa" href="' + WA_DEFAULT + '" target="_blank" rel="noopener noreferrer" aria-label="Continue on WhatsApp" title="Continue on WhatsApp">' + ICON_WA + '</a>' +
          '<button type="button" class="btx-chat-iconbtn btx-close" aria-label="Close chat">' + ICON_X + '</button></div>' +
        '<div class="btx-chat-body" role="log" aria-live="polite" aria-relevant="additions"></div>' +
        '<div class="btx-chips" aria-label="Suggested questions"></div>' +
        '<form class="btx-leadform" novalidate autocomplete="on"><p>Share your details and our team will send a quotation and call you.</p>' +
          '<div class="btx-form-error" role="alert"></div>' +
          '<input type="text" name="name" placeholder="Your name" autocomplete="name" aria-label="Your name" maxlength="100">' +
          '<input type="tel" name="phone" placeholder="WhatsApp / phone number" autocomplete="tel" inputmode="tel" aria-label="Phone number" maxlength="20">' +
          '<input type="text" name="business" placeholder="Business name (optional)" autocomplete="organization" aria-label="Business name" maxlength="150">' +
          '<input class="btx-hp" type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">' +
          '<div class="btx-leadform-row"><button type="submit" class="btx-btn">Request a call back</button><button type="button" class="btx-btn btx-btn-ghost btx-lead-cancel">Not now</button></div></form>' +
        '<div class="btx-chat-foot"><form class="btx-chat-form" autocomplete="off">' +
          '<input class="btx-chat-input" type="text" name="text" placeholder="Type your message…" aria-label="Your message" maxlength="600" enterkeyhint="send">' +
          '<button type="submit" class="btx-chat-send" aria-label="Send message">' + ICON_SEND + '</button></form>' +
          '<p class="btx-chat-note">AI assistant &middot; may make mistakes &middot; <a href="/privacy-policy/">Privacy</a></p></div>' +
      '</div>';
    document.body.appendChild(root);
    els.root = root;
    els.launcher = root.querySelector('.btx-chat-launcher');
    els.hello = root.querySelector('.btx-chat-hello');
    els.helloX = root.querySelector('.btx-chat-hello-x');
    els.panel = root.querySelector('.btx-chat-panel');
    els.body = root.querySelector('.btx-chat-body');
    els.chips = root.querySelector('.btx-chips');
    els.lead = root.querySelector('.btx-leadform');
    els.leadErr = root.querySelector('.btx-form-error');
    els.form = root.querySelector('.btx-chat-form');
    els.input = root.querySelector('.btx-chat-input');
    els.send = root.querySelector('.btx-chat-send');
    els.wa = root.querySelector('.btx-wa');
  }

  function scrollDown() { els.body.scrollTop = els.body.scrollHeight; }
  function addMsg(role, text) {
    var d = document.createElement('div');
    d.className = 'btx-msg ' + (role === 'visitor' || role === 'me' ? 'btx-msg-me' : 'btx-msg-bot');
    if (role === 'visitor' || role === 'me') d.textContent = text; else d.innerHTML = fmt(text);
    els.body.appendChild(d);
    scrollDown();
    return d;
  }
  function typing(on) {
    var t = els.body.querySelector('.btx-typing-wrap');
    if (on && !t) {
      t = document.createElement('div');
      t.className = 'btx-msg btx-msg-bot btx-typing-wrap';
      t.setAttribute('aria-label', 'Assistant is typing');
      t.innerHTML = '<span class="btx-typing"><i></i><i></i><i></i></span>';
      els.body.appendChild(t); scrollDown();
    } else if (!on && t) t.remove();
  }
  function setChips(list) {
    els.chips.innerHTML = '';
    (list || []).forEach(function (label) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'btx-chip'; b.textContent = label;
      b.addEventListener('click', function () { send(label); });
      els.chips.appendChild(b);
    });
    if (!state.leadCaptured) {
      var c = document.createElement('button');
      c.type = 'button'; c.className = 'btx-chip'; c.textContent = 'Request a call back';
      c.addEventListener('click', function () { showLead(true); });
      els.chips.appendChild(c);
    }
  }
  function addPdfChip(url) {
    if (!url || url === state.lastPdf) return;
    state.lastPdf = url;
    var a = document.createElement('a');
    a.className = 'btx-chip'; a.href = url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = 'Download quotation PDF';
    els.chips.appendChild(a);
  }
  function showLead(on) {
    if (state.leadCaptured && on) return;
    els.lead.classList.toggle('is-on', !!on);
    if (on) { els.leadErr.textContent = ''; scrollDown(); }
  }
  function setWhatsApp(url) {
    if (url && /^https:\/\/wa\.me\//.test(url)) { state.whatsapp = url; els.wa.href = url; }
  }

  function post(path, body) {
    return fetch(API + path, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: 'Please try again.' }; }).then(function (d) { d.status = r.status; return d; });
    });
  }

  function offlineReply() {
    return 'Sorry, I could not connect just now. You can reach our team directly on WhatsApp: ' + state.whatsapp;
  }

  function start() {
    if (state.started) return Promise.resolve();
    state.started = true;
    state.token = store(true, KEY) || null;
    return post('start/', { token: state.token, page: location.pathname }).then(function (d) {
      if (!d.ok) throw new Error('start');
      state.token = d.token; store(false, KEY, d.token);
      state.leadCaptured = !!d.lead_captured;
      setWhatsApp(d.whatsapp);
      els.body.innerHTML = '';
      (d.messages || []).forEach(function (m) { addMsg(m.role, m.text); if (m.role === 'visitor') state.userMsgs++; });
      setChips(d.quick_replies);
    }).catch(function () {
      state.started = false;
      els.body.innerHTML = '';
      addMsg('bot', 'Hi! I\'m Bee, BThinkX\'s assistant. I could not load the chat just now, but you can reach us on WhatsApp: ' + state.whatsapp);
    });
  }

  function send(text) {
    text = (text || '').trim();
    if (!text || state.busy) return;
    state.busy = true; els.send.disabled = true;
    els.chips.innerHTML = '';
    addMsg('me', text); els.input.value = '';
    state.userMsgs++;
    typing(true);
    var ensure = state.token ? Promise.resolve() : (state.started = false, start());
    ensure.then(function () {
      if (!state.token) throw new Error('no session');
      return post('message/', { token: state.token, text: text, page: location.pathname });
    }).then(function (d) {
      typing(false);
      if (d.status === 404) { store(false, KEY, ''); state.token = null; state.started = false; addMsg('bot', 'This chat had expired, please send your message again.'); return; }
      if (!d.ok) { addMsg('bot', d.error || offlineReply()); return; }
      addMsg('bot', d.reply);
      setWhatsApp(d.whatsapp);
      if (d.lead_captured) { state.leadCaptured = true; showLead(false); track('chatbot_lead'); }
      if (d.pdf_url) addPdfChip(d.pdf_url);
      if (!state.leadCaptured && state.userMsgs >= 2 && !els.lead.dataset.shown) { els.lead.dataset.shown = '1'; showLead(true); }
    }).catch(function () {
      typing(false); addMsg('bot', offlineReply());
    }).then(function () {
      state.busy = false; els.send.disabled = false;
      if (state.open && window.matchMedia('(min-width: 521px)').matches) els.input.focus();
    });
  }

  function submitLead(e) {
    e.preventDefault();
    var f = els.lead, btn = f.querySelector('.btx-btn[type=submit]');
    var name = f.name.value.trim(), phone = f.phone.value.trim();
    els.leadErr.textContent = '';
    if (name.length < 2) { els.leadErr.textContent = 'Please enter your name.'; f.name.focus(); return; }
    if (phone.replace(/\D/g, '').length < 10) { els.leadErr.textContent = 'Please enter a valid 10-digit mobile number.'; f.phone.focus(); return; }
    btn.disabled = true; btn.textContent = 'Sending…';
    var ensure = state.token ? Promise.resolve() : (state.started = false, start());
    ensure.then(function () {
      return post('lead/', { token: state.token, name: name, phone: phone, business: f.business.value.trim(), website: f.website.value });
    }).then(function (d) {
      if (!d.ok) { els.leadErr.textContent = d.error || 'Something went wrong. Please try WhatsApp.'; return; }
      state.leadCaptured = true; showLead(false);
      if (d.reply) addMsg('bot', d.reply);
      if (d.pdf_url) addPdfChip(d.pdf_url);
      track('chatbot_lead');
    }).catch(function () { els.leadErr.textContent = 'Could not send. Please try WhatsApp.'; })
      .then(function () { btn.disabled = false; btn.textContent = 'Request a call back'; });
  }

  function openChat() {
    if (state.open) return;
    state.open = true;
    els.panel.classList.add('is-open'); els.launcher.setAttribute('aria-expanded', 'true');
    els.hello.classList.remove('is-on'); seen(true);
    track('chatbot_open');
    start().then(function () {
      scrollDown();
      if (window.matchMedia('(min-width: 521px)').matches) els.input.focus();
    });
  }
  function closeChat() {
    if (!state.open) return;
    state.open = false;
    els.panel.classList.remove('is-open'); els.launcher.setAttribute('aria-expanded', 'false');
    els.launcher.focus({ preventScroll: true });
  }

  function init() {
    build();
    els.launcher.addEventListener('click', openChat);
    els.hello.addEventListener('click', function (e) { if (e.target !== els.helloX) openChat(); });
    els.helloX.addEventListener('click', function () { els.hello.classList.remove('is-on'); seen(true); });
    els.root.querySelector('.btx-close').addEventListener('click', closeChat);
    els.root.querySelector('.btx-lead-cancel').addEventListener('click', function () { showLead(false); });
    els.form.addEventListener('submit', function (e) { e.preventDefault(); send(els.input.value); });
    els.lead.addEventListener('submit', submitLead);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && state.open) closeChat(); });
    window.btxOpenChat = openChat;   // any "Chat with us" link on the site can call this

    if (location.hash === '#chat') openChat();   // e.g. a "Chat with us" link to /#chat
    if (!seen(false)) {
      setTimeout(function () { if (!state.open) els.hello.classList.add('is-on'); }, 3500);
      setTimeout(function () { els.hello.classList.remove('is-on'); seen(true); }, 18000);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
