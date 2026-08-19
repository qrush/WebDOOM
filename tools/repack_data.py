#!/usr/bin/env python3
"""
Build a slim data package for the tribute page.

public/doom1.data is 92MB, and 94% of that is music MP3s the engine never
manages to load anyway ("Cannot find preloaded audio /doom1/music/intro.mp3"
appears in its own startup log). The tribute only needs the two WADs, plus the
sound effects, which together come to about 5.5MB.

Emscripten's file package is just the kept files concatenated, with a manifest
of byte ranges embedded in the loader JS -- so repacking is: copy the byte
ranges we want, renumber them, and rewrite the manifest.

Outputs (originals are left untouched so the stock index.html still works):
    public/tribute.data        the slim package
    public/tribute-engine.js   doom1.js with the manifest rewritten

Run from the repo root:  python3 tools/repack_data.py
"""
import json, os, re, sys

PUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'public')
SRC_JS   = os.path.join(PUB, 'doom1.js')
SRC_DATA = os.path.join(PUB, 'doom1.data')
OUT_JS   = os.path.join(PUB, 'tribute-engine.js')
OUT_DATA = os.path.join(PUB, 'tribute.data')

# Files substituted on the way into the slim package. The camera weapon should
# not go bang: i_sound.c:185 loads effects as plain files ("sfx/ds<name>.wav"),
# and the pistol's sfx name is "pistol" (sounds.c:128), so overriding
# sfx/dspistol.wav is all it takes to turn the gunshot into a shutter click.
# Regenerate the replacement with tools/make_shutter.py.
OVERRIDES = {
    '/sfx/dspistol.wav': os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      'dspistol.wav'),
}

def keep(name):
    if name.endswith('.wad'):  return True     # prboom.wad + doom1.wad
    if '/sfx/' in name:        return True     # weapon/door sounds, ~1.2MB
    return False                               # music: 90MB, and unusable

js = open(SRC_JS).read()
m = re.search(r'"files":\s*(\[.*?\])\s*,\s*"remote_package_size":\s*(\d+)', js, re.S)
if not m:
    sys.exit('ERROR: could not find the file-package manifest in doom1.js')
files, old_size = json.loads(m.group(1)), int(m.group(2))

blob = open(SRC_DATA, 'rb').read()
if len(blob) != old_size:
    sys.exit('ERROR: doom1.data is %d bytes, manifest says %d' % (len(blob), old_size))

out, kept, cur, swapped = bytearray(), [], 0, []
for f in files:
    if not keep(f['filename']):
        continue
    sub = OVERRIDES.get(f['filename'])
    if sub and os.path.exists(sub):
        chunk = open(sub, 'rb').read()
        swapped.append((f['filename'], f['end'] - f['start'], len(chunk)))
    else:
        chunk = blob[f['start']:f['end']]
    kept.append({'start': cur, 'audio': f.get('audio', 0),
                 'end': cur + len(chunk), 'filename': f['filename']})
    out += chunk
    cur += len(chunk)

if not any(k['filename'].endswith('doom1.wad') for k in kept):
    sys.exit('ERROR: doom1.wad missing from the repack')

open(OUT_DATA, 'wb').write(bytes(out))

new_js = (js[:m.start(1)] + json.dumps(kept, separators=(',', ':')) +
          ',"remote_package_size":' + str(len(out)) + js[m.end(2):])
# point the loader at the slim package instead of the original
n = new_js.count('"doom1.data"')
new_js = new_js.replace('"doom1.data"', '"tribute.data"')
open(OUT_JS, 'w').write(new_js)

assert '"tribute.data"' in new_js and n >= 1, 'failed to retarget the package name'
assert 'doom1.wasm' in new_js, 'lost the wasm reference'

for (nm, was, now) in swapped:
    print('substituted %s: %d -> %d bytes' % (nm, was, now))
print('kept %d of %d files' % (len(kept), len(files)))
for k in kept[:4]:
    print('   %-28s %9d bytes' % (k['filename'], k['end'] - k['start']))
if len(kept) > 4:
    print('   ... and %d more (sfx)' % (len(kept) - 4))
print()
print('doom1.data   %10d bytes  (%.1f MB)' % (old_size, old_size / 1e6))
print('tribute.data %10d bytes  (%.1f MB)  -- %.1f%% smaller'
      % (len(out), len(out) / 1e6, 100 * (1 - len(out) / old_size)))
print('wrote', os.path.relpath(OUT_DATA), 'and', os.path.relpath(OUT_JS))
