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

SIDE       = 320                # wall length, the same in every room
DOOR_W     = 192                # hallway opening
HALL_LEN   = 420                # straight run between rooms

def _circumradius(n): return SIDE / (2.0 * math.sin(math.pi / n))
def _apothem(n):      return _circumradius(n) * math.cos(math.pi / n)

HERE = os.path.dirname(os.path.abspath(__file__))
src = Wad(os.path.join(HERE, SRC_WAD))
pal = palette(src)
match = build_matcher(pal)

# ---------------------------------------------------------------------------
# Two octagonal rooms joined by a hallway: you spawn in the Lenny room and walk
# north-east into the room where the team speaks.
#
# One doorway per room means seven walls left over in each, which is exactly
# the seven-videos-a-room split we want. (The three-room version spent two of
# the main room's walls on doorways, so it could only ever be 6/7/7.)
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
# Each room carries its own side count, so one can be given an extra wall
# without touching the other. The team room is a NONAGON: one wall goes to the
# doorway, and an octagon leaves only seven after that -- one short of the
# eight people who recorded a message.
# ---------------------------------------------------------------------------

ROOMS = [
    {'name': 'main', 'key': 'lenny', 'label': 'Lenny',         'sides': 8},
    {'name': 'team', 'key': 'team',  'label': 'From the team', 'sides': 9},
]
MAIN, TEAM = ROOMS
MAIN_DOOR  = 6                  # main wall the hallway leaves by
TEAM_DOOR  = 0                  # team wall it arrives at
ROOM_INDEX = dict((r['name'], i) for i, r in enumerate(ROOMS))

def _normal(rm, k):
    """Outward bearing of wall k -- the direction its midpoint faces."""
    return -2.0 * math.pi * (k + 1) / rm['sides'] + rm['rot']

def _corner(rm, k):
    n = rm['sides']
    a = -(math.pi / n + k * 2.0 * math.pi / n) + rm['rot']   # clockwise
    return (rm['c'][0] + rm['R'] * math.cos(a),
            rm['c'][1] + rm['R'] * math.sin(a))

for _rm in ROOMS:
    _rm['R'], _rm['AP'] = _circumradius(_rm['sides']), _apothem(_rm['sides'])
MAIN['rot'], MAIN['c'] = 0.0, (0.0, 0.0)

# Rotate the team room so its doorway wall points straight back down the
# hallway. With equal side counts some wall always happened to line up; with
# 8 against 9 none does, and without the rotation the corridor would meet the
# room at an angle and the outline would cross itself.
_out = _normal(MAIN, MAIN_DOOR)
TEAM['rot'] = _out + math.pi + 2.0 * math.pi * (TEAM_DOOR + 1) / TEAM['sides']
_d = MAIN['AP'] + HALL_LEN + TEAM['AP']
TEAM['c'] = (MAIN['c'][0] + _d * math.cos(_out), MAIN['c'][1] + _d * math.sin(_out))

def _lerp(p, q, t):
    return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)

def _wall_pts(rm, k, door=False):
    """corner, then the two edges of the centred screen (or doorway)."""
    a, b = _corner(rm, k), _corner(rm, k + 1)
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    w = DOOR_W if door else SCREEN_W
    return a, _lerp(a, b, (L - w) / 2.0 / L), _lerp(a, b, (L + w) / 2.0 / L)

EDGES   = []      # (a, b, wall_tex or None, screen_index or None)
SCREENS = []      # {'room', 'centre', 'a', 'b', 'name'}

def _edge(a, b, screen_room=None):
    if math.hypot(b[0] - a[0], b[1] - a[1]) < 1e-6:
        return
    if screen_room is None:
        EDGES.append((a, b, WALL_TEX, None, None))
    else:
        SCREENS.append({'room': ROOM_INDEX[screen_room],
                        'centre': ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0),
                        'a': a, 'b': b})
        EDGES.append((a, b, None, len(SCREENS) - 1, None))

