/* BThinkX mascot bee. On the first page of a session a honeybee flies across the screen (right to left),
 * stops twice to look at the visitor (middle, far left), then swoops over to the chat launcher, lands on it
 * and greets them. Later pages just show it perched there.
 *
 * Easter egg: tapping the bee while it flies splashes it, and on release it drops to the bottom of the screen
 * and stays there; its "soul" then floats up and perches on the chat icon instead.
 *
 * Cost: a single element positioned with a compositor-only transform from requestAnimationFrame for ~11s,
 * after which the loop stops completely. No libraries, no canvas/WebGL; the only layout read is the
 * perch rect while landing. Honours prefers-reduced-motion. */
(function () {
  'use strict';
  if (window.__btxBee) return;
  window.__btxBee = true;

  var SEEN_KEY = 'btx_bee_intro_seen';
  var EDGE = 16;                 // px kept clear between the bee and every screen edge (covers hover bob)
  var PASS_SECONDS = 4.6;        // time to cross the screen (right to left) at full speed
  var HOVER_AT = [0.5, 1.0];     // sweep positions (0..1) where the bee stops to look at the visitor: middle, then far left
  var HOVER_SECONDS = 1.3;
  var LAND_SECONDS = 3.0;        // one smooth swoop from the far left to the chat icon
  var FADE_IN_SECONDS = 0.5;
  var FOLLOW = 7;                // how tightly the bee tracks its path (higher = snappier, lower = floatier)
  var GRAVITY = 2400;            // px/s^2 for the fall
  var CORPSE_TILT = 190;         // deg: lying on its back
  var SOUL_DELAY = 1500;         // ms the bee lies there before its soul rises
  var SOUL_SECONDS = 2.6;
  var DROPS = 16;                // blood drops in a splash

  /* '' (not seen yet), '1' (intro played) or 'killed' (visitor squashed the bee; only its soul remains) */
  function introSeen() {
    try { return window.sessionStorage.getItem(SEEN_KEY) || ''; } catch (e) { return ''; }
  }
  function markIntroSeen(value) {
    try { window.sessionStorage.setItem(SEEN_KEY, value || '1'); } catch (e) { /* private mode: intro may replay */ }
  }

  var DEFS = '<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false"><defs>' +
    '<linearGradient id="btxbAb" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffd84a"/><stop offset=".55" stop-color="#f7ae0c"/><stop offset="1" stop-color="#b86f00"/></linearGradient>' +
    '<radialGradient id="btxbTh" cx=".45" cy=".4" r=".65"><stop offset="0" stop-color="#ffe27a"/><stop offset=".6" stop-color="#e9a30e"/><stop offset="1" stop-color="#8a5200"/></radialGradient>' +
    '<radialGradient id="btxbEye" cx=".35" cy=".3" r=".8"><stop offset="0" stop-color="#7a5430"/><stop offset=".5" stop-color="#20140a"/><stop offset="1" stop-color="#050302"/></radialGradient>' +
    '<linearGradient id="btxbWg" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity=".78"/><stop offset="1" stop-color="#bfe3ff" stop-opacity=".34"/></linearGradient>' +
    '<clipPath id="btxbClipF"><ellipse cx="60" cy="64" rx="15" ry="21"/></clipPath>' +
    '<clipPath id="btxbClip"><path d="M12 52C10 38 28 31 46 33C60 35 68 42 68 50C68 60 56 66 40 65C24 65 13 60 12 52Z"/></clipPath>' +
    '</defs></svg>';

  /* Side view, facing right. Wings pivot at (73,35) and are animated from CSS. */
  var SIDE = '<svg class="btxb btxb-side" viewBox="0 0 120 90" aria-hidden="true" focusable="false">' +
    '<g class="btxb-wing btxb-w2"><path d="M74 36C76 18 60 8 46 13C48 26 60 34 74 37Z" fill="url(#btxbWg)" stroke="#8fb4d6" stroke-opacity=".6" stroke-width=".7"/></g>' +
    '<g class="btxb-legs" fill="none" stroke="#1b130a" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M82 57L90 68L86 76"/><path d="M76 58L78 70L72 79"/><path d="M68 57L62 68L56 74"/>' +
      '<ellipse cx="60" cy="70" rx="3.6" ry="2.6" fill="#f5b81c" stroke="none" transform="rotate(-30 60 70)"/></g>' +
    '<path d="M12 52C10 38 28 31 46 33C60 35 68 42 68 50C68 60 56 66 40 65C24 65 13 60 12 52Z" fill="url(#btxbAb)"/>' +
    '<g clip-path="url(#btxbClip)" fill="#14100a">' +
      '<path d="M8 28L16 28Q20 48 16 70L8 70Z"/><path d="M20 28Q26 48 22 70L31 70Q35 48 29 28Z"/>' +
      '<path d="M36 28Q42 48 38 70L47 70Q51 48 45 28Z"/><path d="M52 28Q58 48 54 70L62 70Q66 48 60 28Z"/>' +
      '<path d="M16 38Q40 30 66 40L66 44Q40 35 16 43Z" fill="#fff" opacity=".16"/></g>' +
    '<path d="M12 51L3 56L12.5 58Z" fill="#2a1a0a"/>' +
    '<ellipse cx="76" cy="45" rx="17" ry="15" fill="url(#btxbTh)"/>' +
    '<ellipse cx="76" cy="45" rx="16.6" ry="14.6" fill="none" stroke="#ffd34e" stroke-width="3" stroke-dasharray="1.2 1.7" opacity=".85"/>' +
    '<circle cx="96" cy="48" r="11" fill="#17110b"/>' +
    '<ellipse cx="100" cy="53.5" rx="5" ry="3" fill="#f2b014"/>' +
    '<g class="btxb-eye"><ellipse cx="99" cy="44" rx="6.2" ry="8" transform="rotate(15 99 44)" fill="url(#btxbEye)"/>' +
    '<circle cx="97" cy="41" r="1.8" fill="#fff" opacity=".85"/></g>' +
    '<path class="btxb-x" d="M94 38L104 49M104 38L94 49" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/>' +
    '<path d="M104 54Q110 55 111 61" fill="none" stroke="#3a2410" stroke-width="2" stroke-linecap="round"/>' +
    '<g class="btxb-ant" fill="none" stroke="#1a1208" stroke-width="1.6" stroke-linecap="round">' +
      '<path d="M100 38Q103 28 111 24"/><path d="M95 37Q94 26 101 20"/>' +
      '<circle cx="111.5" cy="23.6" r="1.7" fill="#1a1208" stroke="none"/><circle cx="101.5" cy="19.6" r="1.7" fill="#1a1208" stroke="none"/></g>' +
    '<g class="btxb-wing btxb-w1"><path d="M72 35C66 10 40 2 24 8C22 22 44 36 72 36Z" fill="url(#btxbWg)" stroke="#8fb4d6" stroke-opacity=".6" stroke-width=".8"/>' +
      '<path d="M71 34C60 22 44 12 28 10M71 35C58 28 44 22 30 16" fill="none" stroke="#8fb4d6" stroke-opacity=".45" stroke-width=".6"/></g>' +
    '</svg>';

  /* Front view (looking at the visitor). Same 120x90 box so the two views cross-fade in place. */
  var FRONT = '<svg class="btxb btxb-front" viewBox="0 0 120 90" aria-hidden="true" focusable="false">' +
    '<g class="btxf-wl"><path d="M48 52C36 26 10 18 5 32C5 47 28 56 48 54Z" fill="url(#btxbWg)" stroke="#8fb4d6" stroke-opacity=".6" stroke-width=".8"/></g>' +
    '<g class="btxf-wr"><path d="M72 52C84 26 110 18 115 32C115 47 92 56 72 54Z" fill="url(#btxbWg)" stroke="#8fb4d6" stroke-opacity=".6" stroke-width=".8"/></g>' +
    '<g fill="none" stroke="#1b130a" stroke-width="1.8" stroke-linecap="round"><path d="M50 56L42 66L45 72"/><path d="M70 56L78 66L75 72"/></g>' +
    '<ellipse cx="60" cy="64" rx="15" ry="21" fill="url(#btxbAb)"/>' +
    '<g clip-path="url(#btxbClipF)" fill="#14100a"><rect x="40" y="57" width="40" height="5"/><rect x="40" y="67" width="40" height="5"/><rect x="40" y="77" width="40" height="7"/></g>' +
    '<path d="M56 84L60 90L64 84Z" fill="#2a1a0a"/>' +
    '<circle cx="60" cy="50" r="15" fill="url(#btxbTh)"/>' +
    '<circle cx="60" cy="50" r="14.6" fill="none" stroke="#ffd34e" stroke-width="3" stroke-dasharray="1.2 1.7" opacity=".85"/>' +
    '<g class="btxf-head">' +
      '<g fill="none" stroke="#1a1208" stroke-width="1.6" stroke-linecap="round"><path d="M55 27Q49 14 41 11"/><path d="M65 27Q71 14 79 11"/></g>' +
      '<circle cx="40.5" cy="10.6" r="1.8" fill="#1a1208"/><circle cx="79.5" cy="10.6" r="1.8" fill="#1a1208"/>' +
      '<path d="M45 38C45 25 52 22 60 22C68 22 75 25 75 38C75 50 68 55 60 55C52 55 45 50 45 38Z" fill="#17110b"/>' +
      '<path d="M52 48Q60 58 68 48Q60 54 52 48Z" fill="#f2b014"/>' +
      '<g class="btxf-eyes"><ellipse cx="51.5" cy="36" rx="6" ry="8.5" transform="rotate(-8 51.5 36)" fill="url(#btxbEye)"/><ellipse cx="68.5" cy="36" rx="6" ry="8.5" transform="rotate(8 68.5 36)" fill="url(#btxbEye)"/>' +
      '<circle cx="49.5" cy="32.5" r="1.9" fill="#fff" opacity=".85"/><circle cx="66.5" cy="32.5" r="1.9" fill="#fff" opacity=".85"/></g>' +
      '<path d="M56 48Q60 51.5 64 48" fill="none" stroke="#f7d46a" stroke-width="1.4" stroke-linecap="round"/>' +
    '</g></svg>';

  window.btxBeeFace = function () { return FRONT; };

  var SPLAT = '<svg viewBox="0 0 68 68" aria-hidden="true" focusable="false"><path d="M34 6c6 0 7 8 12 9s12-3 14 4-6 9-4 14 10 6 6 12-10 2-13 7-1 12-9 12-7-9-13-10-14 4-16-3 6-9 5-14S2 30 8 24s11 0 14-6 6-12 12-12z" fill="#b3121a"/>' +
    '<circle cx="26" cy="26" r="5" fill="#d9232c" opacity=".8"/><circle cx="43" cy="40" r="3.4" fill="#7d0a11" opacity=".7"/></svg>';

  var MSG_HELLO = 'Hi! I am <b>Bee</b>, your assistant from <b>BTX</b>';
  var MSG_KILLED = 'You killed me, right? No worries, my soul will always assist you';

  var els = {};
  var flight = null;      // whatever is animating right now: { raf, timer, node }
  var bubbleTimer = 0;

  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
  function smooth(s) { s = clamp(s, 0, 1); return s * s * (3 - 2 * s); }
  function lerp(a, b, k) { return a + (b - a) * k; }

  /* Point k (0..1, eased by the caller) on the arc from `from` to a target: it rises first, then settles down onto it. */
  function swoop(out, from, tx, ty, vh, k) {
    var j = 1 - k, dx = tx - from.x;
    var c1x = from.x + dx * 0.25, c1y = from.y - vh * 0.3;
    var c2x = tx - dx * 0.18, c2y = ty - vh * 0.28;
    out.x = j * j * j * from.x + 3 * j * j * k * c1x + 3 * j * k * k * c2x + k * k * k * tx;
    out.y = j * j * j * from.y + 3 * j * j * k * c1y + 3 * j * k * k * c2y + k * k * k * ty;
  }

  function place(node, x, y, w, h, scale) {
    node.style.transform = 'translate3d(' + (x - w / 2).toFixed(1) + 'px,' + (y - h / 2).toFixed(1) + 'px,0) scale(' + scale.toFixed(3) + ')';
  }

  function splash(x, y) {
    var el = document.createElement('div');
    el.className = 'btx-bee-splash';
    el.style.left = x + 'px';
    el.style.top = y + 'px';
    var html = SPLAT;
    for (var i = 0; i < DROPS; i++) {
      var ang = Math.random() * Math.PI * 2, dist = 30 + Math.random() * 70, size = 3 + Math.random() * 7;
      html += '<i style="--s:' + size.toFixed(1) + 'px;--dx:' + (Math.cos(ang) * dist).toFixed(0) + 'px;--dy:' + (Math.sin(ang) * dist + 28).toFixed(0) + 'px"></i>';
    }
    el.innerHTML = html;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 1700);
    try { if (navigator.vibrate) navigator.vibrate(40); } catch (e) { /* not supported */ }
  }

  function build(launcher) {
    var defs = document.createElement('div');
    defs.innerHTML = DEFS;
    document.body.appendChild(defs.firstChild);

    els.perch = document.createElement('span');
    els.perch.className = 'btx-bee-perch';
    els.perch.setAttribute('aria-hidden', 'true');
    els.perch.innerHTML = FRONT;
    launcher.appendChild(els.perch);

    // on phones the chat is a bottom sheet; the bee (or its soul) sits on its top edge, fluttering
    els.sheet = document.createElement('span');
    els.sheet.className = 'btx-bee-sheet';
    els.sheet.setAttribute('aria-hidden', 'true');
    els.sheet.innerHTML = FRONT;
    launcher.parentNode.appendChild(els.sheet);

    els.bubble = document.createElement('div');
    els.bubble.className = 'btx-bee-bubble';
    els.bubble.setAttribute('role', 'status');
    els.bubble.innerHTML = '<button type="button" class="btx-bee-x" aria-label="Dismiss">&times;</button><span class="btx-bee-msg"></span>';
    els.msg = els.bubble.querySelector('.btx-bee-msg');
    document.body.appendChild(els.bubble);
    els.bubble.addEventListener('click', function (e) {
      hideBubble();
      if (!e.target.classList.contains('btx-bee-x') && window.btxOpenChat) window.btxOpenChat();
    });
  }

  function showBubble(html, seconds) {
    els.msg.innerHTML = html;
    els.bubble.classList.add('is-on');
    clearTimeout(bubbleTimer);
    bubbleTimer = setTimeout(hideBubble, seconds * 1000);
  }
  function hideBubble() {
    clearTimeout(bubbleTimer);
    els.bubble.classList.remove('is-on');
  }

  /* Ends whatever is animating and shows the perched bee (or its soul); the launcher icon turns black and yellow. */
  function seat(launcher, greet, soul) {
    if (flight) {
      cancelAnimationFrame(flight.raf);
      clearTimeout(flight.timer);
      if (flight.node) flight.node.remove();
      flight = null;
    }
    document.documentElement.classList.remove('btx-bee-flying');
    els.perch.classList.toggle('is-soul', !!soul);
    els.perch.classList.add('is-visible');
    els.sheet.classList.toggle('is-soul', !!soul);
    els.sheet.classList.add('is-visible');
    launcher.classList.add('is-bee-seated');
    window.__btxBeeSeated = true;
    document.dispatchEvent(new CustomEvent('btx:bee-seated'));
    if (!greet) return;
    els.perch.classList.add('is-landed');
    setTimeout(function () { els.perch.classList.remove('is-landed'); }, 1800);
    showBubble(soul ? MSG_KILLED : MSG_HELLO, soul ? 12 : 9);
  }

  /* The squashed bee drops from where it was hit, spins onto its back, bounces and stays at the bottom edge. */
  function fall(node, side, shown, launcher, w, h) {
    var x = shown.x, y = shown.y, y0 = shown.y, vy = 0, last = 0, bounces = 0, spun = 0;
    var scale = shown.scale, a0 = shown.pitch, face = shown.facing < 0 ? -1 : 1;

    function settle(ground) {
      side.style.transform = 'rotate(' + CORPSE_TILT + 'deg) scaleX(' + face + ')';
      node.style.left = (x / window.innerWidth * 100).toFixed(2) + '%';   // pinned to the bottom edge, survives resizes
      node.style.top = 'auto';
      node.style.bottom = '4px';
      node.style.transformOrigin = '50% 100%';
      node.style.transform = 'translateX(-50%) scale(' + scale.toFixed(3) + ')';
      node.classList.add('is-corpse');
      els.corpse = node;
      flight.node = null;
      flight.timer = setTimeout(function () { releaseSoul(launcher, x, ground); }, SOUL_DELAY);
    }

    function tick(ts) {
      var dt = last ? Math.min((ts - last) / 1000, 0.05) : 1 / 60;
      last = ts;
      var ground = window.innerHeight - 4 - h * scale / 2;
      vy += GRAVITY * dt;
      y += vy * dt;
      if (y >= ground) {
        y = ground;
        if (bounces < 2 && vy > 200) { vy = -vy * 0.32; bounces++; } else { settle(ground); return; }
      }
      spun = Math.max(spun, clamp((y - y0) / Math.max(ground - y0, 1), 0, 1));
      side.style.transform = 'rotate(' + (a0 + (CORPSE_TILT - a0 + 360) * spun).toFixed(1) + 'deg) scaleX(' + face + ')';
      place(node, x, y, w, h, scale);
      flight.raf = requestAnimationFrame(tick);
    }
    flight.raf = requestAnimationFrame(tick);
  }

  /* The bee's soul rises from the body and floats over to the chat icon. */
  function releaseSoul(launcher, sx, sy) {
    markIntroSeen('killed');
    var node = document.createElement('div');
    node.className = 'btx-bee-fly is-look is-soul';
    node.setAttribute('aria-hidden', 'true');
    node.innerHTML = FRONT;
    document.body.appendChild(node);
    var w = node.offsetWidth, h = node.offsetHeight;
    var perchScale = els.perch.offsetWidth / w;
    flight.node = node;

    var from = { x: sx, y: sy }, pt = { x: 0, y: 0 }, t = 0, last = 0;
    function tick(ts) {
      var dt = last ? Math.min((ts - last) / 1000, 0.05) : 1 / 60;
      last = ts; t += dt;
      var vw = window.innerWidth, vh = window.innerHeight;
      var p = clamp(t / SOUL_SECONDS, 0, 1), k = smooth(p);
      var r = els.perch.getBoundingClientRect();
      swoop(pt, from, r.left + r.width / 2, r.top + r.height / 2, vh, k);
      var scale = lerp(1, perchScale, k);
      place(node, clamp(pt.x, EDGE + w * scale / 2, vw - EDGE - w * scale / 2), clamp(pt.y, EDGE + h * scale / 2, vh - EDGE - h * scale / 2), w, h, scale);
      node.style.opacity = (Math.min(1, p * 4) * 0.85).toFixed(2);
      if (p >= 1) { seat(launcher, true, true); return; }
      flight.raf = requestAnimationFrame(tick);
    }
    flight.raf = requestAnimationFrame(tick);
  }

  function startFlight(launcher) {
    var node = document.createElement('div');
    node.className = 'btx-bee-fly';
    node.setAttribute('aria-hidden', 'true');
    node.innerHTML = SIDE + FRONT;
    document.body.appendChild(node);
    document.documentElement.classList.add('btx-bee-flying');

    var side = node.querySelector('.btxb-side');
    var w = node.offsetWidth, h = node.offsetHeight;
    var perchScale = els.perch.offsetWidth / w;
    flight = { node: node, raf: 0, timer: 0 };

    // motion state
    var t = 0, last = 0;
    var u = 0;                    // position along the sweep: 0 = right side, 1 = far left
    var nextHover = 0, hoverLeft = 0, resume = 1;
    var looking = false, look = 0;
    var x = 0, y = 0, vx = 0, vy = 0, facing = -1, flip = -1, pitchS = 0;   // flip/pitchS are the eased, drawn values
    var landT = 0, from = null;   // landing swoop: elapsed time and where it started
    var pt = { x: 0, y: 0 };
    var shown = { x: 0, y: 0, scale: 1, facing: 1, pitch: 0 };   // where the bee is drawn right now (for the fall)

    // The sweep: right -> left in one smooth wave. Always inside the screen: the sprite's half size and
    // EDGE are reserved on every side.
    function pathAt(pos, vw, vh) {
      var xMin = EDGE + w / 2, xMax = vw - EDGE - w / 2;
      var yMin = EDGE + h / 2, yMax = vh - EDGE - h / 2;
      var amp = Math.min(vh * 0.24, 240);
      var px = lerp(xMax, xMin, pos);
      var py = vh * 0.45 + amp * Math.sin(pos * Math.PI * 2);
      return { x: clamp(px, xMin, xMax), y: clamp(py, yMin, yMax) };
    }

    // Advances along the sweep, easing to a stop at each hover point; returns true while hovering.
    function advance(dt) {
      var speed = 1 / PASS_SECONDS;
      var hovering = false;
      if (hoverLeft > 0) {
        hoverLeft -= dt; hovering = true; speed = 0;
        if (hoverLeft <= 0) { nextHover++; resume = 0; }
      } else {
        var gap = HOVER_AT[nextHover] - u;
        speed *= clamp(gap / 0.12, 0.1, 1);
        if (gap <= 0.004) { u = HOVER_AT[nextHover]; hoverLeft = HOVER_SECONDS; }
      }
      resume = Math.min(1, resume + dt / 0.5);
      u = Math.min(1, u + speed * smooth(resume) * dt);
      return hovering;
    }

    var start = pathAt(0, window.innerWidth, window.innerHeight);
    x = start.x; y = start.y;

    function frame(ts) {
      var dt = last ? Math.min((ts - last) / 1000, 0.05) : 1 / 60;
      last = ts; t += dt;
      var vw = window.innerWidth, vh = window.innerHeight;
      var nx, ny, scale = 1, landing = 0, hovering = false;
      var follow = 1 - Math.exp(-dt * FOLLOW);   // frame-rate independent easing factor

      if (nextHover < HOVER_AT.length) {
        // the bee glides after its path point instead of sitting on it, which rounds off every start, stop and turn
        hovering = advance(dt);
        var p = pathAt(u, vw, vh);
        nx = x + (p.x - x) * follow;
        ny = y + (p.y - y) * follow;
      } else {
        // swoop from the far-left hover point to the icon
        var r = els.perch.getBoundingClientRect();
        if (!from) from = { x: x, y: y };
        landT += dt;
        landing = clamp(landT / LAND_SECONDS, 0, 1);
        var k = smooth(landing);
        swoop(pt, from, r.left + r.width / 2, r.top + r.height / 2, vh, k);
        nx = pt.x; ny = pt.y;
        scale = lerp(1, perchScale, k);
      }

      // keep the (scaled) sprite inside the screen on every frame
      nx = clamp(nx, EDGE + w * scale / 2, vw - EDGE - w * scale / 2);
      ny = clamp(ny, EDGE + h * scale / 2, vh - EDGE - h * scale / 2);

      if (dt > 0) {   // smoothed velocity of the path itself (without the hover jitter)
        vx = lerp(vx, (nx - x) / dt, 0.15);
        vy = lerp(vy, (ny - y) / dt, 0.15);
      }
      x = nx; y = ny;

      // face the visitor while hovering and while settling on the launcher
      var wantLook = hovering || landing > 0.45;
      look = lerp(look, wantLook ? 1 : 0, 1 - Math.exp(-dt * 6));
      if (wantLook !== looking) { looking = wantLook; node.classList.toggle('is-look', looking); }

      // side view: turn round smoothly towards the direction of travel and pitch gently with vertical speed
      if (Math.abs(vx) > 25) facing = vx > 0 ? 1 : -1;
      flip = lerp(flip, facing, 1 - Math.exp(-dt * 9));
      var pitchGoal = Math.atan2(vy, Math.max(Math.abs(vx), 90)) * 57.3 * 0.5 * facing * (1 - look);
      pitchS = lerp(pitchS, pitchGoal, 1 - Math.exp(-dt * 5));
      side.style.transform = 'rotate(' + pitchS.toFixed(1) + 'deg) scaleX(' + flip.toFixed(3) + ')';

      var calm = 1 - landing;   // slow, layered bobbing (no fast jitter)
      shown.x = x + (Math.sin(t * 2.3) * 3 + Math.sin(t * 4.1) * 1.2) * calm;
      shown.y = y + (Math.sin(t * 3.2) * (4 + 4 * look) + Math.sin(t * 6.1) * 1.5) * calm;
      shown.scale = scale; shown.facing = flip; shown.pitch = pitchS;
      place(node, shown.x, shown.y, w, h, scale);
      if (t < FADE_IN_SECONDS + dt) node.style.opacity = Math.min(1, t / FADE_IN_SECONDS).toFixed(2);

      if (landing >= 1) { markIntroSeen(); seat(launcher, true, false); return; }
      flight.raf = requestAnimationFrame(frame);
    }
    flight.raf = requestAnimationFrame(frame);

    // Easter egg: press the bee -> splash and freeze; release -> it drops to the bottom of the screen.
    node.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      cancelAnimationFrame(flight.raf);
      node.classList.remove('is-look');
      node.classList.add('is-dead');
      hideBubble();
      splash(e.clientX, e.clientY);
      var dying = flight;
      function release() {
        window.removeEventListener('pointerup', release);
        window.removeEventListener('pointercancel', release);
        if (flight === dying) fall(node, side, shown, launcher, w, h);
      }
      window.addEventListener('pointerup', release);
      window.addEventListener('pointercancel', release);
    }, { once: true });
  }

  function init() {
    var launcher = document.querySelector('.btx-chat-launcher');
    if (!launcher) return;   // chatbot did not load: nothing to perch on
    document.documentElement.classList.add('btx-bee-on');   // our bubble replaces the chat's own hello
    build(launcher);

    launcher.addEventListener('click', function () {
      hideBubble();
      if (flight) { markIntroSeen(introSeen() === 'killed' || els.corpse ? 'killed' : '1'); seat(launcher, false, !!els.corpse); }
    });

    var seen = introSeen();
    var chatOpen = launcher.getAttribute('aria-expanded') === 'true';
    if (seen || chatOpen) { seat(launcher, false, seen === 'killed'); return; }

    var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduceMotion) { markIntroSeen(); seat(launcher, false, false); setTimeout(function () { showBubble(MSG_HELLO, 9); }, 1200); return; }

    setTimeout(function () { if (!flight && !introSeen()) startFlight(launcher); }, 900);
  }

  // Start as soon as the DOM is ready (chatbot.js, which creates the launcher, is deferred ahead of this file).
  // Waiting for window 'load' would hold the bee back until every image on the page has downloaded.
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
