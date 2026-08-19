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

## Map layout

Three octagonal rooms in a triangle: a main room with a hallway branching to
each of the other two. Every floor and ceiling is at the same height, so the
whole complex is a **single sector** whose boundary is one closed loop -- walk
the main room clockwise and, at each doorway, detour out along the hallway,
around the branch room, and back. Every linedef stays one-sided and there are
no two-sided lines to get wrong.

Screens: 6 in the main room (two of its eight walls are doorways) and 7 in each
branch room = 20. `public/videos.json` maps one Wistia hashed-ID per screen in
`WISTSC00..WISTSC19` order.

## bsp.py

The single-room map shipped **zero** BSP nodes, which prboom reads as "one
subsector, draw everything" (`src/r_main.c:466`). That is only correct while
the map is convex. Three rooms joined by hallways are not, so `bsp.py` builds a
real tree: pick a partition, split straddling segs, recurse, and emit nodes
post-order so the root lands last (the renderer starts at `numnodes-1`).

It is validated to leave convex shapes alone -- a square or a lone octagon
still produces 0 nodes and 1 subsector, exactly matching the old output.

## Sounds

This build loads sound effects as plain files rather than WAD lumps:
`i_sound.c:185` builds `"sfx/ds<name>.wav"` and hands it to `Mix_LoadWAV`. The
pistol's sfx name is `"pistol"` (`sounds.c:128`), so the gunshot is just
`sfx/dspistol.wav`.

`make_shutter.py` synthesises a camera shutter to replace it -- two mechanical
transients (mirror up / first curtain, then second curtain / mirror down about
55ms later), because a single transient reads as a tap rather than a camera.
Output matches the original exactly: mono, 11025 Hz, 8-bit unsigned PCM.

`repack_data.py` substitutes it into the slim package via its `OVERRIDES` map,
so no engine or WAD change is involved.

    python3 tools/make_shutter.py     # regenerate the click
    python3 tools/repack_data.py      # rebuild public/tribute.data