def _plain_wall(rm, k):
    """A screen centred on the wall, with a plain stub either side.

    The stubs are tagged with the screen they flank. A 256-wide screen on a
    320-wide wall leaves 32 units of METAL1 at each end, and from the middle of
    a room that margin swallows about a quarter of every full turn -- so a
    quarter of the shots aimed at a video hit bare wall instead. Treating the
    whole octagon side as one target makes aiming say what it looks like it
    says, without shrinking the margin to nothing."""
    a, s1, s2 = _wall_pts(rm, k)
    lead = len(EDGES)
    _edge(a, s1)
    _edge(s1, s2, screen_room=rm['name'])
    idx = len(SCREENS) - 1
    _edge(s2, _corner(rm, k + 1))
    for e in range(lead, len(EDGES)):
        if EDGES[e][3] is None:
            EDGES[e] = EDGES[e][:4] + (idx,)

def _branch_loop(rm, j, entry):
    """From the doorway edge we arrived at, clockwise all the way back to the
    other doorway edge.

    `entry` is the opening edge the hallway lands on; the stub of the doorway
    wall between it and the next corner still has to be emitted, otherwise the
    outline dangles there and the loop never closes."""
    _, e1, _e2 = _wall_pts(rm, j, door=True)
    _edge(entry, _corner(rm, j + 1))         # rest of the doorway wall
    for n in range(1, rm['sides']):
        _plain_wall(rm, (j + n) % rm['sides'])
    _edge(_corner(rm, j), e1)                # up to the far opening edge
    return e1

for k in range(MAIN['sides']):
    if k == MAIN_DOOR:
        a, d1, d2 = _wall_pts(MAIN, k, door=True)
        _, e1, e2 = _wall_pts(TEAM, TEAM_DOOR, door=True)
        # pair the corridor walls so they run parallel rather than crossing
        if math.hypot(d1[0] - e2[0], d1[1] - e2[1]) > math.hypot(d1[0] - e1[0], d1[1] - e1[1]):
            e1, e2 = e2, e1
        _edge(a, d1)
        _edge(d1, e2)                                   # hallway wall, outbound
        r1 = _branch_loop(TEAM, TEAM_DOOR, e2)
        _edge(r1, d2)                                   # hallway wall, back
        _edge(d2, _corner(MAIN, k + 1))
    else:
        _plain_wall(MAIN, k)

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

WALLS = []        # (v_from, v_to, texture, flanked screen or None)
for (a, b, tex, sidx, flank) in EDGES:
    WALLS.append((_vid(a), _vid(b), tex if tex else SCREEN_TEXS[sidx], flank))

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
    for i, (a, b, _t, _f) in enumerate(WALLS):
        out += struct.pack('<HHHhhHH', a, b, 1, 0, 0, i, 0xFFFF)
    return out

def enc_sidedefs():
    out = b''
    for _a, _b, tex, _f in WALLS:
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
            for i, (a, b, _t, _f) in enumerate(WALLS)]
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

# ---------------------------------------------------------------------------
# First-person camcorder, held in both hands.
#
# Drawn from BEHIND, the way you actually see a camcorder you are holding:
# eyepiece cup up on the left, ribbed battery pack filling the back, red REC
# button on the right, the lens barrel disappearing away from you, and both
# hands wrapped around it. The old sprite was a side-on camcorder floating in
# space with no hands, which read as an object rather than a point of view.
#
# Sprite placement: DOOM puts a psprite's left edge at -leftoffset in 320-wide
# space, and pushes it further down the more negative topoffset is. The stock
# pistol frames encode that as (h - 62) added to a -106 topoffset, which
# bottom-anchors them. We keep the same rule and centre horizontally, so the
# larger two-handed sprite still sits where a weapon should.
# ---------------------------------------------------------------------------

BODY_BLACK = match(24, 24, 26)
BODY_DK    = match(46, 46, 50)
BODY_MID   = match(74, 74, 80)
BODY_LT    = match(112, 112, 120)
BATT       = match(38, 38, 42)
BATT_RIB   = match(62, 62, 68)
EYECUP     = match(16, 16, 18)
EYEGLASS   = match(52, 58, 74)
REC_RED    = match(196, 34, 34)
REC_HOT    = match(255, 96, 84)
SKIN_LT    = match(206, 162, 124)
SKIN_MID   = match(174, 128, 94)
SKIN_SH    = match(128, 88, 62)
SKIN_DK    = match(92, 62, 44)

