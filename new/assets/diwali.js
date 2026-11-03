/* BThinkX Diwali mode.
 *
 * The site decides whether the Diwali Special runs (Django admin > Site settings). Each visitor then decides
 * whether to see it: the Bee asks once, in an iOS-style alert, "Turn on Diwali mode?". The answer is kept in
 * localStorage (btx_diwali_mode = on | off) and can be changed with the switch at the bottom-left.
 *
 *   Diwali mode on   a string of bulbs hanging from a wavy wire across the top of the page, and, when the
 *                    admin allows them, WebGL glitter and crackers.
 *   Diwali mode off  none of that; the offer text and links stay as normal page content.
 *
 * Human events -> crackers
 *   tap / click          a burst where the finger or cursor is (every 3rd one is a bigger flower)
 *   double click / tap   a finale: five bursts across the screen
 *   scroll               a rocket climbs and explodes for every ~700px scrolled
 *   hover the offer      a sparkler fizzes from the button; pressing it fires a salvo
 *   moving the mouse     a light trail of glitter
 *   sending a form       a celebration over the form
 *   the diya button      lights up a burst (or asks to turn Diwali mode on)
 *
 * Rendering: one WebGL canvas, every particle a GL point drawn additively in a single draw call. It is created
 * lazily after the page is idle, sleeps when nothing moves or the tab is hidden, and does nothing for visitors
 * who prefer reduced motion or whose browser has no WebGL.
 */
