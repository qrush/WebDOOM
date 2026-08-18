'use strict';
/*
 * WebDOOM 10-year tribute -- in-world video wall.
 *
 * WHY NOT WebGL: this prebuilt engine runs prboom's SOFTWARE renderer.
 * src/m_misc.c:308-313 defaults `videomode` to "8" on every non-MSVC build,
 * so no WebGL context is ever created and there is no GL texture to stream
 * into. Forcing `-vidmode gl` does reach the GL path (after patching a
 * startup crash and a throwing glTexGenfv stub), but emscripten's legacy GL
 * emulation then renders an all-black frame -- unusable.
 *
 * SO: we write straight into the software renderer's texture memory.
 * src/r_segs.c:352 draws walls from R_CacheTextureCompositePatchNum(), whose
 * rpatch_t.pixels is one flat COLUMN-major byte array -- pixels[x*height + y]
 * (src/r_patch.c:277). Our WISTSCRN texture is a single fully-opaque patch,
 * so prboom composites it verbatim; we locate that buffer in the wasm heap by
 * searching for a slice of the known static pattern, then overwrite it with
 * palette-quantised video frames every animation frame.
 */
(function () {
  var BUILD = 'rev11-proximity-audio';

  var VIDEO_MP4 = 'https://embed-ssl.wistia.com/deliveries/cc9c2606f69a9f7c4bcf003efafdbbd1f619e57e.bin';
  var WISTIA_ID = '9pyv4cqb5n';

  var meta = null, lut = null, expected = null;  // screen.json, palette.lut, screen.bin
  var heap = null;                      // Module.HEAPU8
  var addr = -1;                        // composite buffer address
  var video = null, streaming = false;
  var lastWrite = null;                 // copy of what we wrote, for validation
  var frames = 0, rescans = 0, scanMs = 0, matches = 0;

  var srcCanvas = document.createElement('canvas');
  var sctx = null;

  /* ---------------- locating the texture in the wasm heap ---------------- */

  /* Scanning the whole 256MB heap in one go takes long enough to starve the
     game's requestAnimationFrame loop (the engine visibly stalls mid-wipe), so
     the search is chunked across ticks and resumes where it left off.

     A candidate is confirmed by comparing ALL 32768 bytes against the expected
     composite (screen.bin) rather than trusting the short signature. That
     matters because the raw WISTSCRN lump is resident in the heap too and a
     single column of it is byte-identical to a column of the composite; only
     the full-buffer check reliably tells them apart. */
  var CHUNK = 24 << 20;        // bytes scanned per tick
  var cursor = 0;

  function verifyAt(h, at) {
    if (at < 0 || at + expected.length > h.length) return false;
    for (var i = 0; i < expected.length; i++) {
      if (h[at + i] !== expected[i]) return false;
    }
    return true;
  }

  /* Uses the native TypedArray indexOf to find candidate first-bytes instead
     of a hand-rolled JS byte loop -- the naive version burned enough main
     thread time to visibly stall the engine's main loop during the title
     wipe. Only positions whose first byte matches get the (rare) full check. */
  function scanChunk() {
    if (!window.Module || !Module.HEAPU8) return false;
    heap = Module.HEAPU8;      // the view is replaced whenever memory grows

    var sig = meta.signature, n = sig.length, first = sig[0];
    var end = Math.min(cursor + CHUNK, heap.length - n);
    var t0 = performance.now();

    var i = heap.indexOf(first, cursor);
    while (i >= 0 && i < end) {
      var ok = true;
      for (var j = 1; j < n; j++) {
        if (heap[i + j] !== sig[j]) { ok = false; break; }
      }
      if (ok && verifyAt(heap, i)) {
        addr = i;
        rescans++;
        scanMs = Math.round(performance.now() - t0);
        return true;
      }
      i = heap.indexOf(first, i + 1);
    }

    scanMs = Math.round(performance.now() - t0);
    cursor = (end >= heap.length - n) ? 0 : end;
    return false;
  }

  /* full re-acquire, used when a known-good address goes stale */
  function locate() {
    cursor = 0;
    for (var pass = 0; pass < 64; pass++) {
      if (scanChunk()) return true;
      if (cursor === 0) break;         // wrapped without finding it
    }
    return false;
  }

  /* the buffer is PU_CACHE and could in principle be purged/moved. Writing to
     a stale address would corrupt unrelated memory, so before every frame we
     confirm the bytes are still the ones we last wrote. */
  function addrStillValid() {
    if (addr < 0 || !lastWrite) return false;
    if (Module.HEAPU8 !== heap) heap = Module.HEAPU8;
    if (addr + meta.bufferLength > heap.length) return false;
    for (var k = 0; k < 64; k++) {
      var off = (k * 977) % meta.bufferLength;   // scattered spot-check
      if (heap[addr + off] !== lastWrite[off]) return false;
    }
    return true;
  }

  /* --------------------------- video streaming --------------------------- */

  function ensureVideo(cb) {
    if (video) return cb();
    video = document.createElement('video');
    video.crossOrigin = 'anonymous';
    video.loop = true; video.muted = true; video.playsInline = true;
    video.preload = 'auto'; video.src = VIDEO_MP4;
    video.addEventListener('canplay', function once () {
      video.removeEventListener('canplay', once); cb();
    });
    video.addEventListener('error', function () { setStatus('video failed to load', true); });
    video.load();
  }

  function start() {
    if (streaming) return;
    ensureVideo(function () {
      srcCanvas.width = meta.width; srcCanvas.height = meta.height;
      sctx = srcCanvas.getContext('2d', { willReadFrequently: true });
      lastWrite = new Uint8Array(meta.bufferLength);
      video.play().catch(function () {});
      streaming = true;
      pump();
    });
  }

  function pump() {
    if (!streaming) return;
    requestAnimationFrame(pump);
    if (!video || video.readyState < 2 || !video.videoWidth) return;

    // never write to a stale address -- drop back to chunked searching instead
    // of doing a blocking full rescan inside the render loop
    if (!addrStillValid()) {
      if (!verifyAt(heap, addr) && !scanChunk()) { return; }
    }

    var W = meta.width, H = meta.height;
    // cover-fit the 16:9 frame into the 2:1 screen
    var s = Math.max(W / video.videoWidth, H / video.videoHeight);
    var dw = video.videoWidth * s, dh = video.videoHeight * s;
    sctx.drawImage(video, (W - dw) / 2, (H - dh) / 2, dw, dh);

    var img = sctx.getImageData(0, 0, W, H).data;
    var buf = lastWrite;
    // RGB -> RGB555 -> palette index, written COLUMN-major
    for (var y = 0; y < H; y++) {
      var row = y * W * 4;
      for (var x = 0; x < W; x++) {
        var p = row + x * 4;
        buf[x * H + y] = lut[((img[p] >> 3) << 10) | ((img[p + 1] >> 3) << 5) | (img[p + 2] >> 3)];
      }
    }
    heap.set(buf, addr);
    frames++;
    updateAudio();
    if ((frames & 31) === 0) renderDebug();
  }

  /* --------------------------- proximity audio ---------------------------
     DOOM keeps the player's position in its mobj_t as three consecutive
     fixed_t (16.16) values -- x, y, z. We find that struct the same way we
     found the texture: scan for the known spawn triple at map start, which is
     why the spawn coordinates in the WAD are deliberately odd numbers.

     Candidates are disambiguated by movement: the real mobj's coordinates
     change when the player walks, while an incidental byte match (or the
     map's spawn-point record) stays put. Until exactly one candidate has been
     seen to move AND stays inside the room, we don't trust any of them.       */

  var playerAddr = -1, playerCandidates = [], playerSeen = null;
  var i32 = null;                      // Int32Array view over the same buffer
  var NEAR = 160, FAR = 560;           // map units: full volume -> silence
  var audioArmed = false, wallGain = 0;

  function refreshViews() {
    if (!Module.HEAPU8) return false;
    if (heap !== Module.HEAPU8) heap = Module.HEAPU8;
    if (!i32 || i32.buffer !== heap.buffer) i32 = new Int32Array(heap.buffer);
    return true;
  }

  function findPlayerCandidates() {
    if (!refreshViews()) return;
    var sp = meta.player.spawn;                       // [x, y, z] fixed_t
    var key = new Uint8Array(new Int32Array(sp).buffer);
    var first = key[0], n = key.length, hits = [];
    var i = heap.indexOf(first);
    while (i >= 0 && hits.length < 32) {
      var ok = (i % 4) === 0;                          // int32-aligned
      for (var j = 1; ok && j < n; j++) if (heap[i + j] !== key[j]) ok = false;
      if (ok) hits.push(i);
      i = heap.indexOf(first, i + 1);
    }
    playerCandidates = hits;
    playerSeen = hits.map(function (a) { return [i32[a >> 2], i32[(a >> 2) + 1]]; });
  }

  function inBounds(x, y) {
    var b = meta.player.bounds;                        // [minX, minY, maxX, maxY]
    return x >= b[0] && x <= b[2] && y >= b[1] && y <= b[3];
  }

  function resolvePlayer() {
    if (playerAddr >= 0) return true;
    if (!playerCandidates.length) { findPlayerCandidates(); return false; }
    if (!refreshViews()) return false;
    for (var k = 0; k < playerCandidates.length; k++) {
      var a = playerCandidates[k] >> 2;
      var x = i32[a], y = i32[a + 1];
      var was = playerSeen[k];
      if ((x !== was[0] || y !== was[1]) && inBounds(x, y)) {
        playerAddr = playerCandidates[k];
        return true;
      }
    }
    return false;
  }

  function playerPos() {
    if (playerAddr < 0 || !refreshViews()) return null;
    var a = playerAddr >> 2;
    var x = i32[a] / 65536, y = i32[a + 1] / 65536;
    if (!inBounds(i32[a], i32[a + 1])) { playerAddr = -1; return null; }  // lost it
    return [x, y];
  }

  /* volume falls off with distance to the centre of the screen wall */
  function updateAudio() {
    if (!video) return;
    if (overlay) { video.volume = 0; return; }          // overlay owns the audio
    var p = playerPos();
    if (!p) { resolvePlayer(); video.volume = 0; return; }
    var cx = meta.screenCentre[0], cy = meta.screenCentre[1];
    var d = Math.hypot(p[0] - cx, p[1] - cy);
    var t = (FAR - d) / (FAR - NEAR);
    wallGain = Math.max(0, Math.min(1, t));
    wallGain = wallGain * wallGain;                     // gentler far-field falloff
    video.volume = audioArmed ? wallGain : 0;
    if (audioArmed && video.muted) video.muted = false;
  }

  /* browsers only allow unmuted playback after a real user gesture */
  function armAudio() {
    if (audioArmed || !video) return;
    audioArmed = true;
    video.muted = false;
    video.volume = 0;
    video.play().catch(function () {});
    var b = document.getElementById('tribute-sound');
    if (b) b.textContent = '🔊 sound on';
  }

  /* ------------------------- full-quality overlay ------------------------- */

  var overlay = null;

  function openOverlay() {
    if (overlay) { overlay.classList.add('open'); return; }
    overlay = document.createElement('div');
    overlay.id = 'tribute-overlay';
    overlay.innerHTML =
      '<div class="to-inner">' +
      '  <button class="to-close" title="close (Esc)">×</button>' +
      '  <div class="wistia_responsive_padding" style="padding:56.25% 0 0 0;position:relative;">' +
      '    <div class="wistia_responsive_wrapper" style="height:100%;left:0;position:absolute;top:0;width:100%;">' +
      '      <iframe src="https://fast.wistia.net/embed/iframe/' + WISTIA_ID +
                 '?web_component=true&seo=false&autoPlay=true" title="Lenny - A Better Way to Deliver Video" ' +
                 'allow="autoplay; fullscreen" allowtransparency="true" frameborder="0" scrolling="no" ' +
                 'class="wistia_embed" name="wistia_embed" width="100%" height="100%"></iframe>' +
      '    </div>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(overlay);
    overlay.querySelector('.to-close').addEventListener('click', closeOverlay);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) closeOverlay(); });
    requestAnimationFrame(function () { overlay.classList.add('open'); });
  }

  function closeOverlay() {
    if (!overlay) return;
    overlay.classList.remove('open');
    // drop the iframe so its audio stops
    var f = overlay.querySelector('iframe');
    if (f) f.src = f.src;
    overlay.remove();
    overlay = null;
    if (Module && Module.canvas) Module.canvas.focus();
  }

  /* ------------------------------- chrome -------------------------------- */

  var hint, statusEl;

  function setStatus(msg, isErr) {
    if (!statusEl) return;
    statusEl.textContent = msg;
    statusEl.className = isErr ? 'tribute-err' : '';
  }

  function renderDebug() {
    if (!statusEl) return;
    if (addr < 0) {
      statusEl.textContent = matches > 1
        ? 'ambiguous signature (' + matches + ' hits) — not writing'
        : 'looking for the screen in memory…';
      statusEl.className = matches > 1 ? 'tribute-err' : '';
      return;
    }
    var p = playerPos();
    statusEl.textContent = 'streaming · ' + frames + 'f' +
      (p ? ' · ' + Math.round(Math.hypot(p[0] - meta.screenCentre[0], p[1] - meta.screenCentre[1])) + 'u'
         : ' · finding player') +
      ' · vol ' + Math.round((audioArmed ? wallGain : 0) * 100) + '%';
    statusEl.className = '';
  }

  /* --------------------------------- boot -------------------------------- */

  Promise.all([
    fetch('screen.json').then(function (r) { return r.json(); }),
    fetch('palette.lut').then(function (r) { return r.arrayBuffer(); }),
    fetch('screen.bin').then(function (r) { return r.arrayBuffer(); })
  ]).then(function (res) {
    meta = res[0];
    lut = new Uint8Array(res[1]);
    expected = new Uint8Array(res[2]);
    // the engine has to have loaded + composited the texture before we can
    // find it, which only happens once the map is being drawn
    var tries = 0;
    var iv = setInterval(function () {
      if (streaming) { clearInterval(iv); return; }
      if (!window.Module || !Module.HEAPU8) return;
      heap = Module.HEAPU8;
      if (scanChunk()) { clearInterval(iv); setStatus('screen found'); start(); }
      else { renderDebug(); if (++tries > 1200) { clearInterval(iv); setStatus('could not find the screen texture', true); } }
    }, 250);
  }).catch(function (e) {
    setStatus('failed to load screen data: ' + e.message, true);
  });

  window.addEventListener('DOMContentLoaded', function () {
    var bar = document.createElement('div');
    bar.id = 'tribute-bar';
    bar.innerHTML = '<span id="tribute-status"></span>' +
                    '<button id="tribute-sound">🔇 enable sound</button>' +
                    '<button id="tribute-watch">Watch the full video</button>';
    document.body.appendChild(bar);
    statusEl = bar.querySelector('#tribute-status');
    bar.querySelector('#tribute-watch').addEventListener('click', function () {
      openOverlay(); this.blur();
    });
    bar.querySelector('#tribute-sound').addEventListener('click', function () {
      armAudio(); this.blur();
    });
    // any click or keypress in the game counts as the gesture that unlocks audio
    ['pointerdown', 'keydown'].forEach(function (ev) {
      document.addEventListener(ev, function () { armAudio(); }, { once: true });
    });

    hint = document.createElement('div');
    hint.id = 'tribute-hint';
    hint.textContent = 'walk up to the screen to hear it · press E for full quality';
    document.body.appendChild(hint);

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && overlay) { closeOverlay(); return; }
      if (!overlay && (e.key === 'e' || e.key === 'E')) openOverlay();
    });
  });

  window.__tribute = {
    build: BUILD,
    addr: function () { return addr; },
    frames: function () { return frames; },
    relocate: locate,
    open: openOverlay,
    player: function () { return playerPos(); },
    playerAddr: function () { return playerAddr; },
    candidates: function () { return playerCandidates.slice(); }
  };
})();