SPR_W, SPR_H = 150, 95            # canvas for every held frame
# Shared by the held frames and the fullbright overlay so the lamp lines up
# exactly. Sits in the clear strip between the left fingertips (which reach
# x~46) and the battery pack (which starts at x=66) -- on the right, where a
# real camcorder puts it, the thumb covers it and the indicator never shows.
REC_POS      = (56, 58)

def canvas(w, h):
    return [None] * (w * h)

def px_(g, w, h, x, y, c):
    x, y = int(round(x)), int(round(y))
    if 0 <= x < w and 0 <= y < h:
        g[y * w + x] = c

def rect(g, w, h, x0, y0, x1, y1, c):
    for y in range(int(round(y0)), int(round(y1))):
        for x in range(int(round(x0)), int(round(x1))):
            px_(g, w, h, x, y, c)

def ellipse(g, w, h, cx, cy, rx, ry, c):
    for y in range(int(cy - ry - 1), int(cy + ry + 2)):
        for x in range(int(cx - rx - 1), int(cx + rx + 2)):
            if rx > 0 and ry > 0 and ((x - cx) / float(rx)) ** 2 + ((y - cy) / float(ry)) ** 2 <= 1.0:
                px_(g, w, h, x, y, c)

def circle(g, w, h, cx, cy, r, c):
    ellipse(g, w, h, cx, cy, r, r, c)

def capsule(g, w, h, x0, y0, x1, y1, r, c):
    """A finger: a thick line with rounded ends."""
    steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    for i in range(steps + 1):
        t = i / float(steps)
        circle(g, w, h, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, r, c)

def camcorder_body(g, w, h, dy, recording):
    """The camcorder itself, seen from behind."""
    bx0, bx1 = 30, 120
    by0, by1 = 14 + dy, 76 + dy

    # body block, with a lit top edge and shaded underside for form
    rect(g, w, h, bx0 - 2, by0 - 2, bx1 + 2, by1 + 2, BODY_BLACK)
    rect(g, w, h, bx0, by0, bx1, by1, BODY_MID)
    rect(g, w, h, bx0, by0, bx1, by0 + 5, BODY_LT)
    rect(g, w, h, bx0, by1 - 6, bx1, by1, BODY_DK)

    # eyepiece cup, up and to the left -- the strongest "camcorder" cue
    ecx, ecy = 50, by0 + 6
    ellipse(g, w, h, ecx, ecy, 19, 13, BODY_BLACK)
    ellipse(g, w, h, ecx, ecy, 15, 10, EYECUP)
    ellipse(g, w, h, ecx, ecy, 7, 5, EYEGLASS)
    rect(g, w, h, ecx - 6, ecy + 8, ecx + 8, by0 + 14, BODY_DK)   # neck to body

    # battery pack: big ribbed slab across the back
    px0, py0, px1, py1 = 66, by0 + 12, 114, by1 - 10
    rect(g, w, h, px0 - 1, py0 - 1, px1 + 1, py1 + 1, BODY_BLACK)
    rect(g, w, h, px0, py0, px1, py1, BATT)
    for rx in range(px0 + 5, px1 - 3, 7):
        rect(g, w, h, rx, py0 + 4, rx + 2, py1 - 4, BATT_RIB)

    # cassette door seam and a couple of controls on the left of the body
    rect(g, w, h, bx0 + 4, by0 + 16, bx0 + 26, by0 + 18, BODY_DK)
    rect(g, w, h, bx0 + 4, by0 + 24, bx0 + 14, by0 + 28, BODY_LT)

    # REC button
    rcx, rcy = REC_POS[0], REC_POS[1] + dy
    circle(g, w, h, rcx, rcy, 5, BODY_BLACK)
    circle(g, w, h, rcx, rcy, 3, REC_HOT if recording else REC_RED)

    # the lens end, falling away from the viewer on the right
    rect(g, w, h, bx1, by0 + 12, bx1 + 10, by1 - 10, BODY_DK)
    ellipse(g, w, h, bx1 + 10, (by0 + by1) / 2, 5, 14, BODY_BLACK)

