#!/usr/bin/env python3
"""
Build the camera-shutter sound that replaces DOOM's pistol shot.

This build loads sound effects as plain files, not WAD lumps: i_sound.c:185
builds the path "sfx/ds<name>.wav" and hands it to Mix_LoadWAV. The pistol's
sfx name is "pistol" (sounds.c:128), so the gunshot is simply
sfx/dspistol.wav -- replacing that file is the whole job, no engine or WAD
change required. repack_data.py substitutes the result into tribute.data.

Source: tools/shutter-source.mp3 (a real SLR shutter recording).

The raw file cannot be used as-is. It opens with ~150ms of silence, which in
a game reads as input lag between pulling the trigger and hearing anything,
and it trails off with ~400ms of near-silence. So: decode, trim to the part
that actually makes noise, normalise, fade the edges so the trim does not
click, and emit the exact format the original used -- mono, 11025 Hz, 8-bit
unsigned PCM.

Requires ffmpeg for the MP3 decode; everything after that is deterministic.
"""
import os, struct, subprocess, sys, tempfile

HERE   = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, 'shutter-source.mp3')
OUT    = os.path.join(HERE, 'dspistol.wav')

RATE      = 11025      # match the sound it replaces
THRESH    = 0.02       # of peak: below this counts as silence
PRE_ROLL  = 0.004      # keep a hair before the first transient
TAIL      = 0.030      # keep a little decay after the last one
FADE_IN   = 0.002
FADE_OUT  = 0.020
HEADROOM  = 0.94

if not os.path.exists(SOURCE):
    sys.exit('ERROR: missing %s' % os.path.relpath(SOURCE))

tmp = os.path.join(tempfile.gettempdir(), '_shutter_decode.wav')
subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', SOURCE,
                '-ac', '1', '-ar', str(RATE), '-acodec', 'pcm_s16le', tmp],
               check=True)

raw = open(tmp, 'rb').read()
pos, data = 12, None
while pos < len(raw):
    cid = raw[pos:pos+4]; sz = struct.unpack_from('<I', raw, pos+4)[0]
    if cid == b'data':
        data = raw[pos+8:pos+8+sz]
    pos += 8 + sz
n = len(data) // 2
s = list(struct.unpack('<%dh' % n, data[:n*2]))

peak = max(abs(v) for v in s) or 1
lim = peak * THRESH
first = next((i for i, v in enumerate(s) if abs(v) > lim), 0)
last  = next((i for i in range(n - 1, -1, -1) if abs(s[i]) > lim), n - 1)
start = max(0, first - int(PRE_ROLL * RATE))
end   = min(n, last + int(TAIL * RATE))
clip  = s[start:end]

# normalise, then fade both edges so trimming cannot introduce a pop
m = max(abs(v) for v in clip) or 1
fi, fo = int(FADE_IN * RATE), int(FADE_OUT * RATE)
out = []
for i, v in enumerate(clip):
    g = v / float(m) * HEADROOM
    if i < fi:
        g *= i / float(fi)
    if i >= len(clip) - fo:
        g *= (len(clip) - i) / float(fo)
    out.append(g)

pcm = bytes(max(0, min(255, int(round(v * 127.0)) + 128)) for v in out)
wav = (b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVE' +
       b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, RATE, RATE, 1, 8) +
       b'data' + struct.pack('<I', len(pcm)) + pcm)
open(OUT, 'wb').write(wav)

print('source %s: %.3fs' % (os.path.basename(SOURCE), n / float(RATE)))
print('trimmed %.0fms of leading silence, %.0fms of tail'
      % (1000.0 * start / RATE, 1000.0 * (n - end) / RATE))
print('wrote %s: %d samples, %.3fs, mono %dHz 8-bit'
      % (os.path.relpath(OUT), len(pcm), len(pcm) / float(RATE), RATE))
step = max(1, len(pcm) // 20)
for i in range(0, len(pcm) - step, step):
    lvl = max(abs(c - 128) for c in pcm[i:i+step]) / 127.0
    print('   %5.0f ms |%s' % (1000.0 * i / RATE, '#' * int(lvl * 44)))