(function () {
  'use strict';

  var KEY = 'btx_diwali_mode';
  var root = document.documentElement;
  var host = document.getElementById('diwali-fx');
  var glitterOn = !!host && host.getAttribute('data-glitter') === '1';
  var crackersOn = !!host && host.getAttribute('data-crackers') === '1';
  var reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var coarse = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);

  /* ====================================================================== visitor mode */
  var mode = '';
  try { mode = localStorage.getItem(KEY) || ''; } catch (e) { /* private mode: asked again next visit */ }
  var switchBtn = null;

  function isOn() { return mode === 'on'; }

  function setMode(value) {
    mode = value;
    try { localStorage.setItem(KEY, value); } catch (e) { /* ignore */ }
    root.classList.toggle('btx-diwali-on', isOn());
    paintSwitch();
    document.dispatchEvent(new CustomEvent('btx:diwali-mode', { detail: { on: isOn() } }));
  }

  function paintSwitch() {
    if (!switchBtn) return;
    switchBtn.setAttribute('aria-pressed', isOn() ? 'true' : 'false');
    switchBtn.textContent = isOn() ? 'Diwali mode: on' : 'Diwali mode: off';
    switchBtn.title = isOn() ? 'Turn Diwali mode off' : 'Turn Diwali mode on';
  }

  function buildSwitch() {
    switchBtn = document.createElement('button');
    switchBtn.type = 'button';
    switchBtn.className = 'diwali-mute';
    switchBtn.addEventListener('click', function () { setMode(isOn() ? 'off' : 'on'); });
    document.body.appendChild(switchBtn);
    paintSwitch();
  }

  function toast(title, sub) {
    var el = document.createElement('div');
    el.className = 'diwali-toast';
    el.setAttribute('role', 'status');
    el.innerHTML = '<strong></strong><small></small>';
    el.firstChild.textContent = title;
    el.lastChild.textContent = sub;
    document.body.appendChild(el);
    requestAnimationFrame(function () { el.classList.add('is-in'); });
    setTimeout(function () {
      el.classList.remove('is-in');
      setTimeout(function () { el.remove(); }, 450);
    }, 4600);
  }

  /* ---------- string of bulbs: a wavy wire with bulbs hanging from it, at most 10% of the screen tall ---------- */
  var BULB_COLOURS = ['#ffc857', '#a5a4fa', '#5eead4', '#ff9f1c'];
  var lightsTimer = 0;

  function buildLights() {
    var el = document.querySelector('.diwali-lights');
    if (!el || !isOn()) return;
    var W = window.innerWidth, H = window.innerHeight;
    var avail = Math.round(Math.max(56, Math.min(H * 0.10, 120)));      // the 10% of the screen it may use
    el.style.height = avail + 'px';
    var CORD = 7, BULB = 14;                                            // cord length, bulb height
    var low = avail - CORD - BULB - 4;                                  // lowest the wire may sag to
    var swags = W < 640 ? 1.7 : W < 1100 ? 2.7 : 3.6;                   // how many loops across the width

    // an uneven, hand-hung curve: two sines of different length plus a slow drift, always inside [0, 1]
    function f(x) {
      var u = x / W;
      var v = 0.5 + 0.27 * Math.sin(6.2832 * swags * u - 1.3) + 0.15 * Math.sin(6.2832 * swags * 1.85 * u + 0.8) +
              0.06 * Math.sin(6.2832 * 0.7 * u + 2.1);
      return Math.max(0, Math.min(1, v));
    }
    function y(x) { return 4 + (low - 4) * f(x); }

    var d = '';
    for (var x = -10; x <= W + 10; x += 6) d += (d ? 'L' : 'M') + x + ' ' + y(x).toFixed(1);

    var gap = W < 640 ? 26 : 32;
    var n = Math.max(6, Math.floor(W / gap));
    var bulbs = '';
    for (var i = 0; i < n; i++) {
      var bx = (i + 0.5) * W / n, by = y(bx), c = BULB_COLOURS[i % 4];
      bulbs += '<g class="b' + (i % 4) + '">' +
        '<circle cx="' + bx.toFixed(1) + '" cy="' + (by + CORD + 6.5).toFixed(1) + '" r="11" fill="' + c + '" opacity=".22"/>' +
        '<ellipse cx="' + bx.toFixed(1) + '" cy="' + (by + CORD + 7).toFixed(1) + '" rx="4.4" ry="6.4" fill="' + c + '"/></g>' +
        '<path class="wire" d="M' + bx.toFixed(1) + ' ' + by.toFixed(1) + 'v' + CORD + '"/>' +
        '<rect class="cap" x="' + (bx - 1.8).toFixed(1) + '" y="' + (by + CORD - 1).toFixed(1) + '" width="3.6" height="3" rx="1"/>';
    }
    el.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + avail + '" preserveAspectRatio="none" aria-hidden="true" focusable="false">' +
      '<path class="wire" d="' + d + '"/>' + bulbs + '</svg>';
  }

  window.addEventListener('resize', function () {
    clearTimeout(lightsTimer);
    lightsTimer = setTimeout(buildLights, 150);
  });
  document.addEventListener('btx:diwali-mode', buildLights);

  /* ---------- the Bee asks (iOS-style alert) ---------- */
  var asking = false;

  function openAsk() {
    if (asking) return;
    asking = true;
    var opener = document.activeElement;
    var face = window.btxBeeFace ? window.btxBeeFace() : '';
    var wrap = document.createElement('div');
    wrap.className = 'diwali-ask';
    wrap.innerHTML =
      '<div class="diwali-ask__box" role="alertdialog" aria-modal="true" aria-labelledby="dAskT" aria-describedby="dAskD">' +
        '<div class="diwali-ask__body">' +
          (face ? '<div class="diwali-ask__bee" aria-hidden="true">' + face + '</div>' : '') +
          '<h2 id="dAskT">Turn on Diwali mode?</h2>' +
          '<p id="dAskD">Hi, I am Bee. Want to see BThinkX in festive mode? You get string lights, golden glitter and crackers that pop when you tap.</p>' +
        '</div>' +
        '<div class="diwali-ask__actions">' +
          '<button type="button" data-act="no">Not now</button>' +
          '<button type="button" data-act="yes" class="is-primary">Turn On</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(wrap);
    var yes = wrap.querySelector('[data-act="yes"]');
    var no = wrap.querySelector('[data-act="no"]');
    requestAnimationFrame(function () { wrap.classList.add('is-in'); yes.focus(); });

    function close(answer) {
      document.removeEventListener('keydown', onKey, true);
      wrap.classList.remove('is-in');
      setTimeout(function () { wrap.remove(); }, 260);
      asking = false;
      if (opener && opener.focus) { try { opener.focus(); } catch (e) { /* ignore */ } }
      setMode(answer);
      if (answer === 'on') toast('Diwali mode is on', crackersOn ? (coarse ? 'Tap' : 'Click') + ' anywhere for crackers.' : 'Happy Diwali from BThinkX.');
    }
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close('off'); }
      else if (e.key === 'Tab') {                       // keep focus inside the alert
        e.preventDefault();
        (document.activeElement === yes ? no : yes).focus();
      }
    }
    document.addEventListener('keydown', onKey, true);
    yes.addEventListener('click', function () { close('on'); });
    no.addEventListener('click', function () { close('off'); });
  }

  // The Bee asks once it has landed on the chat icon (bee.js sets the flag / fires the event);
  // without a Bee on the page it asks a little after the page has loaded.
  function scheduleAsk() {
    if (mode) return;
    var done = false;
    function go(delay) {
      if (done) return;
      done = true;
      setTimeout(function () { if (!mode) openAsk(); }, delay);
    }
    if (window.__btxBeeSeated) { go(1200); return; }
    document.addEventListener('btx:bee-seated', function () { go(1400); }, { once: true });
    setTimeout(function () { go(0); }, document.querySelector('.btx-chat-launcher') ? 16000 : 2500);
  }

  document.addEventListener('click', function (e) {
    var diya = e.target.closest && e.target.closest('.diwali-note__icon');
    if (!diya) return;
    if (!isOn()) { openAsk(); return; }
    var fx = window.btxDiwali;
    var r = diya.getBoundingClientRect();
    if (fx) fx.burst(r.left + r.width / 2, r.top, { count: 36, power: 3.4, gold: true, flower: true });
  });

  /* ====================================================================== WebGL effects */
  function initEffects(host) {
    var MAX_PARTICLES = 640;
    var FLOATS = 9;                         // x, y, size, r, g, b, a, kind, (pad)

    function rgb(hex) {
      var n = parseInt(hex.slice(1), 16);
      return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
    }
    function palette(list) { return list.map(rgb); }
    var GOLD = palette(['#ffd166', '#ffc857', '#ffb703', '#ff9f1c', '#fff1b8']);
    var FIREWORK = palette(['#ffd166', '#ff5d73', '#ff9f1c', '#5eead4', '#a5a4fa', '#ffffff', '#7bf1a8']);
    var WHITE = rgb('#ffffff');

    var muted = !isOn();
    var canvas = null, gl = null, program = null, buffer = null, loc = null;
    var data = new Float32Array(MAX_PARTICLES * FLOATS + 120 * FLOATS);
    var failed = false;
    var W = window.innerWidth, H = window.innerHeight, dpr = Math.min(window.devicePixelRatio || 1, 2);
    var particles = [], rockets = [], ambient = [];
    var running = false, lastFrame = 0, idleReady = false;

    function rand(a, b) { return a + Math.random() * (b - a); }
    function pick(list) { return list[Math.floor(Math.random() * list.length)]; }
    function blocked() { return !crackersOn || muted || failed; }

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
      var colours = opts.gold ? GOLD : [pick(FIREWORK), pick(FIREWORK), WHITE];
      var power = opts.power || rand(2.2, 4.2);
      for (var i = 0; i < n; i++) {
        var a = (i / n) * Math.PI * 2 + rand(-0.05, 0.05);
        var sp = opts.flower ? power * (0.55 + 0.45 * Math.abs(Math.sin(a * 3))) : power * rand(0.45, 1);
        spark(x, y, Math.cos(a) * sp, Math.sin(a) * sp, rand(45, 80), pick(colours), rand(1.6, 3), 0.045);
      }
      for (var k = 0; k < 8; k++) spark(x, y, rand(-1, 1), rand(-1.2, 0.4), rand(70, 110), pick(GOLD), rand(1.2, 2.2), 0.02);
      start();
    }

    function launchRocket(x, targetY) {
      if (blocked()) return;
      rockets.push({ x: x, y: H + 10, vy: -rand(8, 11), targetY: targetY, vx: rand(-0.4, 0.4) });
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
      if (alive && !document.hidden) requestAnimationFrame(frame); else running = false;
    }

    /* ---------- human events ---------- */
    function isFormField(el) {
      return !!(el && el.closest && el.closest('input, textarea, select, [contenteditable="true"]'));
    }
    function inUi(el) {
      return !!(el && el.closest && el.closest('.diwali-mute, .diwali-ask'));
    }

    var clicks = 0, lastBurst = 0;
    document.addEventListener('pointerdown', function (e) {
      if (blocked() || e.button > 0 || isFormField(e.target) || inUi(e.target)) return;
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
      if (blocked() || isFormField(e.target) || inUi(e.target)) return;
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

    document.addEventListener('submit', function (e) {
      var form = e.target;
      if (blocked() || !form || (form.matches && form.matches('[data-no-fx]'))) return;
      if (typeof form.checkValidity === 'function' && !form.checkValidity()) return;
      var r = form.getBoundingClientRect();
      finale(Math.min(Math.max(r.left + r.width / 2, 60), W - 60), Math.min(Math.max(r.top + 60, 80), H - 80));
    }, true);

    document.addEventListener('btx:celebrate', function () { finale(); });

    // The visitor turns Diwali mode on or off.
    document.addEventListener('btx:diwali-mode', function (e) {
      muted = !e.detail.on;
      particles.length = 0;
      rockets.length = 0;
      seedAmbient();
      if (!muted) { start(); setTimeout(function () { launchRocket(W * 0.5, H * 0.3); }, 350); }
    });

    var resizeTimer = 0;
    window.addEventListener('resize', function () { clearTimeout(resizeTimer); resizeTimer = setTimeout(resize, 150); });
    document.addEventListener('visibilitychange', function () { if (!document.hidden) start(); });

    window.btxDiwali = { burst: burst, finale: finale, rocket: launchRocket };

    // Nothing touches WebGL until the page has finished loading and the browser is idle.
    function whenIdle() {
      idleReady = true;
      seedAmbient();
      if (glitterOn && !muted) start();
    }
    function afterLoad() {
      if (window.requestIdleCallback) window.requestIdleCallback(whenIdle, { timeout: 2500 });
      else setTimeout(whenIdle, 1500);
    }
    if (document.readyState === 'complete') afterLoad();
    else window.addEventListener('load', afterLoad, { once: true });
  }

  /* ====================================================================== start */
  root.classList.toggle('btx-diwali-on', isOn());
  buildSwitch();
  buildLights();
  if (host && (glitterOn || crackersOn) && !reduceMotion) initEffects(host);
  scheduleAsk();
})();
