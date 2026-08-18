import sys, struct, math, json, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wad import Wad, palette, build_matcher, encode_picture

SRC_WAD    = 'doom1.wad'
OUT_WAD    = 'tribute.wad'
OUT_DIR    = '/Users/qrush/wistia/WebDOOM/public'

WALL_TEX   = 'METAL1'      # ordinary walls (64x128)
SCREEN_TEX = 'WISTSCRN'    # our own texture, defined below
SCREEN_W, SCREEN_H = 256, 128   # 2:1 -- close to the video's 16:9
FLOOR_FLAT = 'FLOOR4_8'
CEIL_FLAT  = 'CEIL3_5'
ROOM_H     = SCREEN_H      # wall height == texture height -> exactly one tile

HERE = os.path.dirname(os.path.abspath(__file__))
src = Wad(os.path.join(HERE, SRC_WAD))
pal = palette(src)
match = build_matcher(pal)

# ---------------------------------------------------------------------------
# geometry: one room, north wall split flank / screen / flank so the screen
# segment is exactly SCREEN_W map units wide and ROOM_H tall -- one clean
# texture tile, no seams and no stretching.
#
#     v1 -------- v2 ===== SCREEN ===== v3 -------- v4
#     |                                              |
#     |                  room                        |
#     v0 ------------------------------------------ v5
# ---------------------------------------------------------------------------

HALF = SCREEN_W // 2                      # 128

# Deliberately odd spawn coordinates: the runtime finds the player's mobj_t by
# scanning the heap for its fixed_t (x, y, z) triple at map start, and round
# numbers like 0/-64 collide with unrelated data far too often.
PLAYER_X, PLAYER_Y = -151, -233
VERTS = [
    (-HALF - 96, -288),   # v0 SW
    (-HALF - 96,  128),   # v1 NW
    (-HALF,       128),   # v2 screen-left
    ( HALF,       128),   # v3 screen-right
    ( HALF + 96,  128),   # v4 NE
    ( HALF + 96, -288),   # v5 SE
]

WALLS = [                                  # (v_from, v_to, texture)
    (0, 1, WALL_TEX),
    (1, 2, WALL_TEX),
    (2, 3, SCREEN_TEX),                    # <-- the video screen
    (3, 4, WALL_TEX),
    (4, 5, WALL_TEX),
    (5, 0, WALL_TEX),
]

THINGS = [
    # x, y, angle, type, flags (7 = all skills)
    (PLAYER_X, PLAYER_Y, 90, 1, 7),   # player 1 start, facing the screen
    (-HALF - 40, -250, 0, 48, 7),   # techno pillar (decor)
    ( HALF + 40, -250, 0, 48, 7),   # techno pillar (decor)
]

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

def enc_segs():
    out = b''
    for i, (a, b, _t) in enumerate(WALLS):
        x0, y0 = VERTS[a]; x1, y1 = VERTS[b]
        ang = int(round(math.atan2(y1 - y0, x1 - x0) * 65536.0 / (2 * math.pi))) & 0xFFFF
        if ang >= 0x8000: ang -= 0x10000
        out += struct.pack('<HHhHhh', a, b, ang, i, 0, 0)
    return out

def enc_ssectors():
    return struct.pack('<HH', len(WALLS), 0)

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

