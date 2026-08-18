import sys, struct, math, json, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wad import Wad, palette, build_matcher, encode_picture
from bsp import BSP, Seg

SRC_WAD    = 'doom1.wad'
OUT_WAD    = 'tribute.wad'
OUT_DIR    = '/Users/qrush/wistia/WebDOOM/public'

WALL_TEX   = 'METAL1'
SCREEN_W, SCREEN_H = 256, 144   # exactly 16:9
FLOOR_FLAT = 'FLOOR4_8'
CEIL_FLAT  = 'CEIL3_5'
ROOM_H     = SCREEN_H

OCT_SIDE   = 320                # octagon wall length
DOOR_W     = 192                # hallway opening
HALL_LEN   = 420                # straight run between rooms

R  = OCT_SIDE / (2.0 * math.sin(math.pi / 8))   # circumradius
AP = R * math.cos(math.pi / 8)                  # apothem

HERE = os.path.dirname(os.path.abspath(__file__))
src = Wad(os.path.join(HERE, SRC_WAD))
pal = palette(src)
match = build_matcher(pal)

# ---------------------------------------------------------------------------
# Three octagonal rooms in a triangle: a main room with a hallway branching to
# each of the other two.
#
# Every floor and ceiling is at the same height, so the whole complex is a
# SINGLE sector whose boundary is one closed, non-self-intersecting loop --
# walk the main room clockwise and, at each doorway, detour out along the
# hallway, around the branch room, and back. That keeps every linedef
# one-sided and avoids two-sided lines entirely.
#
# It is emphatically NOT convex though, so unlike the single-room version this
# map needs a real BSP (bsp.py). Rendered without one, walls draw through each
# other -- prboom's numnodes==0 path (src/r_main.c:466) means "one subsector,
# draw everything", which only ever worked because one octagon is convex.
#
# Wall k faces outward at -pi*(k+1)/4, so wall 4 faces NW and wall 6 faces NE;
# each branch room's doorway is the wall pointing back toward main.
# ---------------------------------------------------------------------------

MAIN_C     = (0.0, 0.0)
MAIN_DOORS = {4: 0, 6: 2}       # main wall -> the branch room's facing wall

def _corner(c, k):
    a = -(math.pi / 8 + k * math.pi / 4)        # clockwise
    return (c[0] + R * math.cos(a), c[1] + R * math.sin(a))

def _wall_normal(k):
    return -math.pi * (k + 1) / 4.0

def _lerp(p, q, t):
    return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)

def _wall_pts(c, k, door=False):
    """corner, then the two edges of the centred screen (or doorway)."""
    a, b = _corner(c, k), _corner(c, k + 1)
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    w = DOOR_W if door else SCREEN_W
    return a, _lerp(a, b, (L - w) / 2.0 / L), _lerp(a, b, (L + w) / 2.0 / L)

BRANCH_C = {}
for _k in MAIN_DOORS:
    _a = _wall_normal(_k)
    _d = 2 * AP + HALL_LEN
    BRANCH_C[_k] = (MAIN_C[0] + _d * math.cos(_a), MAIN_C[1] + _d * math.sin(_a))

ROOMS = [('main', MAIN_C)] + [('branch%d' % k, BRANCH_C[k]) for k in sorted(MAIN_DOORS)]
ROOM_INDEX = dict((n, i) for i, (n, _) in enumerate(ROOMS))

EDGES   = []      # (a, b, wall_tex or None, screen_index or None)
SCREENS = []      # {'room', 'centre', 'a', 'b', 'name'}

def _edge(a, b, screen_room=None):
    if math.hypot(b[0] - a[0], b[1] - a[1]) < 1e-6:
        return
    if screen_room is None:
        EDGES.append((a, b, WALL_TEX, None))
    else:
        SCREENS.append({'room': ROOM_INDEX[screen_room],
                        'centre': ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0),
                        'a': a, 'b': b})
        EDGES.append((a, b, None, len(SCREENS) - 1))

def _plain_wall(c, k, room):
    a, s1, s2 = _wall_pts(c, k)
    _edge(a, s1)
    _edge(s1, s2, screen_room=room)
    _edge(s2, _corner(c, k + 1))

