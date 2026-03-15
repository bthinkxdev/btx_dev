/* BThinkX Dev — Shared JS */

(function () {
  'use strict';

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

  /* Standalone blog tabs */
  document.querySelectorAll('.blog-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.blog-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
    });
  });

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
})();