def static_pixels():
    """Deterministic LCG static. Row-major list, len = SCREEN_W*SCREEN_H."""
    px = [0] * (SCREEN_W * SCREEN_H)
    s = 0x1BADB002
    ramp = GREYS[2:len(GREYS) // 2]        # darker half -> an idle screen
    for y in range(SCREEN_H):
        for x in range(SCREEN_W):
            s = (1103515245 * s + 12345) & 0xFFFFFFFF
            px[y * SCREEN_W + x] = ramp[(s >> 16) % len(ramp)]
    return px

SCREEN_PIXELS = static_pixels()

def composite_bytes(px):
    """The exact bytes prboom will hold in rpatch_t.pixels: column-major."""
    out = bytearray(SCREEN_W * SCREEN_H)
    for x in range(SCREEN_W):
        base = x * SCREEN_H
        for y in range(SCREEN_H):
            out[base + y] = px[y * SCREEN_W + x]
    return bytes(out)

COMPOSITE = composite_bytes(SCREEN_PIXELS)

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
    names.append(SCREEN_TEX.encode('ascii').ljust(8, b'\0'))
    return struct.pack('<i', len(names)) + b''.join(names), len(names) - 1

def build_texture1(new_patch_index):
    t = src.get('TEXTURE1')
    n = struct.unpack('<i', t[:4])[0]
    offs = struct.unpack('<%di' % n, t[4:4 + 4 * n])
    # slice each original texture record out verbatim
    records = []
    for i, o in enumerate(offs):
        npatch = struct.unpack('<h', t[o + 20:o + 22])[0]
        end = o + 22 + npatch * 10
        records.append(t[o:end])
    # our texture: one patch, full size, at origin
    rec = struct.pack('<8sihhih', SCREEN_TEX.encode('ascii').ljust(8, b'\0'),
                      0, SCREEN_W, SCREEN_H, 0, 1)
    rec += struct.pack('<hhhhh', 0, 0, new_patch_index, 1, 0)
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
out.lumps.append(('VERTEXES', enc_vertexes()))
out.lumps.append(('SEGS', enc_segs()))
out.lumps.append(('SSECTORS', enc_ssectors()))
out.lumps.append(('NODES', b''))      # 0 nodes -> prboom trivial-map path (r_main.c:466)
out.lumps.append(('SECTORS', enc_sectors()))
out.lumps.append(('REJECT', b''))     # auto-padded
out.lumps.append(('BLOCKMAP', b''))   # <8 bytes -> P_CreateBlockMap()

out.lumps.append(('PNAMES', pnames))
out.lumps.append(('TEXTURE1', build_texture1(patch_idx)))
out.lumps.append((SCREEN_TEX, encode_picture(SCREEN_W, SCREEN_H, SCREEN_PIXELS, 0, 0, None)))

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
SIG_LEN = SCREEN_H * 3
sig = list(COMPOSITE[:SIG_LEN])

# RGB555 -> palette-index lookup table, so per-frame quantisation is O(1)
LUT_PATH = os.path.join(OUT_DIR, 'palette.lut')
if os.path.exists(LUT_PATH) and len(open(LUT_PATH,'rb').read()) == 32768:
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

open(os.path.join(OUT_DIR, 'palette.lut'), 'wb').write(bytes(lut))
# the FULL expected composite, so the runtime can verify a candidate address
# byte-for-byte instead of trusting a short signature
open(os.path.join(OUT_DIR, 'screen.bin'), 'wb').write(COMPOSITE)
open(os.path.join(OUT_DIR, 'screen.json'), 'w').write(json.dumps({
    'width': SCREEN_W, 'height': SCREEN_H,
    'bufferLength': SCREEN_W * SCREEN_H,
    'signature': sig,
    'note': 'composite is column-major: pixels[x*height + y] (src/r_patch.c:277)',
    # --- proximity audio ---
    # DOOM stores positions as fixed_t (16.16). The player's mobj holds
    # x, y, z as three consecutive int32s, so the spawn triple is the search
    # key; z at spawn is the floor height (0).
    'player': {
        'spawn':  [PLAYER_X << 16, PLAYER_Y << 16, 0],
        'bounds': [min(v[0] for v in VERTS) << 16, min(v[1] for v in VERTS) << 16,
                   max(v[0] for v in VERTS) << 16, max(v[1] for v in VERTS) << 16],
    },
    # centre of the screen wall, in map units -- distance is measured to this
    'screenCentre': [0, 128],
    'fracBits': 16,
}))
print('wrote palette.lut (%d bytes) and screen.json (sig %d bytes)' % (len(lut), SIG_LEN))

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