def hands(g, w, h, dy):
    """Both hands wrapped round the body, first-person grip.

    Drawn in PHASES -- every dark rim first, then every fill, then the
    highlights -- rather than finishing one finger before starting the next.
    Per-finger outlining looks fine in isolation but each new outline carves a
    gap out of the finger beside it, so the hand came out as a stack of
    detached sausages instead of one connected mass.

    Restrained on purpose: palms tucked into the bottom corners, three fingers
    a side, tips overlapping the body edge by about ten pixels. At DOOM's
    resolution the silhouette is all you get, so the camcorder stays the hero
    and the hands only frame it."""

    def hand(px, py, tips, thumb, mirror):
        d = -1 if mirror else 1
        shapes = []
        # palm mass
        shapes.append(('e', px, py, 20, 16))
        shapes.append(('r', px - 18 * (0 if mirror else 1) - (18 if mirror else 0),
                       py - 2, 36, h))
        # fingers, and the thumb
        for i, fy in enumerate(tips):
            shapes.append(('c', px - d * 4, fy + 5 + dy, px + d * (34 - i * 3), fy + dy, 5))
        shapes.append(('c', thumb[0], thumb[1] + dy, thumb[2], thumb[3] + dy, thumb[4]))
        return shapes

    left  = hand(16, 88 + dy, (52, 63, 74), (14, 82, 40, 78, 5), mirror=False)
    # the right thumb has to START down at the palm and reach up to the body,
    # otherwise it renders as a bar floating in space beside the camcorder
    right = hand(134, 88 + dy, (54, 65, 76), (146, 74, 110, 30, 6), mirror=True)

    for phase, col, grow in ((0, SKIN_DK, 1), (1, SKIN_MID, 0), (2, SKIN_LT, -2)):
        for shapes in (left, right):
            for sh in shapes:
                if sh[0] == 'e':
                    _, cx, cy, rx, ry = sh
                    if phase == 2:
                        ellipse(g, w, h, cx - 2, cy + 4, rx - 8, ry - 7, col)
                    else:
                        ellipse(g, w, h, cx, cy, rx + grow, ry + grow, col)
                elif sh[0] == 'r':
                    if phase == 2:
                        continue
                    _, x, y, ww, hh = sh
                    rect(g, w, h, x - grow, y - grow, x + ww + grow, y + hh, col)
                else:
                    _, x0, y0, x1, y1, r = sh
                    rr = max(1, r + grow)
                    if phase == 2:
                        capsule(g, w, h, x0 + 1, y0 - 1, x1 - 1, y1 - 1, rr, col)
                    else:
                        capsule(g, w, h, x0, y0, x1, y1, rr, col)

def camera_frame(w, h, dy=0, recording=False):
    g = canvas(w, h)
    camcorder_body(g, w, h, dy, recording)
    return g

def flash_frame(w, h, dy=0):
    """Fullbright overlay: just the REC lamp glowing, no muzzle flash."""
    g = canvas(w, h)
    cx, cy = REC_POS[0], REC_POS[1] + dy
    circle(g, w, h, cx, cy, 6, REC_RED)
    circle(g, w, h, cx, cy, 4, REC_HOT)
    return g

def _union_bbox(frames, w, h):
    """Tightest box containing artwork from ALL frames.

    Cropping each frame to its own bounds would let the recording frames --
    which sit a few pixels higher -- shift relative to the idle one, so the
    camcorder would visibly jump when you pull the trigger."""
    x0, y0, x1, y1 = w, h, 0, 0
    for g in frames:
        for y in range(h):
            row = y * w
            for x in range(w):
                if g[row + x] is not None:
                    if x < x0: x0 = x
                    if x > x1: x1 = x
                    if y < y0: y0 = y
                    if y > y1: y1 = y
    return x0, y0, x1 + 1, y1 + 1

def _crop(g, w, h, box):
    x0, y0, x1, y1 = box
    return [g[y * w + x] for y in range(y0, y1) for x in range(x0, x1)]

