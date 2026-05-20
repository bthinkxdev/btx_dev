/* BThinkX Dev — Shared JS */

(function () {
  'use strict';

  /* ── Theme (light / dark) — persisted; inline script in <head> prevents flash ── */
  function themeIsLight() {
    return document.documentElement.getAttribute('data-theme') === 'light';
  }
  function themeSyncUI() {
    var light = themeIsLight();
    document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
      btn.setAttribute('aria-checked', light ? 'true' : 'false');
      btn.setAttribute('aria-label', light ? 'Switch to dark mode' : 'Switch to light mode');
      btn.classList.toggle('theme-toggle-btn--light', light);
    });
    document.querySelectorAll('[data-theme-toggle-status]').forEach(function (el) {
      el.textContent = light ? 'Light mode on' : 'Dark mode on';
    });
    var m = document.querySelector('meta[name="theme-color"]');
    if (m) m.setAttribute('content', light ? '#f3f3f8' : '#09090b');
  }
  function themeApply(light) {
    try {
      if (light) {
        document.documentElement.setAttribute('data-theme', 'light');
        localStorage.setItem('theme', 'light');
      } else {
        document.documentElement.removeAttribute('data-theme');
        localStorage.setItem('theme', 'dark');
      }
    } catch (e) {}
    themeSyncUI();
  }
  document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      themeApply(!themeIsLight());
    });
    btn.addEventListener('keydown', function (e) {
      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        themeApply(!themeIsLight());
      }
    });
  });
  themeSyncUI();

  /* ── Ambient graphics: remove after intro fade (saves GPU; reload = replay) ── */
  const ambientGfx = document.querySelector('.ambient-gfx');
  if (ambientGfx && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const finish = function () {
      ambientGfx.classList.add('ambient-gfx--done');
    };
    ambientGfx.addEventListener('animationend', function (e) {
      if (e.target === ambientGfx && e.animationName === 'ambientGfxIntro') finish();
    });
    setTimeout(finish, 2000);
  } else if (ambientGfx && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    ambientGfx.classList.add('ambient-gfx--done');
  }

  /* ── WhatsApp: every 15s highlight + “Connect with us” tooltip ── */
  const waWrap = document.getElementById('whatsappFloatWrap');
  const waTip = document.getElementById('whatsappTooltip');
  const waReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (waWrap && !waReduced) {
    const INTERVAL_MS = 15000;
    const BURST_MS = 3200;
    function waBurst() {
      waWrap.classList.add('wa-burst');
      if (waTip) {
        waTip.setAttribute('aria-hidden', 'false');
      }
      clearTimeout(waWrap._waHide);
      waWrap._waHide = setTimeout(function () {
        waWrap.classList.remove('wa-burst');
        if (waTip) waTip.setAttribute('aria-hidden', 'true');
      }, BURST_MS);
    }
    setTimeout(function () {
      waBurst();
      setInterval(waBurst, INTERVAL_MS);
    }, INTERVAL_MS);
  }

  /* ── Cursor glow ── */
  const glow = document.getElementById('cursor-glow');
  if (glow) {
    window.addEventListener('mousemove', e => {
      glow.style.left = e.clientX + 'px';
      glow.style.top  = e.clientY + 'px';
    });
  }

  /* ── Pill menu toggle ── */
  const pillMenuBtn  = document.getElementById('pillMenuBtn');
  const pillDropdown = document.getElementById('pillDropdown');
  const mobileNav    = document.getElementById('mobileNav');

  function isMobile() { return window.innerWidth <= 768; }

  function closeDropdown() {
    if (!pillDropdown) return;
    pillDropdown.classList.remove('open');
    if (pillMenuBtn) {
      pillMenuBtn.classList.remove('open');
      pillMenuBtn.setAttribute('aria-expanded', 'false');
    }
  }

  function closeMobileNav() {
    if (!mobileNav) return;
    mobileNav.classList.remove('open');
    if (pillMenuBtn) pillMenuBtn.classList.remove('open');
    document.body.style.overflow = '';
  }

  function openMobileNav() {
    if (!mobileNav) return;
    mobileNav.classList.add('open');
    if (pillMenuBtn) pillMenuBtn.classList.add('open');
    document.body.style.overflow = 'hidden';
  }

  /* ── Single delegated click handler — no stopPropagation needed ── */
  document.addEventListener('click', function(e) {
    const menuBtn       = e.target.closest('#pillMenuBtn');
    const dropClose     = e.target.closest('#pillDropdownClose');
    const mobileClose   = e.target.closest('#mobileClose');
    const inNavbar      = e.target.closest('.navbar-pill');
    const inMobileNav   = e.target.closest('#mobileNav');

    if (menuBtn) {
      if (isMobile()) {
        mobileNav && mobileNav.classList.contains('open') ? closeMobileNav() : openMobileNav();
      } else {
        const isOpen = pillDropdown.classList.toggle('open');
        pillMenuBtn.classList.toggle('open', isOpen);
        pillMenuBtn.setAttribute('aria-expanded', String(isOpen));
      }
      return;
    }

    if (dropClose) { closeDropdown(); return; }
    if (mobileClose) { closeMobileNav(); return; }

    /* Click outside both menus → close everything */
    if (!inNavbar && !inMobileNav) { closeDropdown(); }
  });

  /* Escape key closes both */
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeDropdown(); closeMobileNav(); }
  });

  /* ── Scroll-triggered animations ── */
  const observer = new IntersectionObserver(entries => {
    entries.forEach(e => { if (e.isIntersecting) e.target.classList.add('visible'); });
  }, { threshold: 0.08, rootMargin: '0px 0px -32px 0px' });
  document.querySelectorAll('.animate-up, .animate-fade').forEach(el => observer.observe(el));

  /* ── FAQ accordion ── */
  document.querySelectorAll('.faq-item').forEach(item => {
    item.addEventListener('click', () => {
      const wasOpen = item.classList.contains('open');
      document.querySelectorAll('.faq-item').forEach(i => i.classList.remove('open'));
      if (!wasOpen) item.classList.add('open');
    });
  });

  /* ── Portfolio / Blog filter tabs ── */
  document.querySelectorAll('[data-tab-group]').forEach(group => {
    const groupId = group.dataset.tabGroup;
    group.querySelectorAll('.filter-btn, .blog-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        group.querySelectorAll('.filter-btn, .blog-tab').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
      });
    });
  });

  /* Standalone filter btns */
  const filterBtns = document.querySelectorAll('.filter-btn');
  const projectCards = document.querySelectorAll('.project-card[data-category]');
  if (filterBtns.length && projectCards.length) {
    filterBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        filterBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const filter = btn.dataset.filter;
        projectCards.forEach(card => {
          const show = filter === 'all' || card.dataset.category === filter;
          card.style.display = show ? '' : 'none';
          if (show) {
            card.style.gridColumn = card.classList.contains('featured') && filter === 'all' ? 'span 2' : '';
          }
        });
      });
    });
  }

  /* Close menu after clicking a Services submenu link (still jumps to #section) */
  document.querySelectorAll('.pill-dropdown a.services-nav-link, #mobileNav a.services-nav-link').forEach(a => {
    a.addEventListener('click', () => {
      closeDropdown();
      closeMobileNav();
    });
  });

  /* Blog listing: topic filters + hide cards by category */
  const blogFilterTabs = document.querySelectorAll('.blog-filter-tabs [data-blog-filter]');
  if (blogFilterTabs.length) {
    blogFilterTabs.forEach(tab => {
      tab.addEventListener('click', () => {
        const f = tab.getAttribute('data-blog-filter');
        blogFilterTabs.forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        document.querySelectorAll('[data-blog-category]').forEach(el => {
          const c = el.getAttribute('data-blog-category');
          el.style.display = f === 'all' || f === c ? '' : 'none';
        });
      });
    });
  } else {
    document.querySelectorAll('.blog-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.blog-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
      });
    });
  }

  /* ── Contact form ── */
  const contactForm = document.getElementById('contactForm');
  if (contactForm) {
    contactForm.addEventListener('submit', e => {
      e.preventDefault();
      const btn = document.getElementById('submitBtn');
      if (btn) { btn.textContent = 'Sending...'; btn.disabled = true; }
      setTimeout(() => {
        contactForm.style.display = 'none';
        const success = document.getElementById('formSuccess');
        if (success) success.style.display = 'block';
      }, 1200);
    });
  }

  /* ── Ultimate package popup — session: max 2/day, 12hr gap ── */
  const btxBackdrop = document.getElementById('btxUltimateBackdrop');
  const btxToast = document.getElementById('btxUltimateToast');
  const btxForm = document.getElementById('btxUltimateForm');
  const BTX_POPUP_DAY = 'btx_popup_day';
  const BTX_POPUP_COUNT = 'btx_popup_count';
  const BTX_POPUP_LAST = 'btx_popup_last';
  const BTX_GAP_MS = 12 * 60 * 60 * 1000;
  const BTX_MAX_PER_DAY = 2;
  const WA_BASE = 'https://wa.me/917736094292?text=';

  function btxTodayKey() {
    return new Date().toDateString();
  }

  function btxStore() {
    try {
      return window.localStorage;
    } catch (e) {
      return null;
    }
  }

  function btxResetDayIfNeeded() {
    var store = btxStore();
    if (!store) return;
    var today = btxTodayKey();
    if (store.getItem(BTX_POPUP_DAY) !== today) {
      store.setItem(BTX_POPUP_DAY, today);
      store.setItem(BTX_POPUP_COUNT, '0');
    }
  }

  function btxCanShowPromo() {
    var store = btxStore();
    if (!store) return true;
    try {
      btxResetDayIfNeeded();
      var count = parseInt(store.getItem(BTX_POPUP_COUNT) || '0', 10);
      var last = parseInt(store.getItem(BTX_POPUP_LAST) || '0', 10);
      if (count >= BTX_MAX_PER_DAY) return false;
      if (last && Date.now() - last < BTX_GAP_MS) return false;
      return true;
    } catch (e) {
      return true;
    }
  }

  function btxRecordPromoShow() {
    var store = btxStore();
    if (!store) return;
    try {
      btxResetDayIfNeeded();
      var count = parseInt(store.getItem(BTX_POPUP_COUNT) || '0', 10);
      store.setItem(BTX_POPUP_COUNT, String(count + 1));
      store.setItem(BTX_POPUP_LAST, String(Date.now()));
    } catch (e) {}
  }

  function btxOpenModal() {
    if (!btxBackdrop) return;
    btxBackdrop.removeAttribute('hidden');
    btxBackdrop.setAttribute('aria-hidden', 'false');
    requestAnimationFrame(function () {
      btxBackdrop.classList.add('is-open');
    });
    document.body.style.overflow = 'hidden';
  }

  function btxCloseModal(showToastAfter) {
    if (!btxBackdrop) return;
    btxBackdrop.classList.remove('is-open');
    btxBackdrop.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    setTimeout(function () {
      btxBackdrop.setAttribute('hidden', '');
    }, 320);
    if (showToastAfter && btxToast) {
      btxShowToast();
    }
  }

  function btxShowToast() {
    if (!btxToast) return;
    btxToast.removeAttribute('hidden');
    requestAnimationFrame(function () {
      btxToast.classList.add('is-visible');
    });
    clearTimeout(btxToast._hideTimer);
    btxToast._hideTimer = setTimeout(btxHideToast, 12000);
  }

  function btxHideToast() {
    if (!btxToast) return;
    btxToast.classList.remove('is-visible');
    setTimeout(function () {
      btxToast.setAttribute('hidden', '');
    }, 400);
  }

  function btxBuildWaText(name, phone, business, message) {
    var parts = [
      'Hi BThinkX, I want the Ultimate Package (₹99,999).',
      name ? 'Name: ' + name : '',
      phone ? 'Phone: ' + phone : '',
      business ? 'Business: ' + business : '',
      message ? 'Message: ' + message : ''
    ].filter(Boolean);
    return encodeURIComponent(parts.join(' '));
  }

  if (btxBackdrop) {
    var btxCloseBtn = document.getElementById('btxUltimateClose');
    if (btxCloseBtn) {
      btxCloseBtn.addEventListener('click', function () {
        btxCloseModal(true);
      });
    }
    btxBackdrop.addEventListener('click', function (e) {
      if (e.target === btxBackdrop) btxCloseModal(true);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && btxBackdrop.classList.contains('is-open')) {
        btxCloseModal(true);
      }
    });

    document.querySelectorAll('[data-open-ultimate-popup]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        btxOpenModal();
      });
    });

    if (btxForm) {
      btxForm.addEventListener('submit', function (e) {
        e.preventDefault();
        var name = (document.getElementById('btxEnqName') || {}).value || '';
        var phone = (document.getElementById('btxEnqPhone') || {}).value || '';
        var business = (document.getElementById('btxEnqBusiness') || {}).value || '';
        var message = (document.getElementById('btxEnqMessage') || {}).value || '';
        if (!name.trim() || !phone.trim()) {
          if (!name.trim()) document.getElementById('btxEnqName')?.focus();
          else document.getElementById('btxEnqPhone')?.focus();
          return;
        }
        window.open(WA_BASE + btxBuildWaText(name.trim(), phone.trim(), business.trim(), message.trim()), '_blank', 'noopener,noreferrer');
        btxCloseModal(false);
      });
    }

    var toastClose = document.getElementById('btxUltimateToastClose');
    if (toastClose) toastClose.addEventListener('click', btxHideToast);

    if (btxCanShowPromo()) {
      setTimeout(function () {
        if (btxCanShowPromo()) {
          btxRecordPromoShow();
          btxOpenModal();
        }
      }, 2800);
    }
  }
})();
