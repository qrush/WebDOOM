"""
A minimal DOOM BSP node builder.

The single-octagon map got away with shipping zero NODES: prboom special-cases
numnodes==0 to mean "one subsector, draw everything" (src/r_main.c:466), which
renders correctly only because a convex room's walls can never occlude each
other. The moment the map has more than one room joined by hallways it is
non-convex, segs need front-to-back ordering, and a real tree is required.

Build order per node:
  1. if the seg set is convex, emit a subsector (leaf)
  2. otherwise pick a partition line, split any seg that straddles it,
     recurse front then back, then emit the node

Nodes are emitted post-order so the root ends up last, which is what the
renderer assumes when it starts at numnodes-1.
"""
import math, struct

NF_SUBSECTOR = 0x8000
EPS = 1e-6


class Seg:
    __slots__ = ('x1', 'y1', 'x2', 'y2', 'linedef', 'side', 'offset')

    def __init__(self, x1, y1, x2, y2, linedef, side=0, offset=0):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.linedef, self.side, self.offset = linedef, side, offset

    def length(self):
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)


def _side_of(px, py, dx, dy, x, y):
    """>0 front (left of the direction vector), <0 back, ~0 on the line."""
    return (x - px) * dy - (y - py) * dx


def _classify(seg, px, py, dx, dy):
    a = _side_of(px, py, dx, dy, seg.x1, seg.y1)
    b = _side_of(px, py, dx, dy, seg.x2, seg.y2)
    if abs(a) < EPS: a = 0.0
    if abs(b) < EPS: b = 0.0
    if a == 0 and b == 0:
        return 'collinear', a, b
    if a >= 0 and b >= 0:
        return 'front', a, b
    if a <= 0 and b <= 0:
        return 'back', a, b
    return 'split', a, b


def _split_seg(seg, px, py, dx, dy, a, b):
    """Cut seg at the partition, returning (front_part, back_part)."""
    t = a / (a - b)
    mx = seg.x1 + (seg.x2 - seg.x1) * t
    my = seg.y1 + (seg.y2 - seg.y1) * t
    first = Seg(seg.x1, seg.y1, mx, my, seg.linedef, seg.side, seg.offset)
    second = Seg(mx, my, seg.x2, seg.y2, seg.linedef, seg.side,
                 seg.offset + int(round(math.hypot(mx - seg.x1, my - seg.y1))))
    return (first, second) if a > 0 else (second, first)


def _is_convex(segs):
    """Convex when no seg has any other seg's endpoint behind it."""
    for s in segs:
        dx, dy = s.x2 - s.x1, s.y2 - s.y1
        for o in segs:
            if o is s:
                continue
            for (x, y) in ((o.x1, o.y1), (o.x2, o.y2)):
                if _side_of(s.x1, s.y1, dx, dy, x, y) < -EPS:
                    return False
    return True


def _pick_partition(segs):
    """Cheapest split: fewest cuts, then best front/back balance.

    Axis-aligned candidates are tried first -- they split far less in
    rectilinear geometry, and hallways meeting rooms are exactly that."""
    best, best_score = None, None
    axis = [s for s in segs if abs(s.x2 - s.x1) < EPS or abs(s.y2 - s.y1) < EPS]
    for cand in (axis or segs):
        dx, dy = cand.x2 - cand.x1, cand.y2 - cand.y1
        if abs(dx) < EPS and abs(dy) < EPS:
            continue
        f = b = x = 0
        for s in segs:
            kind, _, _ = _classify(s, cand.x1, cand.y1, dx, dy)
            if kind == 'split': x += 1
            elif kind == 'front': f += 1
            elif kind == 'back': b += 1
            else: f += 1
        if f + x == 0 or b + x == 0:
            continue                      # useless: everything on one side
        score = (x * 8) + abs(f - b)
        if best_score is None or score < best_score:
            best, best_score = cand, score
    return best


def _bbox(segs):
    xs = [c for s in segs for c in (s.x1, s.x2)]
    ys = [c for s in segs for c in (s.y1, s.y2)]
    # DOOM bbox order is top, bottom, left, right
    return [int(math.ceil(max(ys))), int(math.floor(min(ys))),
            int(math.floor(min(xs))), int(math.ceil(max(xs)))]


class BSP:
    def __init__(self, segs):
        self.out_segs = []       # final, in subsector order
        self.subsectors = []     # (numsegs, firstseg)
        self.nodes = []          # dicts, post-order (root last)
        self._build(list(segs))

    def _leaf(self, segs):
        first = len(self.out_segs)
        self.out_segs.extend(segs)
        self.subsectors.append((len(segs), first))
        return (len(self.subsectors) - 1) | NF_SUBSECTOR

    def _build(self, segs, depth=0):
        if not segs:
            raise ValueError('empty seg set')
        if len(segs) <= 2 or _is_convex(segs) or depth > 64:
            return self._leaf(segs)

        part = _pick_partition(segs)
        if part is None:
            return self._leaf(segs)

        px, py = part.x1, part.y1
        dx, dy = part.x2 - part.x1, part.y2 - part.y1

        front, back = [], []
        for s in segs:
            kind, a, b = _classify(s, px, py, dx, dy)
            if kind == 'split':
                f, bk = _split_seg(s, px, py, dx, dy, a, b)
                front.append(f); back.append(bk)
            elif kind == 'front':
                front.append(s)
            elif kind == 'back':
                back.append(s)
            else:
                # collinear: keep with the side it faces
                (front if (s.x2 - s.x1) * dx + (s.y2 - s.y1) * dy > 0 else back).append(s)

        if not front or not back:
            return self._leaf(segs)

        fbox, bbox_ = _bbox(front), _bbox(back)
        fchild = self._build(front, depth + 1)
        bchild = self._build(back, depth + 1)
        self.nodes.append({'x': int(round(px)), 'y': int(round(py)),
                           'dx': int(round(dx)), 'dy': int(round(dy)),
                           'fbox': fbox, 'bbox': bbox_,
                           'children': (fchild, bchild)})
        return len(self.nodes) - 1

    # ---- lump encoders ----------------------------------------------------

    def encode(self, vert_index):
        """vert_index(x, y) -> vertex number, adding new split vertices."""
        segs = b''
        for s in self.out_segs:
            v1 = vert_index(s.x1, s.y1)
            v2 = vert_index(s.x2, s.y2)
            ang = int(round(math.atan2(s.y2 - s.y1, s.x2 - s.x1) * 65536.0 / (2 * math.pi))) & 0xFFFF
            if ang >= 0x8000:
                ang -= 0x10000
            segs += struct.pack('<HHhHhh', v1, v2, ang, s.linedef, s.side, s.offset)

        ssectors = b''.join(struct.pack('<HH', n, f) for (n, f) in self.subsectors)

        nodes = b''
        for nd in self.nodes:
            nodes += struct.pack('<hhhh', nd['x'], nd['y'], nd['dx'], nd['dy'])
            nodes += struct.pack('<hhhh', *nd['fbox'])
            nodes += struct.pack('<hhhh', *nd['bbox'])
            nodes += struct.pack('<HH', nd['children'][0], nd['children'][1])
        return segs, ssectors, nodes