def build_sprite_replacements():
    """All held frames share one canvas so the hands never jump between frames.

    Offsets follow DOOM's psprite rule: the left edge lands at -leftoffset in
    320-wide space, so -(160 - W/2) centres it; and the stock pistol frames
    bottom-anchor by adding (h - 62) to a -106 topoffset, which we reuse.
    """
    # idle, then the two "recording" frames: a small lift and the REC lamp lit,
    # instead of the pistol's recoil
    specs = [('PISGA0', camera_frame(SPR_W, SPR_H, 0, False)),
             ('PISGB0', camera_frame(SPR_W, SPR_H, -3, True)),
             ('PISGC0', camera_frame(SPR_W, SPR_H, -1, True)),
             ('PISFA0', flash_frame(SPR_W, SPR_H, -3))]

    box = _union_bbox([g for _n, g in specs], SPR_W, SPR_H)
    cw, ch = box[2] - box[0], box[3] - box[1]
    xo = -(160 - cw // 2)              # centre it in 320-wide psprite space
    yo = -106 + (ch - 62)              # bottom-anchor, as the stock frames do

    frames = {}
    for name, g in specs:
        frames[name] = encode_picture(cw, ch, _crop(g, SPR_W, SPR_H, box), xo, yo, None)
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

# straight into public/, alongside the sidecars. Writing the WAD next to this
# script while screen.json/screens.bin went to public/ meant a rebuild updated
# one and not the other, and a stale WAD paired with fresh sidecars fails in
# the worst way: the signatures no longer match any composite in the heap, so
# every screen just stays static with no error anywhere.
out.save(os.path.join(OUT_DIR, OUT_WAD))
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
    'rooms': [{'key': rm['key'], 'name': rm['label'],
               'centre': [round(rm['c'][0]), round(rm['c'][1])]} for rm in ROOMS],
    # 'screen' is the wall that IS a video; 'flank' is a plain stub beside one,
    # which aims at the same video so the whole octagon side is one target.
    'walls': [
        {'a': list(VERTS[a]), 'b': list(VERTS[b]),
         'screen': (SCREEN_TEXS.index(t) if t in SCREEN_TEXS else None),
         'flank': f}
        for (a, b, t, f) in WALLS
    ],
    'note': 'composites are column-major: pixels[x*height + y] (src/r_patch.c:277)',
}, indent=1))
print('wrote %d screens, screens.bin (%d bytes), sig %d bytes each'
      % (len(screens), len(screens) * SCREEN_W * SCREEN_H, SIG_LEN))

# ---------------------------------------------------------------------------
# videos.json has to keep up with the map.
#
# A room that is NOT shuffled needs exactly one ID per wall. With fewer, a wall
# sits on static forever; with more, the surplus is silently ignored -- which
# is precisely how an eighth team message could be added and simply never
# appear, with nothing anywhere reporting a problem. A shuffled room is a pool
# and only needs enough to fill its walls.
# ---------------------------------------------------------------------------

VJSON = os.path.join(OUT_DIR, 'videos.json')
if os.path.exists(VJSON):
    _v = json.load(open(VJSON))
    _shuffled = set(_v.get('shuffle', []))
    for _i, _rm in enumerate(ROOMS):
        _walls = sum(1 for s in SCREENS if s['room'] == _i)
        _ids = _v.get('rooms', {}).get(_rm['key'], [])
        if _rm['key'] in _shuffled:
            assert len(_ids) >= _walls, (
                'videos.json: pool %r holds %d IDs but %r has %d walls'
                % (_rm['key'], len(_ids), _rm['name'], _walls))
        else:
            assert len(_ids) == _walls, (
                'videos.json: room %r lists %d IDs for %d walls -- '
                'add a wall (raise its "sides") or drop an ID'
                % (_rm['key'], len(_ids), _walls))
        assert len(set(_ids)) == len(_ids), 'videos.json: %r repeats an ID' % _rm['key']
        print('  %-6s %2d IDs / %d walls%s'
              % (_rm['key'], len(_ids), _walls,
                 ' (pool)' if _rm['key'] in _shuffled else ''))


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

    _specs = [('PISGA0', camera_frame(SPR_W, SPR_H, 0, False)),
              ('PISGB0', camera_frame(SPR_W, SPR_H, -3, True)),
              ('PISGC0', camera_frame(SPR_W, SPR_H, -1, True)),
              ('PISFA0', flash_frame(SPR_W, SPR_H, -3))]
    _box = _union_bbox([g for _n, g in _specs], SPR_W, SPR_H)
    _cw, _ch = _box[2] - _box[0], _box[3] - _box[1]
    for nm, g in _specs:
        preview(nm, _cw, _ch, _crop(g, SPR_W, SPR_H, _box))
    print('wrote preview_*.png')
