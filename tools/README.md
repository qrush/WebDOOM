# Tribute WAD tooling

Generates `public/tribute.wad` (the custom map, the video-screen texture and
the camcorder weapon sprites) plus the runtime sidecars the browser needs.

## Why these exist

The prebuilt engine in `public/` runs prboom's **software** renderer
(`src/m_misc.c:308-313` defaults `videomode` to `"8"` on non-MSVC builds), so
there is no WebGL texture to stream video into. Instead the page writes video
frames straight into the renderer's composited texture memory. That only works
if the texture's exact bytes are known ahead of time, which is what these
scripts produce.

## Requirements

`doom1.wad` must sit next to the scripts. It is **not** committed (it is id
Software's shareware IWAD); extract it from the packaged data instead:

    dd if=../public/doom1.data of=doom1.wad bs=1 skip=281020 count=4196020

The offsets come from the emscripten file-packager manifest embedded in
`public/doom1.js` (search for `"filename":"/doom1.wad"`).

## Build

    python3 build_tribute.py            # writes tribute.wad + sidecars
    python3 build_tribute.py --preview  # also renders preview_*.png sprites

Outputs:

| file                  | purpose                                                        |
|-----------------------|----------------------------------------------------------------|
| `tribute.wad`         | map, `WISTSCRN` texture, camcorder sprites (copy to `public/`)  |
| `public/screen.json`  | texture size, search signature, player spawn, screen position   |
| `public/screen.bin`   | the exact expected composite, for byte-for-byte verification    |
| `public/palette.lut`  | RGB555 -> DOOM palette index table for per-frame quantisation   |

## Gotchas worth remembering

- Sprite lumps must sit between `S_START`/`S_END`; prboom coalesces that range
  into the `ns_sprites` namespace (`src/w_wad.c:418`) and marker-less lumps are
  silently ignored.
- A PWAD's `PNAMES`/`TEXTURE1` **replace** the IWAD's, so both must re-emit
  every original entry.
- The composite is column-major: `pixels[x*height + y]` (`src/r_patch.c:277`).
- The search signature must span more than one texture column. One column is
  byte-identical to a run inside the raw lump (which is also in the heap), and
  matching that instead corrupts memory.
