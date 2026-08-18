#!/usr/bin/env python3
"""
Generate doom1gl.js from doom1.js.

The prebuilt engine ships with emscripten's LEGACY_GL_EMULATION glue, but a
few legacy fixed-function entry points are unimplemented stubs that *throw*:

    function _glTexGenfv(){throw"glTexGenfv: TODO"}

prboom's gld_Init calls glTexGenfv when setting up sky texture-coordinate
generation (src/gl_main.c ~line 380). The original author already commented
out the neighbouring glTexGenf calls for exactly this reason but left the
glTexGenfv ones in, so enabling the GL renderer kills the engine at startup.

We can't recompile the wasm, but these stubs live in the JS glue, so we
rewrite them into counting no-ops. Sky texgen is the only casualty, and the
tribute map is a fully enclosed room with no sky.

Any *other* TODO-throwing gl* stub gets the same treatment, and each call is
tallied on window.__glTodo so unimplemented calls stay visible instead of
silently corrupting rendering.
"""
import re, sys

SRC, DST = 'doom1.js', 'doom1gl.js'
src = open(SRC).read()

# function _glNAME(...){throw"...: TODO"}
pat = re.compile(r'function (_gl[A-Za-z0-9_]+)\(([^)]*)\)\{throw"[^"]*TODO"\}')

patched = []
def repl(m):
    name, args = m.group(1), m.group(2)
    patched.append(name)
    return ('function %s(%s){var t=(self.__glTodo=self.__glTodo||{});'
            't["%s"]=(t["%s"]||0)+1;}' % (name, args, name, name))

out, n = pat.subn(repl, src)
if not patched:
    sys.exit('ERROR: no TODO-throwing gl stubs found -- did doom1.js change?')

open(DST, 'w').write(out)
print('patched %d stub(s): %s' % (n, ', '.join(patched)))
print('%s -> %s (%d bytes)' % (SRC, DST, len(out)))

# the patched file must still reference the same data/wasm payloads
for asset in ('doom1.data', 'doom1.wasm'):
    assert asset in out, 'lost reference to ' + asset
print('asset references intact: doom1.data, doom1.wasm')
