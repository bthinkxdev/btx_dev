/* Webinar landing page: countdown, the mobile reserve bar, and registration -> Cashfree checkout. */
(function () {
  'use strict';

  /* ---------- countdown ---------- */
  var count = document.getElementById('wbCount');
  if (count) {
    var start = new Date(count.getAttribute('data-start')).getTime();
    var tick = function () {
      var ms = start - Date.now();
      if (isNaN(ms)) { count.textContent = ''; return; }
      if (ms <= 0) { count.textContent = 'Started'; return; }
      var m = Math.floor(ms / 60000), d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60);
      count.textContent = 'Starts in ' + (d ? d + 'd ' : '') + (d || h ? h + 'h ' : '') + (m % 60) + 'm';
    };
    tick();
    setInterval(tick, 30000);
  }

  /* ---------- sticky reserve bar (phones): only while the form is out of view ---------- */
  var bar = document.getElementById('wbSticky');
  var card = document.getElementById('register');
  if (bar && card && 'IntersectionObserver' in window) {
    bar.hidden = false;
    new IntersectionObserver(function (entries) {
      // the form is the first thing on a phone, so the bar appears once the visitor has scrolled past it
      bar.classList.toggle('is-on', !entries[0].isIntersecting);
    }, { threshold: 0.05 }).observe(card);
  }

  /* ---------- registration ---------- */
  var form = document.getElementById('wbForm');
  if (!form) return;
  var btn = document.getElementById('wbSubmit');
  var label = document.getElementById('wbSubmitText');
  var formErr = document.getElementById('wbFormErr');
  var idle = label.textContent;
  var sdkLoading = null;

  // The checkout SDK is only fetched once the visitor starts filling the form, so the page itself stays light.
  function loadSdk() {
    if (window.Cashfree) return Promise.resolve();
    if (sdkLoading) return sdkLoading;
    sdkLoading = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = 'https://sdk.cashfree.com/js/v3/cashfree.js';
      s.onload = resolve;
      s.onerror = function () { sdkLoading = null; reject(new Error('sdk')); };
      document.head.appendChild(s);
    });
    return sdkLoading;
  }
  form.addEventListener('focusin', function () { loadSdk().catch(function () {}); }, { once: true });
  if ('requestIdleCallback' in window) requestIdleCallback(function () { loadSdk().catch(function () {}); }, { timeout: 6000 });

  function busy(on) {
    btn.disabled = on;
    label.textContent = on ? 'Starting secure payment...' : idle;
  }
  function clearErrors() {
    formErr.hidden = true;
    formErr.textContent = '';
    formErr.classList.remove('is-info');
    form.querySelectorAll('.wb-err').forEach(function (e) { e.textContent = ''; });
    form.querySelectorAll('.is-bad').forEach(function (e) { e.classList.remove('is-bad'); });
  }
  function showError(msg, info) {
    formErr.textContent = msg;
    formErr.classList.toggle('is-info', !!info);
    formErr.hidden = false;
    formErr.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  var pixelData = {
    value: Number(form.getAttribute('data-price')), currency: 'INR', content_name: form.getAttribute('data-content-name'),
    content_ids: [form.getAttribute('data-content-id')], content_type: 'product', num_items: 1
  };
  try { if (window.fbq) fbq('track', 'ViewContent', pixelData); } catch (err) { /* tracking must never break the page */ }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    clearErrors();

    var digits = form.elements.phone.value.replace(/\D/g, '');
    var bad = null;
    if (form.elements.name.value.trim().length < 2) bad = ['name', 'Please enter your name.'];
    else if (!/^\S+@\S+\.\S+$/.test(form.elements.email.value.trim())) bad = ['email', 'Enter a valid email address.'];
    else if (!/^(91|0)?[6-9]\d{9}$/.test(digits)) bad = ['phone', 'Enter a valid 10-digit mobile number.'];
    if (bad) {
      form.querySelector('.wb-err[data-field="' + bad[0] + '"]').textContent = bad[1];
      form.elements[bad[0]].classList.add('is-bad');
      form.elements[bad[0]].focus();
      return;
    }

    busy(true);
    Promise.all([
      fetch(form.getAttribute('data-url'), {
        method: 'POST', body: new FormData(form), credentials: 'same-origin',
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
      }).then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); }),
      loadSdk().catch(function () { return null; })
    ]).then(function (out) {
      var res = out[0];
      if (!res.ok || !res.data.success) {
        busy(false);
        var errs = res.data.errors;
        if (errs) {
          Object.keys(errs).forEach(function (k) {
            var slot = form.querySelector('.wb-err[data-field="' + k + '"]');
            if (slot) slot.textContent = errs[k];
            if (form.elements[k]) form.elements[k].classList.add('is-bad');
          });
        } else {
          showError(res.data.error || 'Something went wrong. Please try again.', !!(res.data.already || res.data.info));
        }
        return;
      }
      if (!window.Cashfree) {
        busy(false);
        showError('Payment could not start. Please reload the page and try again.');
        return;
      }
      try {
        var eid = { eventID: res.data.order_id };    // the server sends the same events with this id; Meta merges them
        if (window.fbq) {
          fbq('track', 'Lead', pixelData, eid);
          fbq('track', 'InitiateCheckout', pixelData, eid);
        }
        if (window.gtag) gtag('event', 'begin_checkout', { value: pixelData.value, currency: 'INR' });
      } catch (err) { /* tracking must never block payment */ }
      window.Cashfree({ mode: res.data.mode }).checkout({ paymentSessionId: res.data.payment_session_id, redirectTarget: '_self' });
    }).catch(function () {
      busy(false);
      showError('Network error. Please check your connection and try again.');
    });
  });
})();
