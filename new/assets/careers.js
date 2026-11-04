/* Careers: resume picker (click or drag and drop) and the AJAX application form. Used on the careers list and on
 * every job page; each page has one section[data-career-apply]. */
(function () {
  'use strict';
  var root = document.querySelector('[data-career-apply]');
  if (!root) return;

  var resumeInput = document.getElementById('careerResume');
  var wrap = document.getElementById('careerFileUpload');
  var hint = document.getElementById('careerResumeHint');
  if (resumeInput && wrap && hint) {
    var defaultHint = hint.textContent;
    resumeInput.addEventListener('change', function () {
      if (resumeInput.files && resumeInput.files[0]) {
        var f = resumeInput.files[0];
        hint.textContent = f.name + ' \u00b7 ' + (f.size / (1024 * 1024)).toFixed(2) + ' MB';
        wrap.classList.add('has-file');
      } else {
        hint.textContent = defaultHint;
        wrap.classList.remove('has-file');
      }
    });
    ['dragenter', 'dragover'].forEach(function (ev) {
      wrap.addEventListener(ev, function (e) { e.preventDefault(); e.stopPropagation(); wrap.classList.add('drag-active'); });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      wrap.addEventListener(ev, function (e) { e.preventDefault(); e.stopPropagation(); wrap.classList.remove('drag-active'); });
    });
    wrap.addEventListener('drop', function (e) {
      var files = e.dataTransfer && e.dataTransfer.files;
      if (!files || !files[0]) return;
      var f = files[0];
      if (!(/\.(pdf|doc|docx)$/i.test(f.name) || /pdf|word|officedocument/.test(f.type || ''))) {
        hint.textContent = 'Please drop a PDF or Word file.';
        wrap.classList.remove('has-file');
        return;
      }
      try {
        var dt = new DataTransfer();
        dt.items.add(f);
        resumeInput.files = dt.files;
      } catch (err) { return; }
      resumeInput.dispatchEvent(new Event('change', { bubbles: true }));
    });
  }

  var form = document.getElementById('careerApplyForm');
  if (!form) return;
  var btn = document.getElementById('careerSubmitBtn');
  var success = document.getElementById('careerFormSuccess');
  var panel = document.getElementById('careerFormPanel');
  var label = btn ? btn.innerHTML : '';

  function fail(msg) {
    if (btn) { btn.disabled = false; btn.innerHTML = label; }
    var el = document.getElementById('careerFormError');
    if (!el) {
      el = document.createElement('p');
      el.id = 'careerFormError';
      el.setAttribute('role', 'alert');
      el.style.cssText = 'margin:12px 0 0;color:#ff8a98;font-size:.9rem;';
      form.appendChild(el);
    }
    el.textContent = msg;
    el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var old = document.getElementById('careerFormError');
    if (old) old.remove();
    if (!form.checkValidity()) { form.reportValidity(); return; }
    if (resumeInput && resumeInput.files[0] && resumeInput.files[0].size > 5 * 1024 * 1024) {
      fail('Your resume is larger than 5 MB. Please attach a smaller file.');
      return;
    }
    if (btn) { btn.disabled = true; btn.textContent = 'Sending...'; }
    fetch(form.action, {
      method: 'POST', body: new FormData(form), credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    }).then(function (r) {
      return r.json().then(function (data) { return { ok: r.ok, data: data }; });
    }).then(function (res) {
      if (res.ok && res.data.success) {
        var to = document.getElementById('careerSentTo');
        if (to) to.textContent = form.elements.email.value;
        if (panel) panel.style.display = 'none';
        if (success) { success.style.display = 'block'; success.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
      } else {
        var errs = res.data && res.data.errors;
        fail((errs && (errs.__all__ || Object.keys(errs).map(function (k) { return errs[k]; }).join(' '))) ||
             'Please check the form and try again.');
      }
    }).catch(function () { fail('Network error. Please try again.'); });
  });
})();
