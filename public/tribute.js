'use strict';
/*
 * WebDOOM 10-year tribute -- an octagonal room with a Wistia video on each of
 * its eight walls, plus proximity audio and a full-quality overlay player.
 *
 * WHY THIS WRITES INTO WASM MEMORY:
 * The prebuilt engine runs prboom's SOFTWARE renderer. src/m_misc.c:308-313
 * defaults `videomode` to "8" on every non-MSVC build, so no WebGL context is
 * ever created and there is no GL texture to stream into. (Forcing
 * `-vidmode gl` does reach the GL renderer after patching a startup crash and
 * a throwing glTexGenfv stub -- see make-gl-build.py -- but emscripten's
 * legacy GL emulation then renders an all-black frame.)
 *
 * So: walls are drawn from R_CacheTextureCompositePatchNum (src/r_segs.c:352),
 * whose rpatch_t.pixels is one flat COLUMN-major byte array --
 * pixels[x*height + y] (src/r_patch.c:277). Each WISTSCR* texture is a single
 * fully-opaque patch of deterministic static, so prboom composites it verbatim
 * and its exact bytes are known ahead of time. We find each composite in the
 * heap and overwrite it with palette-quantised video every frame.
 */
(function () {
  // sidecars are regenerated whenever the map changes; a cached copy
  // silently breaks texture lookup, so always revalidate them
  var NOCACHE = { cache: 'no-cache' };
  var BUILD = 'rev31-no-hands';

  var MEDIA_JSON = 'https://fast.wistia.net/embed/medias/';

  var meta = null, lut = null, expected = null, videoIds = null;
  var heap = null, i32 = null;
  var screens = [];                 // {def, addr, video, buf, canvas, ctx, dist}
  var started = false;
  var frames = 0;

  /* ---------------------------- heap plumbing ---------------------------- */

  function refreshViews() {
    if (!window.Module || !Module.HEAPU8) return false;
    if (heap !== Module.HEAPU8) { heap = Module.HEAPU8; i32 = null; }
    if (!i32 || i32.buffer !== heap.buffer) i32 = new Int32Array(heap.buffer);
    return true;
  }

  /* Confirm a candidate byte-for-byte against the expected composite rather
     than trusting the signature. The raw WISTSCR* lumps are resident in the
     heap too, and one texture column of a lump is byte-identical to a column
     of its composite -- only the full check reliably tells them apart. */
  function verifyAt(at, off, len) {
    if (at < 0 || at + len > heap.length) return false;
    for (var i = 0; i < len; i++) {
      if (heap[at + i] !== expected[off + i]) return false;
    }
    return true;
  }

  /* Chunked, because scanning 256MB in one go starves the engine's main loop
     (it visibly stalls mid-wipe). Uses the native typed-array indexOf for the
     first-byte search; only those positions get the full comparison. */
  /* Search 32-BIT WORDS, not bytes.
     A byte anchor is hopeless here: every candidate value occurs roughly once
     every 256 bytes, so a 256MB sweep runs ~1M inner checks and takes tens of
     seconds per screen. (Anchoring on the first byte is worse still -- DOOM
     positions are fixed_t, so the player key starts 00 00 ..., which matches
     hundreds of millions of times in a mostly-zero heap and locks the page up
     hard enough that devtools times out.)

     A specific 32-bit word occurs by chance about once per 4 billion, so
     Int32Array.indexOf lands on the real thing almost immediately. Zone blocks
     are at least 4-byte aligned so the composites are word-aligned; a byte
     fallback covers the case where they somehow are not. */
  var CHUNK_WORDS = 8 << 20;          // 32MB of heap per pass
  var ACQUIRE_EVERY = 2;
  var scanCost = 0;

  function word0(bytes) {
    return ((bytes[3] << 24) | (bytes[2] << 16) | (bytes[1] << 8) | bytes[0]) | 0;
  }

  function matchesSig(at, sig) {
    for (var j = 0; j < sig.length; j++) {
      if (heap[at + j] !== sig[j]) return false;
    }
    return true;
  }

  function scanFor(sc) {
    if (!refreshViews()) return false;
    var sig = sc.def.signature;
    if (sc.word === undefined) sc.word = word0(sig);
    var end = Math.min(sc.cursor + CHUNK_WORDS, i32.length);
    var i = i32.indexOf(sc.word, sc.cursor);
    while (i >= 0 && i < end) {
      var at = i * 4;
      if (matchesSig(at, sig) && verifyAt(at, sc.def.offset, sc.def.bufferLength)) {
        sc.addr = at;
        return true;
      }
      i = i32.indexOf(sc.word, i + 1);
    }
    if (end >= i32.length) {
      sc.cursor = 0;
      sc.passDone = true;
      // Word scanning assumes the composite is 4-byte aligned, which zone
      // blocks always are in practice. If a whole pass finds nothing, fall
      // back to an unaligned byte search once before giving up on this sweep.
      if (!sc.triedBytes) { sc.triedBytes = true; return scanForBytes(sc); }
      sc.triedBytes = false;
    } else {
      sc.cursor = end;
    }
    return false;
  }

  function scanForBytes(sc) {
    var sig = sc.def.signature, first = sig[0];
    var i = heap.indexOf(first);
    while (i >= 0) {
      if (matchesSig(i, sig) && verifyAt(i, sc.def.offset, sc.def.bufferLength)) {
        sc.addr = i;
        return true;
      }
      i = heap.indexOf(first, i + 1);
    }
    return false;
  }

  /* cheap per-frame guard so we never write to an address that moved */
  function stillValid(sc) {
    if (sc.addr < 0 || !sc.wrote) return false;
    if (!refreshViews()) return false;
    if (sc.addr + sc.def.bufferLength > heap.length) return false;
    for (var k = 0; k < 32; k++) {
      var off = (k * 1103) % sc.def.bufferLength;
      if (heap[sc.addr + off] !== sc.buf[off]) return false;
    }
    return true;
  }

  /* --------------------------- player position --------------------------- */

  var playerAddr = -1, playerCandidates = null, playerSeen = null;
  var playerCursor = 0, playerHits = [];

  /* Chunked like the texture search, and for the same reason: before the map
     exists the spawn triple simply isn't in memory, so an unthrottled search
     would run a full 256MB pass EVERY frame and starve the engine's main loop
     (the title-screen wipe visibly freezes). One bounded chunk per frame. */
  var playerKey = null, playerWord = 0;

  function scanPlayerChunk() {
    if (!refreshViews()) return;
    if (!playerKey) {
      playerKey = new Uint8Array(new Int32Array(meta.player.spawn).buffer);
      playerWord = meta.player.spawn[0] | 0;      // the spawn x, as an int32
    }
    var end = Math.min(playerCursor + CHUNK_WORDS, i32.length);
    var i = i32.indexOf(playerWord, playerCursor);
    while (i >= 0 && i < end && playerHits.length < 64) {
      var at = i * 4;
      if (matchesSig(at, playerKey)) playerHits.push(at);
      i = i32.indexOf(playerWord, i + 1);
    }
    if (end >= i32.length) {                      // finished a full pass
      playerCursor = 0;
      if (playerHits.length) {
        playerCandidates = playerHits;
        playerSeen = playerHits.map(function (a) { return [i32[a >> 2], i32[(a >> 2) + 1]]; });
      }
      playerHits = [];
    } else {
      playerCursor = end;
    }
  }

  function inBounds(x, y) {
    var b = meta.player.bounds;
    return x >= b[0] && x <= b[2] && y >= b[1] && y <= b[3];
  }

  /* The spawn triple matches half a dozen places -- the map's own spawn
     record among them. Identify the real mobj by its ANGLE: angle_t is a BAM
     with the full circle at 2^32, so the spawn facing is an exact value none
     of the decoys carry.

     The previous approach waited for the coordinates to change, which meant
     the player stayed unfound until they happened to walk -- so proximity
     audio was silent and aiming fell back to screen 0 for anyone who shot
     before moving. Movement is still used as a fallback. */
  function resolvePlayer() {
    if (playerAddr >= 0) return true;
    if (!playerCandidates || !playerCandidates.length) return false;
    if (!refreshViews()) return false;

    var wantAngle = meta.player.spawnAngle >>> 0;
    for (var k = 0; k < playerCandidates.length; k++) {
      var a = playerCandidates[k] >> 2;
      if ((i32[a + ANGLE_WORD_OFFSET] >>> 0) === wantAngle && inBounds(i32[a], i32[a + 1])) {
        playerAddr = playerCandidates[k];
        return true;
      }
    }
    // fallback: whichever candidate actually moves is the live mobj
    for (var m = 0; m < playerCandidates.length; m++) {
      var b = playerCandidates[m] >> 2;
      if ((i32[b] !== playerSeen[m][0] || i32[b + 1] !== playerSeen[m][1]) &&
          inBounds(i32[b], i32[b + 1])) {
        playerAddr = playerCandidates[m];
        return true;
      }
    }
    return false;
  }

  /* mobj_t lays out: thinker, x, y, z, snext*, sprev*, angle (p_mobj.h:248).
     Pointers are 4 bytes under wasm32, so angle sits 5 int32s past x.
     angle_t is a full-circle-is-2^32 BAM, and 0 points east (+x). */
  var ANGLE_WORD_OFFSET = 5;

  function playerAngle() {
    if (playerAddr < 0 || !refreshViews()) return null;
    // read unsigned: angle_t uses the whole 32-bit range
    var raw = i32[(playerAddr >> 2) + ANGLE_WORD_OFFSET] >>> 0;
    return raw / 4294967296 * Math.PI * 2;
  }

  function playerPos() {
    if (playerAddr < 0 || !refreshViews()) return null;
    var a = playerAddr >> 2;
    if (!inBounds(i32[a], i32[a + 1])) { playerAddr = -1; playerCandidates = null; return null; }
    return [i32[a] / 65536, i32[a + 1] / 65536];
  }

  /* ------------------------------- video --------------------------------- */

  function resolveMp4(id) {
    return fetch(MEDIA_JSON + id + '.json')
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var assets = j.media.assets.filter(function (a) { return a.container === 'mp4'; });
        assets.sort(function (a, b) { return (a.width || 0) - (b.width || 0); });
        // smallest mp4 is plenty for a 256x144 wall and cheapest to decode
        return { url: assets[0].url, name: j.media.name };
      });
  }

  function makeVideo(url) {
    var v = document.createElement('video');
    v.crossOrigin = 'anonymous';
    v.loop = true; v.muted = true; v.playsInline = true;
    v.preload = 'auto'; v.src = url;
    v.load();
    return v;
  }

  /* ---------------------------- proximity audio --------------------------- */

  var currentRoom = -1;

  /* Which room the player is standing in, by nearest room centre. With 20
     screens, decoding every video at once is hopeless -- only the current
     room's videos are allowed to play; the rest are paused so they cost
     nothing. */
  function updateRoom(p) {
    if (!p || !meta.rooms) return;
    var best = -1, bestD = Infinity;
    for (var i = 0; i < meta.rooms.length; i++) {
      var c = meta.rooms[i].centre;
      var d = Math.hypot(p[0] - c[0], p[1] - c[1]);
      if (d < bestD) { bestD = d; best = i; }
    }
    if (best === currentRoom) return;
    currentRoom = best;
    // walking into a room means its walls are about to be drawn, so clear any
    // backoff on its screens and let them be found straight away
    for (var r = 0; r < screens.length; r++) {
      if (screens[r].def.room === currentRoom && screens[r].addr < 0) {
        screens[r].nextTry = 0; screens[r].backoff = 0;
      }
    }
    for (var k = 0; k < screens.length; k++) {
      var sc = screens[k];
      if (!sc.video) continue;
      if (sc.def.room === currentRoom) {
        if (sc.video.paused) sc.video.play().catch(function () {});
      } else if (!sc.video.paused) {
        sc.video.pause();
        sc.video.volume = 0;
      }
    }
  }

  /* Map units: full volume -> silence. These have to be scaled to the ROOM,
     not to arm's length. A room's centre is 386 units from every screen, so
     the old 150/430 curve (squared, no less) put the middle of a room at 2.5%
     volume -- which is why the videos went silent once you stopped standing
     with your nose against a wall. */
  var NEAR = 120, FAR = 950;
  var audioArmed = false;

  function armAudio() {
    if (audioArmed) return;
    audioArmed = true;
    screens.forEach(function (sc) {
      if (sc.video) { sc.video.muted = false; sc.video.volume = 0; sc.video.play().catch(function () {}); }
    });
    var b = document.getElementById('tribute-sound');
    if (b) { b.textContent = '🔊 sound on'; b.classList.add('on'); }
  }

  /* Exactly ONE screen is audible at a time: the one you are aiming at, or
     the nearest one in this room if you are not pointed at any. Six or seven
     overlapping soundtracks is mush, and the whole point is to hear the screen
     you walked up to. Falloff is linear so the far side of a room is still
     faintly audible rather than abruptly silent. */
  function audioFocus(p) {
    if (!audioArmed || overlay || !p) return null;
    var sc = aimedScreen();
    if (sc && (currentRoom < 0 || sc.def.room === currentRoom)) return sc;
    var best = null;
    for (var i = 0; i < screens.length; i++) {
      var c = screens[i];
      if (currentRoom >= 0 && c.def.room !== currentRoom) continue;
      if (!best || c.dist < best.dist) best = c;
    }
    return best;
  }

  function updateAudio(p) {
    var focus = audioFocus(p);
    for (var i = 0; i < screens.length; i++) {
      var sc = screens[i];
      if (!sc.video) continue;
      if (sc !== focus) { if (sc.video.volume) sc.video.volume = 0; continue; }
      sc.video.volume = Math.max(0, Math.min(1, (FAR - sc.dist) / (FAR - NEAR)));
    }
  }

  /* ------------------------------ main loop ------------------------------ */

  // Quantising all eight screens every frame is far too much work, so the two
  // nearest are refreshed every frame and the rest round-robin behind them.
  // Quantising a screen is 36864 palette lookups; doing four a frame on top of
  // a software renderer at 540p is too much. The one you are looking at needs
  // to be smooth, the rest can lag a little.
  var HOT = 1, WARM = 1, rr = 0;

  function blit(sc) {
    var v = sc.video;
    if (!v || v.readyState < 2 || !v.videoWidth) return;
    // acquisition happens in acquire(); here we only guard against a moved buffer
    if (!stillValid(sc) && !verifyAt(sc.addr, sc.def.offset, sc.def.bufferLength)) {
      sc.addr = -1; sc.wrote = false; return;
    }
    var W = sc.def.width, H = sc.def.height;
    var s = Math.max(W / v.videoWidth, H / v.videoHeight);
    var dw = v.videoWidth * s, dh = v.videoHeight * s;
    sc.ctx.drawImage(v, (W - dw) / 2, (H - dh) / 2, dw, dh);

    var img = sc.ctx.getImageData(0, 0, W, H).data;
    var buf = sc.buf;
    for (var y = 0; y < H; y++) {
      var row = y * W * 4;
      for (var x = 0; x < W; x++) {
        var p = row + x * 4;
        buf[x * H + y] = lut[((img[p] >> 3) << 10) | ((img[p + 1] >> 3) << 5) | (img[p + 2] >> 3)];
      }
    }
    heap.set(buf, sc.addr);
    sc.wrote = true;
  }

  /* At most ONE bounded scan per frame across the whole app, so acquisition
     never competes with the engine for the main thread. */
  /* All eight composites are allocated back-to-back when the level loads, so
     once one is found the rest are almost certainly a short way either side of
     it. Starting the next search just below the last hit turns a ~256MB sweep
     per screen into a near-instant one; the cursor still wraps for a full
     sweep if the guess is wrong. */
  var lastFound = -1, acqIdx = 0;

  /* Acquisition has to be strictly bounded or it eats the frame loop.
     A wall texture's composite does not exist until DOOM first draws that
     wall, so screens the player has never faced can never be found -- and
     rescanning 256MB for each of them, forever, saturates the main thread
     hard enough that requestAnimationFrame stops firing entirely.

     So: the player comes first (one cheap, very selective int32 key, and
     rooms/audio/aim all depend on it), then only screens in the CURRENT room,
     and any screen that completes a fruitless full pass backs off before it is
     allowed to try again. */
  var BACKOFF_MIN = 2000, BACKOFF_MAX = 20000;

  function acquire() {
    if (frames % ACQUIRE_EVERY !== 0) return;
    var now = performance.now();
    var t0 = now;

    if (playerAddr < 0) {
      if (!resolvePlayer()) scanPlayerChunk();
      scanCost = performance.now() - t0;
      return;
    }

    var pending = [];
    for (var i = 0; i < screens.length; i++) {
      var sc = screens[i];
      if (sc.addr >= 0) continue;
      if ((sc.nextTry || 0) > now) continue;
      if (currentRoom >= 0 && sc.def.room !== currentRoom) continue;
      pending.push(sc);
    }
    if (!pending.length) { scanCost = 0; return; }

    var pick = pending[acqIdx % pending.length];
    acqIdx++;
    if (!pick.primed && lastFound >= 0) {
      pick.cursor = Math.max(0, (lastFound >> 2) - (512 << 10));
      pick.primed = true;
    }
    pick.passDone = false;
    if (scanFor(pick)) {
      lastFound = pick.addr;
      pick.backoff = 0;
    } else if (pick.passDone) {
      // swept the whole heap and it simply is not there yet
      pick.backoff = Math.min(BACKOFF_MAX, Math.max(BACKOFF_MIN, (pick.backoff || 0) * 2));
      pick.nextTry = now + pick.backoff;
    }
    scanCost = performance.now() - t0;
  }

  function pump() {
    requestAnimationFrame(pump);
    if (!started || !refreshViews()) return;

    acquire();

    var p = playerPos();

    for (var i = 0; i < screens.length; i++) {
      var c = screens[i].def.centre;
      screens[i].dist = p ? Math.hypot(p[0] - c[0], p[1] - c[1]) : 1e9;
    }
    // only the current room's screens are candidates for blitting
    var order = screens.filter(function (s) {
      return currentRoom < 0 || s.def.room === currentRoom;
    }).sort(function (a, b) { return a.dist - b.dist; });

    for (var h = 0; h < HOT && h < order.length; h++) {
      if (order[h].addr >= 0) blit(order[h]);
    }
    for (var k = 0; k < WARM; k++) {
      var sc = order[HOT + ((rr + k) % Math.max(1, order.length - HOT))];
      if (sc && sc.addr >= 0) blit(sc);
    }
    rr += WARM;

    updateAudio(p);
    frames++;
    if ((frames & 15) === 0) renderStatus();
  }

  /* ------------------------- full-quality overlay ------------------------- */

  var overlay = null;

  function nearestScreen() {
    var best = null;
    for (var i = 0; i < screens.length; i++) {
      if (!best || screens[i].dist < best.dist) best = screens[i];
    }
    return best;
  }

  /* Which wall the player's crosshair is actually pointed at.
     Rather than comparing bearings (which happily "picks" a screen even when
     you are aiming at the plain wall beside it), cast the aim ray against the
     real wall segments from the WAD and see what it hits. The room is convex,
     so the ray crosses exactly one segment and there is no occlusion.
     Returns the screen for that wall, or null when a plain wall was hit. */
  function aimedScreen() {
    var p = playerPos(), a = playerAngle();
    if (!p || a === null) return null;
    var dx = Math.cos(a), dy = Math.sin(a);
    var walls = meta.walls, bestT = Infinity, hit = null;

    for (var i = 0; i < walls.length; i++) {
      var w = walls[i];
      var sx = w.b[0] - w.a[0], sy = w.b[1] - w.a[1];
      var den = dx * sy - dy * sx;
      if (Math.abs(den) < 1e-9) continue;                  // parallel
      var ox = w.a[0] - p[0], oy = w.a[1] - p[1];
      var t = (ox * sy - oy * sx) / den;                   // along the aim ray
      var u = (ox * dy - oy * dx) / den;                   // along the segment
      if (t > 0 && u >= 0 && u <= 1 && t < bestT) { bestT = t; hit = w; }
    }
    if (!hit || hit.screen === null || hit.screen === undefined) return null;
    return screens[hit.screen] || null;
  }

  function openOverlay(forced) {
    if (overlay) return;
    var sc = forced || aimedScreen() || nearestScreen();
    if (!sc || !sc.id) return;
    overlay = document.createElement('div');
    overlay.id = 'tribute-overlay';
    overlay.innerHTML =
      '<div class="to-inner">' +
      '  <button class="to-close" title="close (Esc)">×</button>' +
      '  <div class="wistia_responsive_padding" style="padding:56.25% 0 0 0;position:relative;">' +
      '    <div class="wistia_responsive_wrapper" style="height:100%;left:0;position:absolute;top:0;width:100%;">' +
      '      <iframe src="https://fast.wistia.net/embed/iframe/' + sc.id +
                 '?web_component=true&seo=false&autoPlay=true" title="' + (sc.name || 'Wistia video') + '" ' +
                 'allow="autoplay; fullscreen" allowtransparency="true" frameborder="0" scrolling="no" ' +
                 'class="wistia_embed" name="wistia_embed" width="100%" height="100%"></iframe>' +
      '    </div>' +
      '  </div>' +
      '  <div class="to-caption">' + (sc.name || '') + '</div>' +
      '</div>';
    document.body.appendChild(overlay);
    overlay.querySelector('.to-close').addEventListener('click', closeOverlay);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) closeOverlay(); });
    requestAnimationFrame(function () { overlay.classList.add('open'); });
  }

  function closeOverlay() {
    if (!overlay) return;
    overlay.remove();
    overlay = null;
    focusGame();
  }

  /* ------------------------------- chrome -------------------------------- */

  var statusEl;

  function renderStatus() {
    if (!statusEl) return;
    var located = screens.filter(function (s) { return s.addr >= 0; }).length;
    var p = playerPos();
    statusEl.textContent =
      located + '/' + screens.length + ' screens · ' +
      (currentRoom < 0 ? '' : meta.rooms[currentRoom].name + ' · ') +
      (p ? Math.round(nearestScreen().dist) + 'u away' : 'finding player') +
      ' · ' + frames + 'f · scan ' + scanCost.toFixed(1) + 'ms';
  }

  function focusGame() {
    var c = Module && Module.canvas;
    if (c) { c.setAttribute('tabindex', '0'); c.focus(); }
  }

  /* Fullscreen the stage ourselves rather than using Module.requestFullscreen:
     emscripten's helper either leaves the canvas at its native size (letterboxed
     off-centre) or resizes the backing store, which the software renderer can't
     follow. Scaling the element with CSS keeps aspect and stays crisp. */
  function goFullscreen() {
    var stage = document.getElementById('stage');
    if (!document.fullscreenElement) {
      (stage.requestFullscreen ? stage.requestFullscreen() : Promise.reject())
        .then(lockPointer)
        .catch(function () {});
    } else {
      document.exitFullscreen();
    }
    focusGame();
  }

  /* Without pointer lock the mouse runs out of window and turning stops, which
     reads as "I can't turn all the way around". */
  function lockPointer() {
    var c = Module && Module.canvas;
    if (c && c.requestPointerLock) { try { c.requestPointerLock(); } catch (e) {} }
  }

  /* --------------------------------- boot -------------------------------- */

  Promise.all([
    fetch('screen.json', NOCACHE).then(function (r) { return r.json(); }),
    fetch('palette.lut', NOCACHE).then(function (r) { return r.arrayBuffer(); }),
    fetch('screens.bin', NOCACHE).then(function (r) { return r.arrayBuffer(); }),
    fetch('videos.json', NOCACHE).then(function (r) { return r.json(); })
  ]).then(function (res) {
    meta = res[0];
    lut = new Uint8Array(res[1]);
    expected = new Uint8Array(res[2]);
    videoIds = res[3].screens;

    screens = meta.screens.map(function (def, i) {
      var cv = document.createElement('canvas');
      cv.width = def.width; cv.height = def.height;
      return {
        def: def, addr: -1, cursor: 0, wrote: false, dist: 1e9,
        buf: new Uint8Array(def.bufferLength),
        canvas: cv, ctx: cv.getContext('2d', { willReadFrequently: true }),
        video: null, id: videoIds[i] || null, name: null
      };
    });

    // resolve every wall's MP4 (and title) in parallel
    screens.forEach(function (sc) {
      if (!sc.id) return;
      resolveMp4(sc.id).then(function (r) {
        sc.name = r.name;
        sc.video = makeVideo(r.url);
        if (audioArmed) { sc.video.muted = false; sc.video.volume = 0; }
        // only the room the player is actually in gets to decode
        if (currentRoom < 0 || sc.def.room === currentRoom) {
          sc.video.play().catch(function () {});
        }
      }).catch(function () { /* leave this wall as static */ });
    });

    started = true;
    pump();
  });

  window.addEventListener('DOMContentLoaded', function () {
    var bar = document.createElement('div');
    bar.id = 'tribute-bar';
    bar.innerHTML = '<span id="tribute-status"></span>' +
                    '<button id="tribute-sound">🔇 enable sound</button>' +
                    '<button id="tribute-full">Fullscreen</button>' +
                    '<button id="tribute-watch">Watch full quality</button>';
    document.body.appendChild(bar);
    statusEl = bar.querySelector('#tribute-status');
    bar.querySelector('#tribute-sound').addEventListener('click', function () { armAudio(); this.blur(); focusGame(); });
    bar.querySelector('#tribute-full').addEventListener('click', function () { this.blur(); goFullscreen(); });
    bar.querySelector('#tribute-watch').addEventListener('click', function () { this.blur(); openOverlay(); });

    var hint = document.createElement('div');
    hint.id = 'tribute-hint';
    hint.textContent = 'W A S D move · Q E turn (or mouse) · three rooms to explore · shoot a screen to watch it';
    document.body.appendChild(hint);

    ['pointerdown', 'keydown'].forEach(function (ev) {
      document.addEventListener(ev, function () { armAudio(); }, { once: true });
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && overlay) closeOverlay();
    });

    /* Shooting a screen opens it. Fire is the LEFT MOUSE BUTTON in this build
       -- the Ctrl binding is commented out (src/m_misc.c:444) and mouseb_fire
       defaults to 0 (:389). We only open when the aim ray actually lands on a
       screen wall, so shooting the plain walls does nothing. */
    if (Module && Module.canvas) {
      Module.canvas.addEventListener('mousedown', function (e) {
        if (e.button !== 0 || overlay) return;
        var sc = aimedScreen();
        if (sc) openOverlay(sc);
      });
    }

    if (Module && Module.canvas) {
      Module.canvas.addEventListener('click', function () {
        focusGame();
        if (document.fullscreenElement) lockPointer();
      });
    }
  });

  window.__tribute = {
    build: BUILD,
    screens: function () { return screens.map(function (s) {
      return { name: s.def.name, addr: s.addr, dist: Math.round(s.dist), id: s.id, title: s.name };
    }); },
    player: playerPos,
    angle: playerAngle,
    aimed: function () { var f = aimedScreen(); return f && f.def.name; },
    open: openOverlay
  };
})();