def _branch_loop(c, j, room, entry):
    """From the doorway edge we arrived at, clockwise all the way back to the
    other doorway edge.

    `entry` is the opening edge the hallway lands on; the stub of the doorway
    wall between it and the next corner still has to be emitted, otherwise the
    outline dangles there and the loop never closes."""
    _, e1, _e2 = _wall_pts(c, j, door=True)
    _edge(entry, _corner(c, j + 1))          # rest of the doorway wall
    for n in range(1, 8):
        _plain_wall(c, (j + n) % 8, room)
    _edge(_corner(c, j), e1)                 # up to the far opening edge
    return e1

for k in range(8):
    if k in MAIN_DOORS:
        c2, j = BRANCH_C[k], MAIN_DOORS[k]
        a, d1, d2 = _wall_pts(MAIN_C, k, door=True)
        _, e1, e2 = _wall_pts(c2, j, door=True)
        # pair the corridor walls so they run parallel rather than crossing
        if math.hypot(d1[0] - e2[0], d1[1] - e2[1]) > math.hypot(d1[0] - e1[0], d1[1] - e1[1]):
            e1, e2 = e2, e1
        _edge(a, d1)
        _edge(d1, e2)                                   # hallway wall, outbound
        r1 = _branch_loop(c2, j, 'branch%d' % k, e2)
        _edge(r1, d2)                                   # hallway wall, back
        _edge(d2, _corner(MAIN_C, k + 1))
    else:
        _plain_wall(MAIN_C, k, 'main')

NUM_SCREENS = len(SCREENS)
SCREEN_TEXS = ['WISTSC%02d' % i for i in range(NUM_SCREENS)]
for i, s in enumerate(SCREENS):
    s['name'] = SCREEN_TEXS[i]

# clockwise check: negative shoelace means one-sided fronts face the interior
_loop = [e[0] for e in EDGES]
_area = sum(_loop[i][0] * _loop[(i + 1) % len(_loop)][1] -
            _loop[(i + 1) % len(_loop)][0] * _loop[i][1]
            for i in range(len(_loop))) / 2.0
assert _area < 0, 'outline is wound counter-clockwise; walls would face outward'

VERTS = []
_vmap = {}
def _vid(p):
    key = (int(round(p[0])), int(round(p[1])))
    if key not in _vmap:
        _vmap[key] = len(VERTS)
        VERTS.append(key)
    return _vmap[key]

WALLS = []        # (v_from, v_to, texture)
for (a, b, tex, sidx) in EDGES:
    WALLS.append((_vid(a), _vid(b), tex if tex else SCREEN_TEXS[sidx]))

PLAYER_X, PLAYER_Y = -151, -233   # in the main room; deliberately odd numbers
                                  # so the heap search for the mobj is selective
PLAYER_ANGLE = 45                 # P_SpawnPlayer keeps only multiples of 45

THINGS = [(PLAYER_X, PLAYER_Y, PLAYER_ANGLE, 1, 7)]

# ---------------------------------------------------------------------------
# map lumps (struct layouts: src/doomdata.h)
# ---------------------------------------------------------------------------

def enc_vertexes():
    return b''.join(struct.pack('<hh', x, y) for x, y in VERTS)

def enc_linedefs():
    out = b''
    for i, (a, b, _t) in enumerate(WALLS):
        out += struct.pack('<HHHhhHH', a, b, 1, 0, 0, i, 0xFFFF)
    return out

def enc_sidedefs():
    out = b''
    for _a, _b, tex in WALLS:
        out += struct.pack('<hh8s8s8sh', 0, 0, b'-', b'-', tex.encode()[:8], 0)
    return out

def enc_sectors():
    return struct.pack('<hh8s8shhh', 0, ROOM_H, FLOOR_FLAT.encode()[:8],
                       CEIL_FLAT.encode()[:8], 224, 0, 0)

def build_bsp():
    """Real BSP over the outline. The single-room map shipped zero nodes, which
    prboom reads as "one subsector, draw everything" -- correct only while the
    map is convex, which it no longer is."""
    segs = [Seg(VERTS[a][0], VERTS[a][1], VERTS[b][0], VERTS[b][1], i)
            for i, (a, b, _t) in enumerate(WALLS)]
    tree = BSP(segs)
    return tree, tree.encode(lambda x, y: _vid((x, y)))

def enc_things():
    return b''.join(struct.pack('<hhhhh', *t) for t in THINGS)

# ---------------------------------------------------------------------------
# the screen texture
#
# Filled with deterministic "TV static" drawn from the palette's true greys.
# Two jobs at once:
#   1. before playback it reads as a switched-on but idle screen
#   2. prboom composites a fully-opaque single-patch texture verbatim into
#      rpatch_t.pixels (src/r_patch.c:476 memsets 0xff then copies every
#      post), and that buffer is COLUMN-major -- columns[x].pixels =
#      pixels + x*height (r_patch.c:277). So the composited bytes equal this
#      image exactly, in a known order, which lets the runtime locate the
#      buffer in the wasm heap by searching for a slice of it.
# ---------------------------------------------------------------------------

