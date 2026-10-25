/*
 * BThinkX Business OS — shared UI behaviour (Part 2 of the specification).
 * One library so every page behaves the same. No build step, no dependencies
 * (Alpine.js handles small reactive bits; this file handles the rules).
 */
(function () {
  'use strict';

  var csrf = function () {
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return (m && decodeURIComponent(m[1])) || (document.body && document.body.dataset.csrf) || '';
  };

  /* ------------------------------------------------------------ toasts (2.4) */
  function wireToast(el) {
    var sticky = el.dataset.sticky === '1';
    var remaining = 5000, started = Date.now(), timer = null;
    function dismiss() { el.style.transition = 'opacity .2s'; el.style.opacity = '0'; setTimeout(function () { el.remove(); }, 200); }
    function start() { if (!sticky) { started = Date.now(); timer = setTimeout(dismiss, remaining); } }
    function pause() { if (timer) { clearTimeout(timer); timer = null; remaining -= Date.now() - started; } }
    el.addEventListener('mouseenter', pause);
    el.addEventListener('mouseleave', start);
    el.addEventListener('focusin', pause);
    var close = el.querySelector('.crm-toast-close');
    if (close) close.addEventListener('click', dismiss);
    start();
  }

  window.crmToast = function (message, level) {
    level = level || 'success';
    var region = document.getElementById('toast-region');
    if (!region) { return; }
    var cls = { success: 'alert-success', error: 'alert-error', warning: 'alert-warning', info: 'alert-info' }[level] || 'alert-info';
    var el = document.createElement('div');
    el.className = 'crm-toast pointer-events-auto alert shadow-theme-lg bg-white dark:bg-gray-900 ' + cls;
    el.setAttribute('role', level === 'error' ? 'alert' : 'status');
    el.dataset.sticky = level === 'error' ? '1' : '0';
    var p = document.createElement('p');
    p.className = 'flex-1 text-sm text-gray-700 dark:text-gray-300';
    p.textContent = message;
    var b = document.createElement('button');
    b.type = 'button'; b.className = 'crm-toast-close text-gray-400 hover:text-gray-600'; b.setAttribute('aria-label', 'Dismiss'); b.textContent = '×';
    el.appendChild(p); el.appendChild(b);
    region.appendChild(el);
    wireToast(el);
  };

  /* ------------------------------------------------------------ confirm dialog (2.4, 2.2 rule 6) */
  var confirmState = null;
  window.crmConfirm = function (opts) {
    return new Promise(function (resolve) {
      var box = document.getElementById('crm-confirm');
      if (!box) { resolve(window.confirm(opts.title || 'Are you sure?') ? { reason: '' } : null); return; }
      box.querySelector('#crm-confirm-title').textContent = opts.title || 'Are you sure?';
      box.querySelector('#crm-confirm-body').textContent = opts.body || '';
      var ok = box.querySelector('#crm-confirm-ok');
      var cancel = box.querySelector('#crm-confirm-cancel');
      ok.textContent = opts.ok || 'Confirm';
      cancel.textContent = opts.cancel || 'Keep editing';
      ok.className = 'btn ' + (opts.danger ? 'btn-danger' : 'btn-primary');
      var wrap = box.querySelector('#crm-confirm-reason-wrap');
      var reason = box.querySelector('#crm-confirm-reason');
      var err = box.querySelector('#crm-confirm-reason-error');
      var minLen = opts.reasonMin || 10;
      reason.value = '';
      err.classList.add('hidden');
      wrap.classList.toggle('hidden', !opts.reason);
      box.classList.remove('hidden'); box.classList.add('flex');
      var prevFocus = document.activeElement;
      (opts.reason ? reason : ok).focus();
      confirmState = {
        done: function (value) {
          box.classList.add('hidden'); box.classList.remove('flex');
          confirmState = null;
          if (prevFocus && prevFocus.focus) prevFocus.focus();
          resolve(value);
        },
        ok: function () {
          if (opts.reason && reason.value.trim().length < minLen) {
            err.textContent = 'Enter at least ' + minLen + ' characters.';
            err.classList.remove('hidden'); reason.focus(); return;
          }
          confirmState.done({ reason: reason.value.trim() });
        }
      };
    });
  };

  document.addEventListener('click', function (e) {
    if (!confirmState) return;
    if (e.target.closest('#crm-confirm-ok')) { confirmState.ok(); }
    else if (e.target.closest('[data-confirm-cancel]')) { confirmState.done(null); }
  });
  document.addEventListener('keydown', function (e) {
    if (confirmState && e.key === 'Escape') { confirmState.done(null); e.stopPropagation(); }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      var s = document.getElementById('global-search');
      if (s) { e.preventDefault(); s.focus(); }
    }
  }, true);

  /* ------------------------------------------------------------ dirty forms (2.5) */
  function isDirty(form) { return form && form.dataset.dirty === '1'; }
  document.addEventListener('input', function (e) {
    var form = e.target.form || e.target.closest('form');
    if (form && form.hasAttribute('data-dirty-check')) form.dataset.dirty = '1';
  });
  document.addEventListener('change', function (e) {
    var form = e.target.form || e.target.closest('form');
    if (form && form.hasAttribute('data-dirty-check')) form.dataset.dirty = '1';
  });
  window.addEventListener('beforeunload', function (e) {
    var dirty = Array.prototype.some.call(document.querySelectorAll('form[data-dirty-check]'), function (f) {
      return isDirty(f) && f.dataset.submitting !== '1' && f.offsetParent !== null;
    });
    if (dirty) { e.preventDefault(); e.returnValue = ''; }
  });

  /* ------------------------------------------------------------ modal store (2.6) */
  document.addEventListener('alpine:init', function () {
    window.Alpine.store('modal', {
      open: '',
      show: function (id) {
        this.open = id;
        document.documentElement.style.overflow = 'hidden';
        setTimeout(function () {
          var m = document.querySelector('[data-modal="' + id + '"]');
          if (!m) return;
          var first = m.querySelector('[aria-invalid="true"], input:not([type=hidden]):not([disabled]), select:not([disabled]), textarea:not([disabled]), button.btn');
          if (first) first.focus();
        }, 60);
      },
      close: function (force) {
        var self = this;
        var m = document.querySelector('[data-modal="' + this.open + '"]');
        var forms = m ? m.querySelectorAll('form[data-dirty-check]') : [];
        var dirty = Array.prototype.some.call(forms, isDirty);
        function doClose() {
          Array.prototype.forEach.call(forms, function (f) { f.dataset.dirty = '0'; });
          self.open = '';
          document.documentElement.style.overflow = '';
          var url = new URL(window.location.href);
          if (url.searchParams.has('modal')) { url.searchParams.delete('modal'); history.replaceState(null, '', url); }
        }
        if (dirty && !force) {
          window.crmConfirm({ title: 'Discard changes?', body: 'You have unsaved changes in this form.', ok: 'Discard changes', cancel: 'Keep editing', danger: true })
            .then(function (r) { if (r) doClose(); });
        } else { doClose(); }
      }
    });
  });

  // Trap focus inside the open modal.
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Tab') return;
    var openModal = document.querySelector('[data-modal]:not([style*="display: none"])');
    if (!openModal || openModal.offsetParent === null && getComputedStyle(openModal).position !== 'fixed') return;
    var focusables = openModal.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])');
    if (!focusables.length) return;
    var first = focusables[0], last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
    else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
  });

  /* ------------------------------------------------------------ submit states (2.2) */
  function setLoading(btn, on) {
    if (!btn) return;
    if (on) {
      if (btn.dataset.originalHtml === undefined) btn.dataset.originalHtml = btn.innerHTML;
      var text = btn.dataset.loadingText || 'Working…';
      btn.innerHTML = '<span class="btn-spinner" aria-hidden="true"></span><span>' + text.replace(/</g, '&lt;') + '</span>';
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
    } else {
      if (btn.dataset.originalHtml !== undefined) btn.innerHTML = btn.dataset.originalHtml;
      btn.disabled = false;
      btn.removeAttribute('aria-busy');
    }
  }
  window.crmSetLoading = setLoading;

  function proceedSubmit(form, submitter) {
    if (form.dataset.submitting === '1') return false;
    form.dataset.submitting = '1';
    // A disabled button is not submitted; carry its name/value in a hidden field.
    if (submitter && submitter.name) {
      var h = form.querySelector('input[type=hidden][data-submitter]');
      if (!h) { h = document.createElement('input'); h.type = 'hidden'; h.setAttribute('data-submitter', '1'); form.appendChild(h); }
      h.name = submitter.name; h.value = submitter.value;
    }
    var btns = submitter ? [submitter] : form.querySelectorAll('button[type=submit], button:not([type])');
    Array.prototype.forEach.call(btns, function (b) { setLoading(b, true); });
    Array.prototype.forEach.call(form.querySelectorAll('button[type=submit], button:not([type])'), function (b) { b.disabled = true; });
    // Loading timeout (2.2 rule 2): never assume the record was created.
    form._crmTimer = setTimeout(function () {
      form.dataset.submitting = '0';
      Array.prototype.forEach.call(form.querySelectorAll('button'), function (b) { setLoading(b, false); });
      window.crmToast('This is taking longer than usual. Check Recent activity before trying again.', 'warning');
    }, 30000);
    return true;
  }

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (form.method && form.method.toLowerCase() === 'get') return;
    if (form.hasAttribute('data-no-loading')) return;
    var submitter = e.submitter || form.querySelector('button[type=submit], button:not([type])');
    if (form.dataset.submitting === '1') { e.preventDefault(); return; }
    var needsConfirm = form.dataset.confirm || (submitter && submitter.dataset.confirm);
    if (needsConfirm && form.dataset.confirmed !== '1') {
      e.preventDefault();
      var src = (submitter && submitter.dataset.confirm) ? submitter : form;
      window.crmConfirm({
        title: src.dataset.confirm,
        body: src.dataset.confirmBody || '',
        ok: src.dataset.confirmOk || (submitter ? submitter.textContent.trim() : 'Confirm'),
        cancel: src.dataset.confirmCancel || 'Cancel',
        danger: src.dataset.confirmDanger === '1',
        reason: src.dataset.confirmReason === 'required',
        reasonMin: parseInt(src.dataset.confirmReasonMin || '10', 10)
      }).then(function (r) {
        if (!r) return;
        if (src.dataset.confirmReason === 'required') {
          var field = form.querySelector('input[name=reason][data-injected]');
          if (!field) { field = document.createElement('input'); field.type = 'hidden'; field.name = 'reason'; field.setAttribute('data-injected', '1'); form.appendChild(field); }
          field.value = r.reason;
        }
        form.dataset.confirmed = '1';
        // form.submit() drops the clicked button, so carry its name/value across.
        if (submitter && submitter.name) {
          var sv = form.querySelector('input[data-submitter]');
          if (!sv) { sv = document.createElement('input'); sv.type = 'hidden'; sv.setAttribute('data-submitter', '1'); form.appendChild(sv); }
          sv.name = submitter.name; sv.value = submitter.value;
        }
        if (proceedSubmit(form, submitter)) {
          HTMLFormElement.prototype.submit.call(form);
        }
      });
      return;
    }
    if (!proceedSubmit(form, submitter)) e.preventDefault();
  });

  // Restore buttons when navigating back (bfcache).
  window.addEventListener('pageshow', function (ev) {
    if (!ev.persisted) return;
    Array.prototype.forEach.call(document.querySelectorAll('form'), function (f) {
      f.dataset.submitting = '0'; f.dataset.confirmed = '0';
      Array.prototype.forEach.call(f.querySelectorAll('button'), function (b) { setLoading(b, false); });
    });
  });

  /* ------------------------------------------------------------ clickable list rows (2.7) */
  // A row/card marked data-href opens its detail page when clicked anywhere
  // that isn't itself an interactive element (link, button, checkbox, etc.).
  function rowTarget(e) {
    var el = e.target.closest('[data-href]');
    if (!el || !el.dataset.href) return null;
    if (e.target.closest('a, button, input, select, textarea, label, [data-no-row-link]')) return null;
    var sel = window.getSelection();
    if (sel && String(sel).length) return null;
    return el;
  }
  document.addEventListener('click', function (e) {
    var el = rowTarget(e);
    if (!el) return;
    if (e.ctrlKey || e.metaKey) { window.open(el.dataset.href, '_blank'); return; }
    window.location.href = el.dataset.href;
  });
  document.addEventListener('auxclick', function (e) {
    if (e.button !== 1) return;
    var el = rowTarget(e);
    if (!el) return;
    e.preventDefault();
    window.open(el.dataset.href, '_blank');
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter') return;
    var el = e.target.closest('[data-href]');
    if (!el || e.target !== el) return;
    window.location.href = el.dataset.href;
  });

  /* ------------------------------------------------------------ list helpers (2.7) */
  var debounceTimer = null;
  document.addEventListener('input', function (e) {
    if (!e.target.matches('[data-debounce-submit]')) return;
    clearTimeout(debounceTimer);
    var form = e.target.form;
    debounceTimer = setTimeout(function () { if (form) form.requestSubmit ? form.requestSubmit() : form.submit(); }, 300);
  });
  document.addEventListener('change', function (e) {
    if (e.target.matches('[data-autosubmit]') && e.target.form) {
      e.target.form.requestSubmit ? e.target.form.requestSubmit() : e.target.form.submit();
    }
  });

  // Bulk selection: checkbox column, bar with count.
  function refreshBulk(scope) {
    var items = scope.querySelectorAll('[data-bulk-item]');
    var checked = Array.prototype.filter.call(items, function (c) { return c.checked; });
    var bar = scope.querySelector('[data-bulk-bar]');
    var count = scope.querySelector('[data-bulk-count]');
    if (count) count.textContent = checked.length;
    if (bar) bar.classList.toggle('hidden', checked.length === 0);
    Array.prototype.forEach.call(scope.querySelectorAll('[data-bulk-requires]'), function (b) { b.disabled = checked.length === 0; });
  }
  document.addEventListener('change', function (e) {
    var scope = e.target.closest('[data-bulk-scope]');
    if (!scope) return;
    if (e.target.matches('[data-bulk-all]')) {
      Array.prototype.forEach.call(scope.querySelectorAll('[data-bulk-item]'), function (c) { c.checked = e.target.checked; });
    }
    if (e.target.matches('[data-bulk-all], [data-bulk-item]')) refreshBulk(scope);
  });

  /* ------------------------------------------------------------ copy (2.10) */
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-copy]');
    if (!b) return;
    e.preventDefault();
    var text = b.dataset.copy;
    var done = function () { window.crmToast(b.dataset.copyMessage || 'Copied to clipboard', 'success'); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
    } else { fallbackCopy(text); done(); }
  });
  function fallbackCopy(text) {
    var ta = document.createElement('textarea'); ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (err) { /* ignore */ }
    ta.remove();
  }

  /* ------------------------------------------------------------ JSON helper */
  window.crmPost = function (url, data) {
    var body = new FormData();
    Object.keys(data || {}).forEach(function (k) { body.append(k, data[k]); });
    var ctrl = new AbortController();
    var t = setTimeout(function () { ctrl.abort(); }, 30000);
    return fetch(url, {
      method: 'POST', body: body, credentials: 'same-origin', signal: ctrl.signal,
      headers: { 'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' }
    }).then(function (r) {
      clearTimeout(t);
      return r.json().catch(function () { return { ok: false, error: 'Unexpected response (' + r.status + ').' }; })
        .then(function (j) { j._status = r.status; return j; });
    }).catch(function () {
      clearTimeout(t);
      return { ok: false, error: 'This is taking longer than usual. Check Recent activity before trying again.' };
    });
  };

  /* ------------------------------------------------------------ kanban drag (5.2, 10.2) */
  function initKanban(root) {
    var dragging = null, origin = null, originNext = null;
    root.addEventListener('dragstart', function (e) {
      var card = e.target.closest('[data-card]');
      if (!card) return;
      dragging = card; origin = card.parentElement; originNext = card.nextElementSibling;
      card.classList.add('is-dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', card.dataset.id); } catch (err) { /* IE */ }
    });
    root.addEventListener('dragend', function () { if (dragging) dragging.classList.remove('is-dragging'); });
    root.addEventListener('dragover', function (e) {
      var col = e.target.closest('[data-column]');
      if (!col || !dragging) return;
      e.preventDefault();
      Array.prototype.forEach.call(root.querySelectorAll('[data-column]'), function (c) { c.classList.toggle('is-over', c === col); });
    });
    root.addEventListener('drop', function (e) {
      var col = e.target.closest('[data-column]');
      Array.prototype.forEach.call(root.querySelectorAll('[data-column]'), function (c) { c.classList.remove('is-over'); });
      if (!col || !dragging) return;
      e.preventDefault();
      var list = col.querySelector('[data-cards]') || col;
      if (list === origin) { dragging = null; return; }
      var card = dragging;
      list.prepend(card);
      dragging = null;
      window.crmPost(card.dataset.moveUrl, { to: col.dataset.column }).then(function (res) {
        if (res.ok) {
          window.crmToast(res.message || 'Moved', 'success');
          if (res.reload) window.location.reload();
        } else {
          // Invalid transition snaps back with a message (5.2).
          if (originNext && originNext.parentElement === origin) origin.insertBefore(card, originNext); else origin.appendChild(card);
          if (res.redirect) { window.location.href = res.redirect; return; }
          window.crmToast(res.error || 'That move is not allowed.', 'error');
        }
      });
    });
  }

  /* ------------------------------------------------------------ deep-linked modal (2.6) */
  // Registered at script-load time, not inside boot(): Alpine fires 'alpine:initialized'
  // before 'DOMContentLoaded' (deferred scripts run before that event), so a listener
  // added only once DOMContentLoaded fires always misses it and the modal never opens —
  // which is exactly how a server-side validation error silently failed to show itself.
  (function () {
    var params = new URLSearchParams(window.location.search);
    var modal = params.get('modal') || (document.body && document.body.dataset.openModal);
    if (!modal) return;
    if (window.Alpine && window.Alpine.store('modal')) {
      window.Alpine.store('modal').show(modal);
    } else {
      document.addEventListener('alpine:initialized', function () { window.Alpine.store('modal').show(modal); });
    }
  })();

  /* ------------------------------------------------------------ boot */
  function boot() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-href]'), function (el) {
      if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '0');
    });
    Array.prototype.forEach.call(document.querySelectorAll('.crm-toast'), wireToast);
    Array.prototype.forEach.call(document.querySelectorAll('[data-kanban]'), initKanban);
    Array.prototype.forEach.call(document.querySelectorAll('[data-bulk-scope]'), refreshBulk);
    // Focus the first error after a failed submit (2.2 'Error').
    var summary = document.querySelector('[data-form-summary]');
    var firstInvalid = document.querySelector('[aria-invalid="true"]');
    if (firstInvalid && !firstInvalid.closest('[data-modal]')) firstInvalid.focus();
    else if (summary) summary.focus();
    var off = document.getElementById('offline-banner');
    if (off) {
      window.addEventListener('offline', function () { off.classList.remove('hidden'); });
      window.addEventListener('online', function () { off.classList.add('hidden'); window.crmToast('Connection restored.', 'success'); });
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
