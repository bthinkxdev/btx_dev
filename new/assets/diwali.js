/* BThinkX Diwali: WebGL glitter and crackers that react to what the visitor does, plus the lead card.
 *
 * Switched on and off from Django admin (Site settings). The server renders #diwali-fx only when
 * glitter or crackers is enabled, and sets data-glitter / data-crackers per effect.
 *
 * Human events -> crackers
 *   tap / click          a burst where the finger or cursor is (every 3rd one is a bigger flower)
 *   double click / tap   a finale: five bursts across the screen
 *   scroll               a rocket climbs and explodes for every ~700px scrolled
 *   hover the offer      a sparkler fizzes from the button; pressing it fires a salvo
 *   moving the mouse     a light trail of glitter
 *   sending a form       a celebration over the form
 *   lighting the diya    the diya button in the offer note
 *
 * Rendering: one WebGL canvas, every particle is a GL point drawn additively in a single draw call.
 * It is created lazily after the page is idle, sleeps when nothing is moving or the tab is hidden, and
 * does nothing for visitors who prefer reduced motion or whose browser has no WebGL.
 * Visitors can switch the effects off with the button at the bottom-left (kept in localStorage).
 */
(function () {
  'use strict';

  var host = document.getElementById('diwali-fx');
  var glitterOn = !!host && host.getAttribute('data-glitter') === '1';
  var crackersOn = !!host && host.getAttribute('data-crackers') === '1';
  var reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  if (host && (glitterOn || crackersOn) && !reduceMotion) initEffects(host, glitterOn, crackersOn);
  initLeadCard();

  /* ====================================================================== effects */
  function initEffects(host, glitterOn, crackersOn) {
    var STORE_KEY = 'btx_diwali_fx';
    var MAX_PARTICLES = 640;
    var FLOATS = 9;                         // x, y, size, r, g, b, a, kind, (pad)
    var coarse = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);

    function rgb(hex) {
      var n = parseInt(hex.slice(1), 16);
      return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
    }
    function palette(list) { return list.map(rgb); }
    var GOLD = palette(['#ffd166', '#ffc857', '#ffb703', '#ff9f1c', '#fff1b8']);
    var FIREWORK = palette(['#ffd166', '#ff5d73', '#ff9f1c', '#5eead4', '#a5a4fa', '#ffffff', '#7bf1a8']);
    var WHITE = rgb('#ffffff');

    var muted = false;
    try { muted = localStorage.getItem(STORE_KEY) === 'off'; } catch (e) { /* private mode */ }
    var override = 0;                       // >0 while a cracker the visitor lit by hand is playing

    var canvas = null, gl = null, program = null, buffer = null, loc = null;
    var data = new Float32Array(MAX_PARTICLES * FLOATS + 120 * FLOATS);
    var failed = false;
    var W = window.innerWidth, H = window.innerHeight, dpr = Math.min(window.devicePixelRatio || 1, 2);
    var particles = [], rockets = [], ambient = [];
    var running = false, lastFrame = 0, idleReady = false;

    function rand(a, b) { return a + Math.random() * (b - a); }
    function pick(list) { return list[Math.floor(Math.random() * list.length)]; }
    function blocked() { return !crackersOn || (muted && !override) || failed; }

    /* ---------- WebGL setup (lazy) ---------- */
    var VERT = 'attribute vec2 a_pos;attribute float a_size;attribute vec4 a_color;attribute float a_kind;' +
      'uniform vec2 u_res;varying vec4 v_color;varying float v_kind;' +
      'void main(){vec2 c=(a_pos/u_res)*2.0-1.0;gl_Position=vec4(c.x,-c.y,0.0,1.0);' +
      'gl_PointSize=a_size;v_color=a_color;v_kind=a_kind;}';
    var FRAG = 'precision mediump float;varying vec4 v_color;varying float v_kind;' +
      'void main(){vec2 p=gl_PointCoord*2.0-1.0;float d=length(p);' +
      'float core=1.0-smoothstep(0.0,1.0,d);core*=core;' +
      'float ray=0.0;if(v_kind>0.5){' +
      'ray=max((1.0-smoothstep(0.0,0.16,abs(p.x)))*(1.0-abs(p.y)),(1.0-smoothstep(0.0,0.16,abs(p.y)))*(1.0-abs(p.x)));}' +
      'float a=clamp(core+ray*0.85,0.0,1.0)*v_color.a;gl_FragColor=vec4(v_color.rgb*a,a);}';

    function shader(type, src) {
      var s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error('shader');
      return s;
    }

    function setupGL() {
      if (!canvas) {
        canvas = document.createElement('canvas');
        canvas.className = 'diwali-canvas';
        canvas.setAttribute('aria-hidden', 'true');
        canvas.addEventListener('webglcontextlost', function (e) { e.preventDefault(); running = false; }, false);
        canvas.addEventListener('webglcontextrestored', function () { try { buildProgram(); start(); } catch (err) { failed = true; } }, false);
        host.insertBefore(canvas, host.firstChild);
      }
      gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true, antialias: false, depth: false,
                                        stencil: false, powerPreference: 'low-power' });
      if (!gl) { failed = true; return; }
      try { buildProgram(); } catch (err) { failed = true; return; }
      resize();
    }

    function buildProgram() {
      program = gl.createProgram();
      gl.attachShader(program, shader(gl.VERTEX_SHADER, VERT));
      gl.attachShader(program, shader(gl.FRAGMENT_SHADER, FRAG));
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error('link');
      gl.useProgram(program);
      loc = {
        pos: gl.getAttribLocation(program, 'a_pos'), size: gl.getAttribLocation(program, 'a_size'),
        color: gl.getAttribLocation(program, 'a_color'), kind: gl.getAttribLocation(program, 'a_kind'),
        res: gl.getUniformLocation(program, 'u_res')
      };
      buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, data.byteLength, gl.DYNAMIC_DRAW);
      var stride = FLOATS * 4;
      gl.enableVertexAttribArray(loc.pos);   gl.vertexAttribPointer(loc.pos, 2, gl.FLOAT, false, stride, 0);
      gl.enableVertexAttribArray(loc.size);  gl.vertexAttribPointer(loc.size, 1, gl.FLOAT, false, stride, 8);
      gl.enableVertexAttribArray(loc.color); gl.vertexAttribPointer(loc.color, 4, gl.FLOAT, false, stride, 12);
      gl.enableVertexAttribArray(loc.kind);  gl.vertexAttribPointer(loc.kind, 1, gl.FLOAT, false, stride, 28);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, gl.ONE);          // additive, colours are premultiplied in the shader
    }

    function resize() {
      W = window.innerWidth;
      H = window.innerHeight;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      if (canvas) {
        canvas.width = Math.round(W * dpr);
        canvas.height = Math.round(H * dpr);
      }
      if (gl) gl.viewport(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight);
      seedAmbient();
    }

    /* ---------- ambient glitter ---------- */
    function newGlint(anywhere) {
      return {
        x: rand(0, W), y: anywhere ? rand(0, H) : -10, vy: rand(0.1, 0.38), vx: rand(-0.1, 0.1),
        r: rand(1.2, 3), phase: rand(0, 6.28), speed: rand(0.03, 0.08), color: pick(GOLD)
      };
    }
    function seedAmbient() {
      ambient = [];
      if (!glitterOn || muted || !idleReady) return;
      var count = Math.max(12, Math.min(coarse ? 20 : 36, Math.round(W / 42)));
      for (var i = 0; i < count; i++) ambient.push(newGlint(true));
    }

    /* ---------- particles ---------- */
    function spark(x, y, vx, vy, life, color, size, gravity) {
      if (particles.length >= MAX_PARTICLES) return;
      particles.push({ x: x, y: y, vx: vx, vy: vy, life: life, max: life, c: color, size: size,
                       g: gravity === undefined ? 0.05 : gravity });
    }

    function burst(x, y, opts) {
      if (blocked()) return;
      opts = opts || {};
      var n = opts.count || (coarse ? 26 : 38);
      var colours = opts.palette || [pick(FIREWORK), pick(FIREWORK), WHITE];
      var power = opts.power || rand(2.2, 4.2);
      for (var i = 0; i < n; i++) {
        var a = (i / n) * Math.PI * 2 + rand(-0.05, 0.05);
        var sp = opts.flower ? power * (0.55 + 0.45 * Math.abs(Math.sin(a * 3))) : power * rand(0.45, 1);
        spark(x, y, Math.cos(a) * sp, Math.sin(a) * sp, rand(45, 80), pick(colours), rand(1.6, 3), 0.045);
      }
      for (var k = 0; k < 8; k++) spark(x, y, rand(-1, 1), rand(-1.2, 0.4), rand(70, 110), pick(GOLD), rand(1.2, 2.2), 0.02);
      start();
    }

    function launchRocket(x, targetY, fromY) {
      if (blocked()) return;
      rockets.push({ x: x, y: fromY === undefined ? H + 10 : fromY, vy: -rand(8, 11), targetY: targetY, vx: rand(-0.4, 0.4) });
      start();
    }

    function finale(cx, cy) {
      if (blocked()) return;
      var spots = [[0.5, 0.35], [0.2, 0.5], [0.8, 0.5], [0.33, 0.25], [0.67, 0.25]];
      spots.forEach(function (s, i) {
        setTimeout(function () {
          burst(cx !== undefined && i === 0 ? cx : W * s[0], cy !== undefined && i === 0 ? cy : H * s[1],
                { flower: i % 2 === 0, power: rand(3.2, 5), count: coarse ? 34 : 50 });
        }, i * 170);
      });
    }

    function sparkler(el, ms) {
      if (blocked() || !el) return;
      var until = performance.now() + (ms || 700);
      (function tick() {
        var r = el.getBoundingClientRect();
        if (!r.width) return;
        var x = r.left + r.width / 2 + rand(-r.width / 3, r.width / 3), y = r.top + r.height / 2;
        for (var i = 0; i < 3; i++) {
          var a = rand(0, 6.28);
          spark(x, y, Math.cos(a) * rand(0.8, 2.4), Math.sin(a) * rand(0.8, 2.4) - 0.6, rand(18, 34), pick(GOLD), rand(1.2, 2.2), 0.06);
        }
        start();
        if (performance.now() < until) requestAnimationFrame(tick);
      })();
    }

    /* ---------- the crackers on the hero card ---------- */
    function keepOn(ms) { override++; setTimeout(function () { override--; }, ms); }
    function centre(el) {
      var r = el.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2, top: r.top };
    }
    function timed(ms, every, fn) {
      var end = performance.now() + ms;
      var id = setInterval(function () { if (performance.now() > end) { clearInterval(id); return; } fn(); }, every);
    }

    function fire(kind, el) {
      if (!crackersOn || failed) return;
      var c = centre(el);
      keepOn(3200);
      if (kind === 'rocket') {
        launchRocket(c.x, Math.max(80, c.top - rand(160, 260)), c.y);
        setTimeout(function () { launchRocket(c.x + rand(-60, 60), Math.max(80, c.top - rand(200, 320)), c.y); }, 350);
      } else if (kind === 'anar') {                         // flower pot: a fountain
        timed(1900, 28, function () {
          for (var i = 0; i < 3; i++) {
            spark(c.x + rand(-6, 6), c.y - 6, rand(-1.6, 1.6), -rand(4, 8), rand(40, 70),
                  pick(GOLD.concat([WHITE])), rand(1.4, 2.6), 0.1);
          }
          start();
        });
      } else if (kind === 'chakra') {                       // spinning wheel
        var ang = 0;
        timed(1700, 24, function () {
          ang += 0.55;
          for (var j = 0; j < 4; j++) {
            var a = ang + j * 1.5708;
            spark(c.x + Math.cos(a) * 18, c.y + Math.sin(a) * 18, Math.cos(a + 1.57) * 3.2 + rand(-0.4, 0.4),
                  Math.sin(a + 1.57) * 3.2 + rand(-0.4, 0.4), rand(24, 40), pick([GOLD[0], FIREWORK[3], FIREWORK[1], WHITE]),
                  rand(1.4, 2.4), 0.03);
          }
          start();
        });
      } else {                                              // big boom
        burst(c.x, c.y - 30, { count: coarse ? 60 : 90, power: 6.5, palette: [GOLD[0], FIREWORK[1], WHITE] });
        setTimeout(function () { burst(c.x - 60, c.y - 90, { count: 40, power: 4.2, flower: true }); }, 160);
        setTimeout(function () { burst(c.x + 60, c.y - 90, { count: 40, power: 4.2, flower: true }); }, 280);
      }
    }

    function celebrate() { keepOn(1600); finale(); }

    /* ---------- render loop ---------- */
    function start() {
      if (running || document.hidden || failed) return;
      if (!gl) { setupGL(); if (failed || !gl) return; }
      running = true;
      lastFrame = performance.now();
      requestAnimationFrame(frame);
    }

    function put(n, x, y, size, c, a, kind) {
      var o = n * FLOATS;
      data[o] = x; data[o + 1] = y; data[o + 2] = size * dpr;
      data[o + 3] = c[0]; data[o + 4] = c[1]; data[o + 5] = c[2]; data[o + 6] = a; data[o + 7] = kind;
    }

    function frame(now) {
      if (!gl || gl.isContextLost()) { running = false; return; }
      var dt = Math.min((now - lastFrame) / 16.67, 3);
      lastFrame = now;
      var i, p, n = 0;

      for (i = rockets.length - 1; i >= 0; i--) {
        var r = rockets[i];
        r.x += r.vx * dt; r.y += r.vy * dt; r.vy *= 0.985;
        spark(r.x, r.y, rand(-0.3, 0.3), rand(0.4, 1.2), rand(14, 24), GOLD[0], rand(1.4, 2.4), 0.02);
        if (r.y <= r.targetY || r.vy > -2) {
          burst(r.x, r.y, { flower: Math.random() < 0.4, count: coarse ? 30 : 44 });
          rockets.splice(i, 1);
        }
      }

      for (i = particles.length - 1; i >= 0; i--) {
        p = particles[i];
        p.vy += p.g * dt; p.vx *= 0.985; p.vy *= 0.985;
        p.x += p.vx * dt; p.y += p.vy * dt; p.life -= dt;
        if (p.life <= 0) { particles.splice(i, 1); continue; }
        var t = p.life / p.max;
        put(n++, p.x, p.y, p.size * (2.6 + t * 2.2), p.c, Math.min(1, t * 1.6), 0);
      }

      if (glitterOn && !muted) {
        for (i = 0; i < ambient.length; i++) {
          var g = ambient[i];
          g.y += g.vy * dt; g.x += g.vx * dt + Math.sin(g.phase) * 0.1; g.phase += g.speed * dt;
          if (g.y > H + 10) ambient[i] = g = newGlint(false);
          var tw = 0.3 + 0.7 * Math.abs(Math.sin(g.phase * 2));
          put(n++, g.x, g.y, g.r * 4.5, g.color, tw * 0.6, g.r > 2 ? 1 : 0);
        }
      }

      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      if (n) {
        gl.uniform2f(loc.res, W * dpr, H * dpr);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, data.subarray(0, n * FLOATS));
        gl.drawArrays(gl.POINTS, 0, n);
      }

      var alive = particles.length || rockets.length || (glitterOn && !muted && ambient.length);
      if (alive && !document.hidden) {
        requestAnimationFrame(frame);
      } else {
        running = false;
      }
    }

    /* ---------- human events ---------- */
    function isFormField(el) {
      return !!(el && el.closest && el.closest('input, textarea, select, [contenteditable="true"]'));
    }

    var clicks = 0, lastBurst = 0;
    document.addEventListener('pointerdown', function (e) {
      if (blocked() || e.button > 0 || isFormField(e.target)) return;
      if (e.target.closest && e.target.closest('.diwali-mute')) return;
      var now = performance.now();
      if (now - lastBurst < 110) return;
      lastBurst = now;
      clicks++;
      if (e.target.closest && e.target.closest('.nav-offer, .nav-offer-block, .diwali-note .btn')) {
        burst(e.clientX, e.clientY, { count: coarse ? 40 : 60, power: 4.6, flower: true });
      } else {
        burst(e.clientX, e.clientY, { flower: clicks % 3 === 0 });
      }
    }, { passive: true });

    document.addEventListener('dblclick', function (e) {
      if (blocked() || isFormField(e.target)) return;
      finale(e.clientX, e.clientY);
    });

    var lastTrail = 0;
    document.addEventListener('pointermove', function (e) {
      if (muted || !glitterOn || e.pointerType !== 'mouse' || !idleReady) return;
      var now = performance.now();
      if (now - lastTrail < 40) return;
      lastTrail = now;
      spark(e.clientX + rand(-4, 4), e.clientY + rand(-4, 4), rand(-0.5, 0.5), rand(-0.2, 0.7), rand(22, 36),
            pick(GOLD), rand(1.2, 2), 0.03);
      start();
    }, { passive: true });

    var scrollAcc = 0, lastY = window.pageYOffset, lastRocket = 0;
    window.addEventListener('scroll', function () {
      if (blocked()) return;
      var y = window.pageYOffset;
      scrollAcc += Math.abs(y - lastY);
      lastY = y;
      var now = performance.now();
      if (scrollAcc > 700 && now - lastRocket > 1300 && rockets.length < 3) {
        scrollAcc = 0;
        lastRocket = now;
        launchRocket(rand(W * 0.12, W * 0.88), rand(H * 0.12, H * 0.5));
      }
    }, { passive: true });

    document.addEventListener('pointerover', function (e) {
      var el = e.target.closest && e.target.closest('.nav-offer, .nav-offer-block, .diwali-note .btn');
      if (!el || (e.relatedTarget && el.contains(e.relatedTarget))) return;
      sparkler(el, 650);
    });

    document.addEventListener('click', function (e) {
      var diya = e.target.closest && e.target.closest('.diwali-note__icon');
      if (!diya) return;
      var r = diya.getBoundingClientRect();
      keepOn(2000);
      burst(r.left + r.width / 2, r.top, { count: 36, power: 3.4, palette: GOLD, flower: true });
    });

    document.addEventListener('submit', function (e) {
      var form = e.target;
      if (blocked() || !form || (form.matches && form.matches('[data-no-fx]'))) return;
      if (typeof form.checkValidity === 'function' && !form.checkValidity()) return;
      var r = form.getBoundingClientRect();
      finale(Math.min(Math.max(r.left + r.width / 2, 60), W - 60), Math.min(Math.max(r.top + 60, 80), H - 80));
    }, true);

    document.addEventListener('btx:celebrate', function () { finale(); });

    /* ---------- visitor switch ---------- */
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'diwali-mute';
    host.appendChild(btn);

    function paintButton() {
      btn.setAttribute('aria-pressed', muted ? 'true' : 'false');
      btn.textContent = muted ? 'Diwali effects: off' : 'Diwali effects: on';
      btn.title = muted ? 'Turn the Diwali effects on' : 'Turn the Diwali effects off';
    }
    paintButton();

    btn.addEventListener('click', function () {
      muted = !muted;
      try { localStorage.setItem(STORE_KEY, muted ? 'off' : 'on'); } catch (e) { /* ignore */ }
      paintButton();
      particles.length = 0;
      rockets.length = 0;
      seedAmbient();
      if (!muted) { start(); burst(W / 2, H / 2, { flower: true, power: 4 }); }
    });

    /* ---------- greeting, once per session ---------- */
    function greet() {
      var key = 'btx_diwali_greeted';
      try { if (sessionStorage.getItem(key)) return; sessionStorage.setItem(key, '1'); } catch (e) { /* ignore */ }
      if (blocked()) return;
      var toast = document.createElement('div');
      toast.className = 'diwali-toast';
      toast.setAttribute('role', 'status');
      toast.innerHTML = '<strong>Happy Diwali from BThinkX</strong><small>' + (coarse ? 'Tap' : 'Click') +
        ' anywhere for crackers.</small>';
      host.appendChild(toast);
      requestAnimationFrame(function () { toast.classList.add('is-in'); });
      setTimeout(function () {
        toast.classList.remove('is-in');
        setTimeout(function () { toast.remove(); }, 450);
      }, 5200);
      setTimeout(function () { launchRocket(W * 0.5, H * 0.3); }, 900);
    }

    var resizeTimer = 0;
    window.addEventListener('resize', function () { clearTimeout(resizeTimer); resizeTimer = setTimeout(resize, 150); });
    document.addEventListener('visibilitychange', function () { if (!document.hidden) start(); });

    window.btxDiwali = { burst: burst, finale: finale, celebrate: celebrate, rocket: launchRocket, fire: fire };

    // Nothing touches WebGL until the page has finished loading and the browser is idle.
    function whenIdle() {
      idleReady = true;
      seedAmbient();
      if (glitterOn && !muted) start();
      setTimeout(greet, 600);
    }
    function afterLoad() {
      if (window.requestIdleCallback) window.requestIdleCallback(whenIdle, { timeout: 2500 });
      else setTimeout(whenIdle, 1500);
    }
    if (document.readyState === 'complete') afterLoad();
    else window.addEventListener('load', afterLoad, { once: true });
  }

  /* ====================================================================== lead card */
  function initLeadCard() {
    var card = document.getElementById('diwaliGame');
    if (!card) return;
    var form = document.getElementById('diwaliLeadForm');
    var thanks = document.getElementById('dgThanks');
    var errorEl = document.getElementById('dgError');
    var submit = document.getElementById('dgSubmit');
    var countEl = document.getElementById('dgCount');
    var barEl = document.getElementById('dgBar');
    var litField = document.getElementById('dgLit');
    var crackers = card.querySelectorAll('.cracker');
    var lit = {};
    var litCount = 0;

    function fx() { return window.btxDiwali; }

    Array.prototype.forEach.call(crackers, function (btn) {
      btn.addEventListener('click', function () {
        var kind = btn.getAttribute('data-kind');
        if (btn.classList.contains('is-burning')) return;
        btn.classList.add('is-burning');
        setTimeout(function () {
          btn.classList.remove('is-burning');
          btn.classList.add('is-lit');
          if (fx()) fx().fire(kind, btn);
          if (kind === 'bomb') {
            card.classList.add('is-shaking');
            setTimeout(function () { card.classList.remove('is-shaking'); }, 500);
          }
        }, 480);
        if (!lit[kind]) {
          lit[kind] = true;
          litCount++;
          if (countEl) countEl.textContent = litCount;
          if (barEl) barEl.style.width = (litCount * 25) + '%';
          if (litField) litField.value = litCount;
          if (form.hidden) {
            form.hidden = false;
            form.classList.add('is-revealed');
          }
          if (litCount === 4 && fx()) setTimeout(function () { fx().celebrate(); }, 1400);
        }
      });
    });

    function showError(msg) {
      errorEl.textContent = msg;
      errorEl.hidden = false;
    }

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      errorEl.hidden = true;
      var name = form.elements.name.value.trim();
      var phone = form.elements.phone.value.replace(/\D/g, '');
      if (name.length < 2) { showError('Please enter your name.'); form.elements.name.focus(); return; }
      if (phone.length < 10) { showError('Enter a valid 10-digit WhatsApp number.'); form.elements.phone.focus(); return; }
      submit.disabled = true;
      var label = submit.textContent;
      submit.textContent = 'Sending...';
      fetch(card.getAttribute('data-lead-url'), {
        method: 'POST', body: new FormData(form), credentials: 'same-origin',
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
      }).then(function (r) {
        return r.json().then(function (data) { return { ok: r.ok, data: data }; });
      }).then(function (res) {
        if (res.ok && res.data.success) {
          form.hidden = true;
          thanks.hidden = false;
          card.classList.add('is-done');
          if (fx()) fx().celebrate();
        } else {
          var errs = res.data && res.data.errors;
          showError(errs ? (errs.__all__ || errs.phone || errs.name || errs[Object.keys(errs)[0]])
                         : 'Something went wrong. Please try again.');
          submit.disabled = false;
          submit.textContent = label;
        }
      }).catch(function () {
        showError('Network error. Please try again.');
        submit.disabled = false;
        submit.textContent = label;
      });
    });
  }
})();
