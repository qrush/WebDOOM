#!/usr/bin/env python3
"""
Synthesise a camera shutter click to replace DOOM's pistol shot.

This build loads sound effects as plain files, not WAD lumps: i_sound.c:185
builds the path "sfx/ds<name>.wav" and hands it to Mix_LoadWAV. The pistol's
sfx name is "pistol" (sounds.c:128), so the gunshot is simply
sfx/dspistol.wav -- swapping that file is the whole job, no engine or WAD
change required.

Output matches the original exactly: mono, 11025 Hz, 8-bit unsigned PCM.

An SLR shutter is two mechanical events, not one: the mirror flips up and the
first curtain opens, then a beat later the second curtain closes and the mirror
drops back. One transient sounds like a tap; two sound like a camera.
"""
import math, os, struct

RATE = 11025
OUT  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dspistol.wav')

def lcg(seed):
    s = seed & 0xFFFFFFFF
    while True:
        s = (1103515245 * s + 12345) & 0xFFFFFFFF
        yield (s >> 8) / 8388608.0 - 1.0        # -1..1

def add_click(buf, at, dur, decay, tones, noise_amp, amp, seed):
    """One mechanical transient: filtered noise plus damped metallic tones."""
    rnd = lcg(seed)
    n = int(dur * RATE)
    prev = 0.0
    for i in range(n):
        idx = at + i
        if idx >= len(buf):
            break
        t = i / float(RATE)
        env = math.exp(-t / decay)
        # noise, lightly low-passed so it reads as a mechanical clack
        # rather than a hiss
        white = next(rnd)
        prev = prev * 0.55 + white * 0.45
        v = prev * noise_amp
        for (f, a, d) in tones:
            v += a * math.sin(2 * math.pi * f * t) * math.exp(-t / d)
        buf[idx] += v * env * amp

total = int(0.16 * RATE)
buf = [0.0] * total

# 1: mirror up + first curtain -- bright, sharp, very short
add_click(buf, 0, 0.045, 0.011,
          [(2600, 0.45, 0.008), (3900, 0.30, 0.005), (1500, 0.25, 0.014)],
          noise_amp=0.85, amp=1.00, seed=0x5EED01)

# 2: second curtain + mirror down, ~55ms later -- duller and a touch softer
add_click(buf, int(0.055 * RATE), 0.070, 0.020,
          [(1700, 0.42, 0.016), (2400, 0.26, 0.010), (900, 0.30, 0.022)],
          noise_amp=0.70, amp=0.80, seed=0xC0FFEE)

peak = max(abs(v) for v in buf) or 1.0
buf = [v / peak * 0.92 for v in buf]            # leave a little headroom

# 8-bit unsigned PCM, centred on 128
pcm = bytes(max(0, min(255, int(round(v * 127.0)) + 128)) for v in buf)

data = (b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVE' +
        b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, RATE, RATE, 1, 8) +
        b'data' + struct.pack('<I', len(pcm)) + pcm)
open(OUT, 'wb').write(data)

print('wrote %s' % os.path.relpath(OUT))
print('  %d samples, %.3f s, mono 11025 Hz 8-bit' % (len(pcm), len(pcm) / float(RATE)))
# quick envelope readout so the two transients are visibly distinct
step = len(pcm) // 16
for i in range(0, len(pcm) - step, step):
    chunk = pcm[i:i+step]
    lvl = max(abs(c - 128) for c in chunk) / 127.0
    print('   %5.0f ms |%s' % (1000.0 * i / RATE, '#' * int(lvl * 40)))
