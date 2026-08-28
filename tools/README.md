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

Two octagonal rooms joined by a hallway running north-east. Every floor and
ceiling is at the same height, so the whole complex is a **single sector**
whose boundary is one closed loop -- walk the main room clockwise and, at the
doorway, detour out along the hallway, around the branch room, and back. Every
linedef stays one-sided and there are no two-sided lines to get wrong.

Screens: one doorway per room leaves 7 of its 8 walls free, so it is exactly
7 + 7 = 14. Adding a second doorway (the earlier three-room triangle) costs the
main room a screen, which is why that version could only be 6/7/7.

`public/videos.json` groups IDs under a **room key** (`lenny`, `team`), not by
screen index, and hands them out in wall order. Screen numbering interleaves
across rooms -- the main room owns `WISTSC00..05` *and* `WISTSC13`, because its
last wall is emitted after the whole branch loop -- so a flat index-keyed list
is a trap. The keys and labels live in `ROOM_META` in `build_tribute.py` and
travel to the runtime through `screen.json`.

## bsp.py

The single-room map shipped **zero** BSP nodes, which prboom reads as "one
subsector, draw everything" (`src/r_main.c:466`). That is only correct while
the map is convex. Rooms joined by a hallway are not, so `bsp.py` builds a
real tree: pick a partition, split straddling segs, recurse, and emit nodes
post-order so the root lands last (the renderer starts at `numnodes-1`).

It is validated to leave convex shapes alone -- a square or a lone octagon
still produces 0 nodes and 1 subsector, exactly matching the old output.

## Sounds

This build loads sound effects as plain files rather than WAD lumps:
`i_sound.c:185` builds `"sfx/ds<name>.wav"` and hands it to `Mix_LoadWAV`. The
pistol's sfx name is `"pistol"` (`sounds.c:128`), so the gunshot is just
`sfx/dspistol.wav`.

`make_shutter.py` builds the replacement from `tools/shutter-source.mp3`, a
real SLR shutter recording. The raw file cannot be dropped in as-is: it opens
with ~160ms of silence, which in a game reads as input lag between pulling the
trigger and hearing anything, and trails off with ~370ms of near-silence. The
script decodes it, trims to the part that actually makes noise, normalises,
fades the edges so the trim cannot pop, and emits the exact format the original
used -- mono, 11025 Hz, 8-bit unsigned PCM (0.38s, against the gunshot's 0.51s).

Needs `ffmpeg` for the MP3 decode; everything after that is deterministic.

The source file is committed (forced past the repo's `*.mp3` ignore rule) so
the build is reproducible. It came from Freesound -- **check the licence and
attribution before publishing this anywhere public.**

`repack_data.py` substitutes it into the slim package via its `OVERRIDES` map,
so no engine or WAD change is involved.

    python3 tools/make_shutter.py     # regenerate the click
    python3 tools/repack_data.py      # rebuild public/tribute.data

Note `dspistol.wav` is **not** committed -- the repo's `.gitignore` excludes
`*.wav`. It is a generated artifact: run `make_shutter.py` to recreate it from
the committed source before `repack_data.py`, which errors out rather than
silently falling back to the original gunshot.

## Weapon sprite

`build_tribute.py` draws a first-person camcorder held in both hands, replacing
the pistol frames (`PISGA0/B0/C0`, plus `PISFA0` as a fullbright overlay).
Rear view: eyepiece cup up on the left, ribbed battery pack across the back,
red REC lamp and lens barrel falling away to the right. `--preview` renders
the frames to `preview_*.png`.

Frames are drawn on one 150x95 canvas and then auto-cropped to the tightest
box containing artwork from **all** of them. Cropping each frame to its own
bounds would let the recording frames -- which sit a few pixels higher --
shift relative to the idle one, so the camcorder would visibly jump when you
pull the trigger.

Placement follows DOOM's psprite rule: a sprite's left edge lands at
`-leftoffset` in 320-wide space, so `-(160 - W/2)` centres it, and the stock
pistol frames bottom-anchor by adding `(h - 62)` to a `-106` topoffset.

The REC lamp sits left of the battery pack rather than on the right where a
real camcorder puts it, so nothing overlaps the recording indicator.