GREYS = sorted([i for i in range(256) if pal[i][0] == pal[i][1] == pal[i][2]],
               key=lambda i: pal[i][0])
assert len(GREYS) >= 8, GREYS
RAMP = GREYS[2:len(GREYS) // 2]        # darker half -> reads as an idle screen

def static_pixels(seed):
    """Deterministic LCG static, row-major, len = SCREEN_W*SCREEN_H.

    Each screen gets its own seed so every texture has a distinct byte
    pattern -- that is what makes the per-screen heap search unambiguous."""
    px = [0] * (SCREEN_W * SCREEN_H)
    st = seed & 0xFFFFFFFF
    for y in range(SCREEN_H):
        for x in range(SCREEN_W):
            st = (1103515245 * st + 12345) & 0xFFFFFFFF
            px[y * SCREEN_W + x] = RAMP[(st >> 16) % len(RAMP)]
    return px

def composite_bytes(px):
    """The exact bytes prboom will hold in rpatch_t.pixels: column-major."""
    out = bytearray(SCREEN_W * SCREEN_H)
    for x in range(SCREEN_W):
        base = x * SCREEN_H
        for y in range(SCREEN_H):
            out[base + y] = px[y * SCREEN_W + x]
    return bytes(out)

SCREEN_PIXELS = [static_pixels(0x1BADB002 + i * 0x9E3779B9) for i in range(NUM_SCREENS)]
COMPOSITES    = [composite_bytes(p) for p in SCREEN_PIXELS]

# ---------------------------------------------------------------------------
# PNAMES / TEXTURE1
#
# A PWAD's PNAMES and TEXTURE1 REPLACE the IWAD's wholesale (W_GetNumForName
# returns the last match), so both must carry every original entry plus ours.
# ---------------------------------------------------------------------------

def build_pnames():
    p = src.get('PNAMES')
    n = struct.unpack('<i', p[:4])[0]
    names = [p[4 + i * 8: 12 + i * 8] for i in range(n)]
    first_new = len(names)
    for t in SCREEN_TEXS:
        names.append(t.encode('ascii').ljust(8, b'\0'))
    return struct.pack('<i', len(names)) + b''.join(names), first_new

def build_texture1(first_patch_index):
    t = src.get('TEXTURE1')
    n = struct.unpack('<i', t[:4])[0]
    offs = struct.unpack('<%di' % n, t[4:4 + 4 * n])
    # slice each original texture record out verbatim
    records = []
    for i, o in enumerate(offs):
        npatch = struct.unpack('<h', t[o + 20:o + 22])[0]
        end = o + 22 + npatch * 10
        records.append(t[o:end])
    # our screens: each one patch, full size, at origin
    for i, tname in enumerate(SCREEN_TEXS):
        rec = struct.pack('<8sihhih', tname.encode('ascii').ljust(8, b'\0'),
                          0, SCREEN_W, SCREEN_H, 0, 1)
        rec += struct.pack('<hhhhh', 0, 0, first_patch_index + i, 1, 0)
        records.append(rec)

    count = len(records)
    header = 4 + 4 * count
    offsets, body, cur = [], b'', header
    for r in records:
        offsets.append(cur); body += r; cur += len(r)
    return struct.pack('<i', count) + struct.pack('<%di' % count, *offsets) + body

# ---------------------------------------------------------------------------
# camera sprite (replaces the pistol)
# ---------------------------------------------------------------------------

BODY_DK, BODY_LT, BODY_MID = match(58, 58, 64), match(120, 120, 128), match(84, 84, 92)
LENS_RING, LENS_GLASS, LENS_GLINT = match(15, 15, 17), match(35, 65, 105), match(215, 230, 240)
STRAP, REC_RED = match(92, 62, 34), match(200, 40, 40)
FLASH_HOT, FLASH_MID, FLASH_EDGE = match(255, 255, 235), match(255, 235, 120), match(255, 190, 40)

def canvas(w, h): return [None] * (w * h)

def px_(g, w, h, x, y, c):
    x, y = int(round(x)), int(round(y))
    if 0 <= x < w and 0 <= y < h: g[y * w + x] = c

def rect(g, w, h, x0, y0, x1, y1, c):
    for y in range(int(y0), int(y1)):
        for x in range(int(x0), int(x1)): px_(g, w, h, x, y, c)

def circle(g, w, h, cx, cy, r, c):
    for y in range(int(cy - r - 1), int(cy + r + 2)):
        for x in range(int(cx - r - 1), int(cx + r + 2)):
            if math.hypot(x - cx, y - cy) <= r: px_(g, w, h, x, y, c)

def line(g, w, h, x0, y0, x1, y1, c, thick=1):
    steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    for i in range(steps + 1):
        t = i / steps
        cx, cy = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        for ox in range(-thick // 2, thick // 2 + 1):
            for oy in range(-thick // 2, thick // 2 + 1):
                px_(g, w, h, cx + ox, cy + oy, c)

def camera_frame(w, h, scale, bx, by):
    """A shoulder-held camcorder, drawn to read at DOOM's sprite resolution.

    Silhouette does the work: a long low body (camcorders are wider than they
    are tall, unlike the boxy stills camera this replaced), a fat lens barrel
    jutting forward on the left, a pale flip-out LCD facing the player on the
    right, and a carry handle across the top. Everything gets a dark outline
    so the shape survives the palette + low resolution."""
    g = canvas(w, h)
    S = scale
    bw, bh = 44 * S, 24 * S
    o = 2 * S

    # --- carry handle across the top ---
    hx0, hx1 = bx + bw * 0.20, bx + bw * 0.78
    rect(g, w, h, hx0 - o, by - 11 * S - o, hx1 + o, by - 5 * S + o, BODY_DK)
    rect(g, w, h, hx0, by - 11 * S, hx1, by - 6 * S, BODY_MID)
    # handle posts down to the body
    rect(g, w, h, hx0, by - 6 * S, hx0 + 5 * S, by, BODY_DK)
    rect(g, w, h, hx1 - 5 * S, by - 6 * S, hx1, by, BODY_DK)

    # --- main body ---
    rect(g, w, h, bx - o, by - o, bx + bw + o, by + bh + o, BODY_DK)
    rect(g, w, h, bx, by, bx + bw, by + bh, BODY_MID)
    rect(g, w, h, bx, by, bx + bw, by + 4 * S, BODY_LT)              # top highlight
    rect(g, w, h, bx, by + bh - 4 * S, bx + bw, by + bh, BODY_DK)    # underside shade

    # --- lens barrel, jutting forward off the left end ---
    lcx, lcy = bx - 3 * S, by + bh * 0.52
    lr = bh * 0.46
    rect(g, w, h, bx - 9 * S - o, lcy - lr - o, bx + 8 * S, lcy + lr + o, BODY_DK)
    rect(g, w, h, bx - 9 * S, lcy - lr, bx + 8 * S, lcy + lr, BODY_MID)
    circle(g, w, h, lcx - 5 * S, lcy, lr + o, BODY_DK)               # barrel rim
    circle(g, w, h, lcx - 5 * S, lcy, lr * 0.82, LENS_RING)
    circle(g, w, h, lcx - 5 * S, lcy, lr * 0.52, LENS_GLASS)
    circle(g, w, h, lcx - 6 * S, lcy - lr * 0.28, lr * 0.18, LENS_GLINT)

    # --- flip-out LCD panel on the right, angled toward the player ---
    px0, py0 = bx + bw * 0.52, by + 3 * S
    px1, py1 = bx + bw * 0.98, by + bh * 0.78
    rect(g, w, h, px0 - o, py0 - o, px1 + o, py1 + o, BODY_DK)
    rect(g, w, h, px0, py0, px1, py1, LENS_GLASS)
    rect(g, w, h, px0 + S, py0 + S, px1 - S, py0 + 3 * S, LENS_GLINT)  # screen sheen

    # --- red REC lamp on the body, left of the LCD ---
    rect(g, w, h, bx + bw * 0.30, by + 7 * S, bx + bw * 0.30 + 5 * S, by + 12 * S, REC_RED)

    # --- hand strap under the body ---
    line(g, w, h, bx + bw * 0.30, by + bh, bx + bw * 0.72, by + bh, STRAP, thick=int(3 * S))
    line(g, w, h, bx + bw * 0.30, by + bh, bx + bw * 0.26, by + bh + 7 * S, STRAP, thick=int(2 * S))
    line(g, w, h, bx + bw * 0.72, by + bh, bx + bw * 0.76, by + bh + 7 * S, STRAP, thick=int(2 * S))
    return g

def flash_frame(w, h, cx, cy, r):
    g = canvas(w, h)
    circle(g, w, h, cx, cy, r * 0.7, FLASH_MID)
    circle(g, w, h, cx, cy, r * 0.4, FLASH_HOT)
    for i in range(8):
        a = (2 * math.pi / 8) * i
        line(g, w, h, cx + math.cos(a) * r * 0.5, cy + math.sin(a) * r * 0.5,
             cx + math.cos(a) * r * 1.05, cy + math.sin(a) * r * 1.05, FLASH_EDGE, thick=2)
    return g

def build_sprite_replacements():
    frames = {}
    for name, w, h, xo, yo, sc, bx, by in [
        ('PISGA0', 57, 62, -126, -106, 0.92, 12, 24),
        ('PISGB0', 79, 82, -104,  -86, 1.15, 15, 32),
        ('PISGC0', 66, 81, -119,  -87, 1.02, 13, 33),
    ]:
        frames[name] = encode_picture(w, h, camera_frame(w, h, sc, bx, by), xo, yo, None)
    w, h = 41, 38
    frames['PISFA0'] = encode_picture(w, h, flash_frame(w, h, w * 0.55, h * 0.55, 15),
                                      -140, -66, None)
    return frames

# ---------------------------------------------------------------------------
# assemble
# ---------------------------------------------------------------------------

pnames, patch_idx = build_pnames()

out = Wad()
out.lumps.append(('E1M1', b''))
out.lumps.append(('THINGS', enc_things()))
out.lumps.append(('LINEDEFS', enc_linedefs()))
out.lumps.append(('SIDEDEFS', enc_sidedefs()))
_tree, (_segs, _ssectors, _nodes) = build_bsp()
out.lumps.append(('SEGS', _segs))
out.lumps.append(('SSECTORS', _ssectors))
out.lumps.append(('NODES', _nodes))
out.lumps.insert(4, ('VERTEXES', enc_vertexes()))   # after BSP: splits add vertices
out.lumps.append(('SECTORS', enc_sectors()))
out.lumps.append(('REJECT', b''))     # auto-padded
out.lumps.append(('BLOCKMAP', b''))   # <8 bytes -> P_CreateBlockMap()

out.lumps.append(('PNAMES', pnames))
out.lumps.append(('TEXTURE1', build_texture1(patch_idx)))
for i, tname in enumerate(SCREEN_TEXS):
    out.lumps.append((tname, encode_picture(SCREEN_W, SCREEN_H, SCREEN_PIXELS[i], 0, 0, None)))

# sprites must live in the ns_sprites namespace (src/w_wad.c:418)
out.lumps.append(('S_START', b''))
for name, data in build_sprite_replacements().items():
    out.lumps.append((name, data))
out.lumps.append(('S_END', b''))

out.save(os.path.join(HERE, OUT_WAD))
print('wrote %s: %d lumps' % (OUT_WAD, len(out.lumps)))

# ---------------------------------------------------------------------------
# runtime sidecars for the JS video streamer
# ---------------------------------------------------------------------------

# The signature MUST span a column boundary. A single column's pixels also
# appear contiguously inside the raw WISTSCRN *lump* (DOOM picture format
# stores whole columns), and that lump is resident in the wasm heap too --
# matching on one column locks onto the lump instead of the composite and
# streams video into the wrong memory. Across a column boundary the lump has
# post headers (pad/0xff/topdelta/length) where the composite has pixels, so
# 3 columns' worth is unambiguous.
# The signature MUST span more than one column. A single column's pixels also
# appear contiguously inside the raw WISTSCR* *lump* (DOOM picture format
# stores whole columns), and those lumps are resident in the wasm heap too --
# matching on one column locks onto the lump instead of the composite and
# streams video into the wrong memory. Across a column boundary the lump has
# post headers (pad/0xff/topdelta/length) where the composite has pixels.
SIG_LEN = SCREEN_H * 3

screens = []
for i, tname in enumerate(SCREEN_TEXS):
    comp = COMPOSITES[i]
    cx, cy = SCREENS[i]['centre']
    screens.append({
        'index': i,
        'name': tname,
        'width': SCREEN_W,
        'height': SCREEN_H,
        'bufferLength': len(comp),
        'offset': i * len(comp),          # into screens.bin
        'signature': list(comp[:SIG_LEN]),
        'centre': [round(cx), round(cy)],
        'room': SCREENS[i]['room'],
    })

# every screen's full expected composite, concatenated, for byte-for-byte
# verification of a candidate heap address
open(os.path.join(OUT_DIR, 'screens.bin'), 'wb').write(b''.join(COMPOSITES))

LUT_PATH = os.path.join(OUT_DIR, 'palette.lut')
if os.path.exists(LUT_PATH) and len(open(LUT_PATH, 'rb').read()) == 32768:
    lut = bytearray(open(LUT_PATH, 'rb').read())
    print('reusing existing palette.lut')
else:
  lut = bytearray(32 * 32 * 32)
  for r5 in range(32):
      for g5 in range(32):
        for b5 in range(32):
            r, g, b = r5 * 8 + 4, g5 * 8 + 4, b5 * 8 + 4
            best, bestd = 0, 1 << 30
            for i in range(256):
                pr, pg, pb = pal[i]
                d = 3 * (pr - r) ** 2 + 6 * (pg - g) ** 2 + (pb - b) ** 2
                if d < bestd: best, bestd = i, d
            lut[(r5 << 10) | (g5 << 5) | b5] = best

open(LUT_PATH, 'wb').write(bytes(lut))
open(os.path.join(OUT_DIR, 'screen.json'), 'w').write(json.dumps({
    'fracBits': 16,
    'screens': screens,
    # DOOM stores positions as fixed_t (16.16). The player's mobj holds
    # x, y, z as three consecutive int32s, so the spawn triple is the search
    # key; z at spawn is the floor height (0).
    'player': {
        'spawn':  [PLAYER_X << 16, PLAYER_Y << 16, 0],
        # mobj_t.angle five int32s past x (thinker, x, y, z, snext*, sprev*,
        # angle -- p_mobj.h:248, 4-byte pointers on wasm32). angle_t is a BAM
        # where the full circle is 2^32, so the spawn facing is an exact,
        # checkable value. That identifies the real player mobj immediately;
        # waiting for its coordinates to change means it stays unfound until
        # the player happens to walk.
        'spawnAngle': (PLAYER_ANGLE * (1 << 32)) // 360,
        'angleWordOffset': 5,
        'bounds': [min(v[0] for v in VERTS) << 16, min(v[1] for v in VERTS) << 16,
                   max(v[0] for v in VERTS) << 16, max(v[1] for v in VERTS) << 16],
    },
    # Every wall segment, so the runtime can raycast the player's aim and know
    # exactly which wall is being shot. The room is convex, so a ray from any
    # interior point crosses exactly one segment -- no occlusion to worry about.
    'rooms': [{'name': n, 'centre': [round(c[0]), round(c[1])]} for (n, c) in ROOMS],
    'walls': [
        {'a': list(VERTS[a]), 'b': list(VERTS[b]),
         'screen': (SCREEN_TEXS.index(t) if t in SCREEN_TEXS else None)}
        for (a, b, t) in WALLS
    ],
    'note': 'composites are column-major: pixels[x*height + y] (src/r_patch.c:277)',
}, indent=1))
print('wrote %d screens, screens.bin (%d bytes), sig %d bytes each'
      % (len(screens), len(screens) * SCREEN_W * SCREEN_H, SIG_LEN))

# ---------------------------------------------------------------------------
# sprite previews (debug aid; `python3 build_tribute.py --preview`)
# ---------------------------------------------------------------------------
if '--preview' in sys.argv:
    from png import write_png

    def preview(name, w, h, g, scale=6):
        rows = []
        for y in range(h):
            row = bytearray()
            for x in range(w):
                c = g[y * w + x]
                if c is None:
                    bg = (30, 30, 34) if (x // 8 + y // 8) % 2 == 0 else (46, 46, 52)
                    px = bytes(bg) + b'\xff'
                else:
                    px = bytes(pal[c]) + b'\xff'
                row += px * scale
            for _ in range(scale):
                rows.append(bytes(row))
        write_png(os.path.join(HERE, 'preview_%s.png' % name), w * scale, h * scale, rows)

    for nm, w, h, sc, bx, by in [('PISGA0', 57, 62, 0.92, 12, 24),
                                 ('PISGB0', 79, 82, 1.15, 15, 32),
                                 ('PISGC0', 66, 81, 1.02, 13, 33)]:
        preview(nm, w, h, camera_frame(w, h, sc, bx, by))
    preview('PISFA0', 41, 38, flash_frame(41, 38, 41 * 0.55, 38 * 0.55, 15))
    print('wrote preview_*.png')
