"""Minimal DOOM WAD toolkit: read/write WADs, DOOM picture format, palette match."""
import struct

class Wad:
    def __init__(self, path=None):
        self.lumps = []            # list of (name, bytes)
        self.sig = b'PWAD'
        if path:
            self.load(path)

    def load(self, path):
        d = open(path, 'rb').read()
        self.sig, n, off = struct.unpack('<4sii', d[:12])
        for i in range(n):
            lo, ln, nm = struct.unpack('<ii8s', d[off + i*16: off + i*16 + 16])
            self.lumps.append((nm.rstrip(b'\0').decode('ascii', 'replace'), d[lo:lo+ln]))

    def get(self, name, start=0):
        for i in range(start, len(self.lumps)):
            if self.lumps[i][0] == name:
                return self.lumps[i][1]
        raise KeyError(name)

    def index(self, name, start=0):
        for i in range(start, len(self.lumps)):
            if self.lumps[i][0] == name:
                return i
        raise KeyError(name)

    def save(self, path, sig=b'PWAD'):
        body, dirents, off = b'', b'', 12
        for nm, data in self.lumps:
            dirents += struct.pack('<ii8s', off, len(data), nm.encode('ascii')[:8].ljust(8, b'\0'))
            body += data
            off += len(data)
        with open(path, 'wb') as f:
            f.write(struct.pack('<4sii', sig, len(self.lumps), 12 + len(body)))
            f.write(body)
            f.write(dirents)


def palette(wad):
    """First 256-colour PLAYPAL palette as a list of (r,g,b)."""
    p = wad.get('PLAYPAL')
    return [tuple(p[i*3:i*3+3]) for i in range(256)]


def build_matcher(pal, avoid=(247,)):
    """Nearest-palette-index lookup with a 5-bit-per-channel cache.

    `avoid` skips indices that DOOM treats specially (247 is the transparency
    key used when encoding pictures below)."""
    cache = {}
    usable = [i for i in range(256) if i not in avoid]
    def match(r, g, b):
        key = (r >> 3, g >> 3, b >> 3)
        hit = cache.get(key)
        if hit is not None:
            return hit
        rr, gg, bb = (r >> 3) * 8 + 4, (g >> 3) * 8 + 4, (b >> 3) * 8 + 4
        best, bestd = 0, 1 << 30
        for i in usable:
            pr, pg, pb = pal[i]
            # weighted euclidean: green matters most perceptually
            d = 3*(pr-rr)**2 + 6*(pg-gg)**2 + (pb-bb)**2
            if d < bestd:
                best, bestd = i, d
        cache[key] = best
        return best
    return match


def encode_picture(width, height, pixels, xoff=0, yoff=0, transparent=None):
    """Encode indexed pixels (row-major list of ints, `transparent` = None pixel)
    into DOOM's column-based picture format."""
    header = struct.pack('<hhhh', width, height, xoff, yoff)
    colofs, coldata = [], b''
    base = 8 + 4 * width
    for x in range(width):
        colofs.append(base + len(coldata))
        post = b''
        y = 0
        while y < height:
            # skip transparent run
            while y < height and pixels[y*width + x] == transparent:
                y += 1
            if y >= height:
                break
            start = y
            run = []
            while y < height and pixels[y*width + x] != transparent and len(run) < 128:
                run.append(pixels[y*width + x])
                y += 1
            # topdelta, length, pad, data..., pad
            post += bytes([min(start, 254), len(run), 0]) + bytes(run) + b'\0'
        post += b'\xff'
        coldata += post
    return header + struct.pack('<%dI' % width, *colofs) + coldata


def decode_picture(data):
    """Decode a DOOM picture -> (w, h, xoff, yoff, pixels) with None for transparent."""
    w, h, xo, yo = struct.unpack('<hhhh', data[:8])
    offs = struct.unpack('<%dI' % w, data[8:8+4*w])
    px = [None] * (w * h)
    for x in range(w):
        p = offs[x]
        while True:
            td = data[p]
            if td == 0xFF:
                break
            ln = data[p+1]
            p += 3
            for i in range(ln):
                y = td + i
                if 0 <= y < h:
                    px[y*w + x] = data[p+i]
            p += ln + 1
    return w, h, xo, yo, px
