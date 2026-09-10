# vimview

An SDL2 image viewer with an endless pannable/zoomable canvas and vim-like
keyboard shortcuts. Point it at a directory of images and browse them clustered
by date or by filename.

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Needs the SDL2 system library (`libSDL2`) — Pillow decodes the images, so
`SDL2_image` is not required.

## Run

```sh
./run.sh ~/Pictures
./run.sh ~/Pictures --thumb 192
```

Headless smoke test (no window needed):

```sh
SDL_VIDEODRIVER=dummy ./run.sh ~/Pictures --selftest
```

## Keys

| Key | Action |
|---|---|
| `h j k l` / arrows | move selection |
| `0` / `$` | first / last image in row |
| `gg` / `G` | first / last image |
| `Ctrl+U` / `Ctrl+D` | pan half screen (selection stays) |
| mouse drag / wheel | pan / zoom |
| `+` / `-` | zoom in / out, `zz` centers on selection |
| `o` | cycle cluster order: date → filename |
| `/` then text, `Enter` | filter by filename (`Esc` cancels) |
| `:` | command line — `:q` quit, `:o date\|filename`, `:h` help |
| `Enter` | fullscreen preview (`Esc`/`Enter`/`q` to leave) |
| `R` | rescan directory |
| `?` | help overlay |
| `q` | quit (`Esc` also clears the active filter) |

In **date** order, images are clustered under per-day headers; in **filename**
order they flow in one alphabetical grid. The status line shows the order,
position, filename, filter, and zoom.

## Notes

- Thumbnails are decoded lazily with Pillow and cached (LRU, 500 entries), so
  large directories open instantly and scroll smoothly.
- Only visible cells are drawn each frame; far-off textures are evicted.
