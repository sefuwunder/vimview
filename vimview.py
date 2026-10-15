#!/usr/bin/env python3
"""vimview — an SDL2 image viewer with an endless canvas and vim-like keys.

Usage:
    vimview.py <image-directory> [--thumb N]

Keys (normal mode):
    h j k l / arrows   move selection
    0 $                first / last image in row
    gg / G             first / last image overall
    Ctrl+U / Ctrl+D    pan canvas half a screen (selection stays)
    mouse drag         pan canvas, mouse wheel zoom
    + / - (= / -)      zoom in / out, zz center on selection
    o                  cycle cluster order: date -> filename
    /                  filter by filename (Enter applies, Esc cancels)
    :                  command line (:q quit, :o date|filename, :h help)
    Enter              fullscreen preview of selection (Esc/Enter/q to leave)
    R                  rescan directory
    ?                  this help
    q                  quit
"""

import argparse
import ctypes
import os
import sys
import time
from collections import OrderedDict
from datetime import datetime

import sdl2
import sdl2.ext
from PIL import Image, ImageDraw, ImageFont

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}

ORDERS = ["date", "filename"]

# --------------------------------------------------------------------------
# small helpers


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def find_font(size):
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


_FONTS = {}


def get_font(size):
    if size not in _FONTS:
        _FONTS[size] = find_font(size)
    return _FONTS[size]


# --------------------------------------------------------------------------
# image catalogue


class ImageItem:
    __slots__ = ("path", "name", "mtime", "date")

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.mtime = os.path.getmtime(path)
        self.date = datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d")


def scan_directory(directory):
    items = []
    for entry in sorted(os.listdir(directory)):
        full = os.path.join(directory, entry)
        if os.path.isfile(full) and os.path.splitext(entry)[1].lower() in IMAGE_EXTS:
            try:
                items.append(ImageItem(full))
            except OSError:
                pass
    return items


# --------------------------------------------------------------------------
# layout: endless canvas of clustered thumbnails


class Layout:
    """Computes world-space cells for the current order + filter."""

    def __init__(self, thumb=256, pad=14, canvas_w=1600):
        self.thumb = thumb
        self.pad = pad
        self.canvas_w = canvas_w
        self.header_h = 44
        self.label_h = 24
        self.group_gap = 36
        self.cells = []  # list of dicts: item, x, y, w, h, row, col
        self.headers = []  # list of (text, x, y, w)
        self.rows = []  # list of lists of cell indexes (for j/k)
        self.total_h = 0

    def build(self, items, order):
        self.cells = []
        self.headers = []
        self.rows = []
        t, pad, cw = self.thumb, self.pad, self.canvas_w
        cell = t + pad
        per_row = max(1, (cw - pad) // cell)

        if order == "date":
            groups = OrderedDict()
            for it in sorted(items, key=lambda i: (i.date, i.name)):
                groups.setdefault(it.date, []).append(it)
            group_list = list(groups.items())
        else:
            group_list = [(None, sorted(items, key=lambda i: i.name.lower()))]

        y = pad
        for gtext, gitems in group_list:
            if gtext is not None:
                self.headers.append((gtext, pad, y, cw - 2 * pad))
                y += self.header_h + pad
            row = []
            x = pad
            for it in gitems:
                idx = len(self.cells)
                self.cells.append(
                    {"item": it, "x": x, "y": y, "w": t, "h": t + self.label_h}
                )
                row.append(idx)
                x += cell
                if len(row) == per_row:
                    self.rows.append(row)
                    row = []
                    x = pad
                    y += t + self.label_h + pad
            if row:
                self.rows.append(row)
                y += t + self.label_h + pad
            y += self.group_gap
        self.total_h = y


# --------------------------------------------------------------------------
# texture caches


class TextureCache:
    def __init__(self, renderer, capacity=500):
        self.ren = renderer
        self.capacity = capacity
        self.cache = OrderedDict()  # path -> (tex, w, h)

    def get(self, path, max_size):
        hit = self.cache.get(path)
        if hit:
            self.cache.move_to_end(path)
            return hit
        try:
            img = Image.open(path).convert("RGBA")
        except Exception:
            return None
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        canvas = Image.new("RGBA", (max_size, max_size), (0, 0, 0, 0))
        canvas.paste(img, ((max_size - img.width) // 2, (max_size - img.height) // 2),
                     img)
        tex = pil_to_texture(self.ren, canvas)
        if tex is None:
            return None
        entry = (tex, canvas.width, canvas.height)
        self.cache[path] = entry
        while len(self.cache) > self.capacity:
            old_path, (old_tex, _, _) = self.cache.popitem(last=False)
            sdl2.SDL_DestroyTexture(old_tex)
        return entry

    def drop_missing(self, valid_paths):
        for path in [p for p in self.cache if p not in valid_paths]:
            tex, _, _ = self.cache.pop(path)
            sdl2.SDL_DestroyTexture(tex)

    def clear(self):
        for _, (tex, _, _) in self.cache.items():
            sdl2.SDL_DestroyTexture(tex)
        self.cache.clear()


def pil_to_texture(renderer, pil_img):
    rgba = pil_img.convert("RGBA")
    w, h = rgba.size
    tex = sdl2.SDL_CreateTexture(
        renderer.sdlrenderer, sdl2.SDL_PIXELFORMAT_ABGR8888,
        sdl2.SDL_TEXTUREACCESS_STATIC, w, h,
    )
    if not tex:
        return None
    sdl2.SDL_UpdateTexture(tex, None, rgba.tobytes(), w * 4)
    sdl2.SDL_SetTextureBlendMode(tex, sdl2.SDL_BLENDMODE_BLEND)
    return tex


class TextCache:
    def __init__(self, renderer):
        self.ren = renderer
        self.cache = {}

    def get(self, text, size=15, color=(200, 200, 200, 255)):
        key = (text, size, color)
        tex = self.cache.get(key)
        if tex:
            return tex
        font = get_font(size)
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        box = probe.textbbox((0, 0), text, font=font)
        w, h = box[2] - box[0] + 6, box[3] - box[1] + 6
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.text((3 - box[0], 3 - box[1]), text, font=font, fill=color)
        tex = pil_to_texture(self.ren, img)
        if tex:
            self.cache[key] = (tex, w, h)
            return tex, w, h
        return None

    def clear(self):
        for tex, _, _ in self.cache.values():
            sdl2.SDL_DestroyTexture(tex)
        self.cache.clear()


def blit(renderer, tex, x, y, w=None, h=None):
    dst = sdl2.SDL_Rect(int(x), int(y), int(w), int(h))
    sdl2.SDL_RenderCopy(renderer.sdlrenderer, tex, None, dst)


def draw_rect_outline(renderer, x, y, w, h, color, thick=3):
    sdl2.SDL_SetRenderDrawColor(renderer.sdlrenderer, *color)
    for i in range(thick):
        r = sdl2.SDL_Rect(int(x) + i, int(y) + i, int(w) - 2 * i, int(h) - 2 * i)
        sdl2.SDL_RenderDrawRect(renderer.sdlrenderer, r)


def fill_rect(renderer, x, y, w, h, color):
    sdl2.SDL_SetRenderDrawColor(renderer.sdlrenderer, *color)
    r = sdl2.SDL_Rect(int(x), int(y), int(w), int(h))
    sdl2.SDL_RenderFillRect(renderer.sdlrenderer, r)


# --------------------------------------------------------------------------
# viewer


HELP_LINES = [
    "h j k l / arrows      move selection",
    "0 $                   first / last in row",
    "gg / G                first / last image",
    "Ctrl+U / Ctrl+D       pan half screen",
    "mouse drag / wheel    pan / zoom",
    "+ -                   zoom in / out      zz center",
    "o                     cycle order: date, filename",
    "/ text Enter          filter by filename (Esc cancels)",
    ":q  :o date|filename  :h",
    "Enter                 preview (Esc leaves)",
    "R rescan    ? help    q quit",
]


class Viewer:
    def __init__(self, directory, thumb=256):
        self.directory = os.path.abspath(directory)
        self.order_idx = 0
        self.filter_text = ""
        self.items = []       # all scanned
        self.visible = []     # after filter
        self.sel = 0
        self.layout = Layout(thumb=thumb)
        self.cam_x = 0.0
        self.cam_y = 0.0
        self.zoom = 1.0
        self.mode = "normal"  # normal | search | command | preview | help
        self.input_buf = ""
        self.pending = None   # pending multi-key (g, z)
        self.pending_at = 0.0
        self.message = ""
        self.message_at = 0.0
        self.win_w, self.win_h = 1280, 800
        self.dragging = False

        sdl2.ext.init()
        self.window = sdl2.ext.Window("vimview", size=(self.win_w, self.win_h),
                                      flags=sdl2.SDL_WINDOW_RESIZABLE)
        self.window.show()
        for rflags in (sdl2.SDL_RENDERER_ACCELERATED |
                       sdl2.SDL_RENDERER_PRESENTVSYNC,
                       0,
                       sdl2.SDL_RENDERER_SOFTWARE):
            try:
                self.renderer = sdl2.ext.Renderer(self.window, flags=rflags)
                break
            except sdl2.ext.SDLError:
                continue
        else:
            raise RuntimeError("could not create an SDL renderer")

        self.tex_cache = TextureCache(self.renderer)
        self.full_cache = TextureCache(self.renderer, capacity=4)
        self.text_cache = TextCache(self.renderer)
        self.rescan()

    # -- data -------------------------------------------------------------
    @property
    def order(self):
        return ORDERS[self.order_idx]

    def rescan(self):
        self.items = scan_directory(self.directory)
        self.apply_filter(quiet=True)
        self.note(f"{len(self.items)} images")

    def apply_filter(self, quiet=False):
        q = self.filter_text.lower()
        self.visible = [it for it in self.items if q in it.name.lower()]
        self.layout.build(self.visible, self.order)
        self.sel = clamp(self.sel, 0, max(0, len(self.visible) - 1))
        self.ensure_visible()
        if not quiet:
            self.note(f"{len(self.visible)}/{len(self.items)} match"
                      if q else f"{len(self.items)} images")

    def note(self, msg):
        self.message = msg
        self.message_at = time.time()

    def cycle_order(self):
        self.order_idx = (self.order_idx + 1) % len(ORDERS)
        self.layout.build(self.visible, self.order)
        self.ensure_visible()
        self.note("order: " + self.order)

    # -- camera -----------------------------------------------------------
    def world_to_screen(self, wx, wy):
        return (wx - self.cam_x) * self.zoom, (wy - self.cam_y) * self.zoom

    def screen_to_world(self, sx, sy):
        return sx / self.zoom + self.cam_x, sy / self.zoom + self.cam_y

    def ensure_visible(self, margin=60):
        if not self.layout.cells:
            return
        c = self.layout.cells[self.sel]
        x0, y0 = c["x"], c["y"]
        x1, y1 = x0 + c["w"], y0 + c["h"]
        vw, vh = self.win_w / self.zoom, (self.win_h - 28) / self.zoom
        m = margin / self.zoom
        if x0 < self.cam_x + m:
            self.cam_x = x0 - m
        elif x1 > self.cam_x + vw - m:
            self.cam_x = x1 - vw + m
        if y0 < self.cam_y + m:
            self.cam_y = y0 - m
        elif y1 > self.cam_y + vh - m:
            self.cam_y = y1 - vh + m
        self.cam_x = max(-200, self.cam_x)

    def pan(self, dx_world, dy_world):
        self.cam_x = max(-200, self.cam_x + dx_world)
        self.cam_y = max(-200, self.cam_y + dy_world)

    def zoom_at(self, factor, sx, sy):
        old = self.zoom
        new = clamp(old * factor, 0.2, 5.0)
        # world point under the cursor before the zoom change
        wx, wy = sx / old + self.cam_x, sy / old + self.cam_y
        self.zoom = new
        # keep that world point under the cursor
        self.cam_x = wx - sx / new
        self.cam_y = wy - sy / new

    # -- selection --------------------------------------------------------
    def move_sel(self, delta):
        if not self.visible:
            return
        self.sel = clamp(self.sel + delta, 0, len(self.visible) - 1)
        self.ensure_visible()

    def move_vert(self, direction):
        """j/k: nearest cell in the row above/below by x-center."""
        if not self.layout.rows:
            return
        cur = self.layout.cells[self.sel]
        cx = cur["x"] + cur["w"] / 2
        # find which row the selection is in
        cur_row = next((r for r in self.layout.rows if self.sel in r), None)
        if cur_row is None:
            return
        ri = self.layout.rows.index(cur_row)
        ni = clamp(ri + direction, 0, len(self.layout.rows) - 1)
        if ni == ri:
            return
        best = min(self.layout.rows[ni],
                   key=lambda i: abs(self.layout.cells[i]["x"] +
                                     self.layout.cells[i]["w"] / 2 - cx))
        self.sel = best
        self.ensure_visible()

    def row_edge(self, first):
        cur_row = next((r for r in self.layout.rows if self.sel in r), None)
        if cur_row:
            self.sel = cur_row[0] if first else cur_row[-1]
            self.ensure_visible()

    # -- input ------------------------------------------------------------
    def handle_keydown_normal(self, sym, mod):
        ctrl = mod & sdl2.KMOD_CTRL
        now = time.time()

        # multi-key sequences (gg, zz)
        if self.pending and now - self.pending_at < 1.0:
            if self.pending == "g" and sym == sdl2.SDLK_g:
                self.sel = 0
                self.ensure_visible()
            elif self.pending == "z" and sym == sdl2.SDLK_z:
                self.center_on_selection()
            self.pending = None
            return
        self.pending = None

        if ctrl and sym == sdl2.SDLK_u:
            self.pan(0, -(self.win_h / self.zoom) / 2)
            return
        if ctrl and sym == sdl2.SDLK_d:
            self.pan(0, (self.win_h / self.zoom) / 2)
            return

        keymap = {
            sdl2.SDLK_h: lambda: self.move_sel(-1),
            sdl2.SDLK_LEFT: lambda: self.move_sel(-1),
            sdl2.SDLK_l: lambda: self.move_sel(1),
            sdl2.SDLK_RIGHT: lambda: self.move_sel(1),
            sdl2.SDLK_j: lambda: self.move_vert(1),
            sdl2.SDLK_DOWN: lambda: self.move_vert(1),
            sdl2.SDLK_k: lambda: self.move_vert(-1),
            sdl2.SDLK_UP: lambda: self.move_vert(-1),
            sdl2.SDLK_0: lambda: self.row_edge(True),
            sdl2.SDLK_DOLLAR: lambda: self.row_edge(False),
            sdl2.SDLK_g: lambda: self.set_pending("g"),
            sdl2.SDLK_z: lambda: self.set_pending("z"),
            sdl2.SDLK_o: self.cycle_order,
            sdl2.SDLK_r: self.rescan,
            sdl2.SDLK_q: lambda: self.quit(),
            sdl2.SDLK_SLASH: lambda: self.enter_input_mode("search"),
            sdl2.SDLK_SEMICOLON: (lambda: self.enter_input_mode("command")
                                  if mod & sdl2.KMOD_SHIFT else None),
            sdl2.SDLK_QUESTION: lambda: self.set_mode("help"),
            sdl2.SDLK_RETURN: lambda: self.set_mode("preview")
            if self.visible else None,
            sdl2.SDLK_EQUALS: lambda: self.zoom_at(1.25, self.win_w / 2,
                                                  self.win_h / 2),
            sdl2.SDLK_PLUS: lambda: self.zoom_at(1.25, self.win_w / 2,
                                                self.win_h / 2),
            sdl2.SDLK_MINUS: lambda: self.zoom_at(0.8, self.win_w / 2,
                                                 self.win_h / 2),
        }
        # capital G (shift+g) -> last image
        if sym == sdl2.SDLK_g and (mod & sdl2.KMOD_SHIFT):
            if self.visible:
                self.sel = len(self.visible) - 1
                self.ensure_visible()
            return
        # $ on US layouts arrives as shift+4 (SDLK_DOLLAR is only for
        # keyboards with a dedicated $ key) -> end of row
        if sym == sdl2.SDLK_4 and (mod & sdl2.KMOD_SHIFT):
            self.row_edge(False)
            return
        fn = keymap.get(sym)
        if fn:
            fn()

    def set_pending(self, ch):
        self.pending = ch
        self.pending_at = time.time()

    def set_mode(self, mode):
        self.mode = mode

    def enter_input_mode(self, mode):
        self.mode = mode
        self.input_buf = ""
        sdl2.SDL_StartTextInput()

    def leave_input_mode(self):
        sdl2.SDL_StopTextInput()
        self.mode = "normal"

    def handle_keydown_input(self, sym):
        if sym == sdl2.SDLK_ESCAPE:
            self.leave_input_mode()
        elif sym == sdl2.SDLK_RETURN:
            text = self.input_buf
            if self.mode == "search":
                self.filter_text = text
                self.sel = 0
                self.apply_filter()
            else:
                self.run_command(text)
            self.leave_input_mode()
        elif sym == sdl2.SDLK_BACKSPACE:
            self.input_buf = self.input_buf[:-1]

    def run_command(self, text):
        parts = text.strip().split()
        if not parts:
            return
        cmd = parts[0]
        if cmd in ("q", "quit"):
            self.quit()
        elif cmd == "o" and len(parts) > 1 and parts[1] in ORDERS:
            self.order_idx = ORDERS.index(parts[1])
            self.layout.build(self.visible, self.order)
            self.ensure_visible()
            self.note("order: " + self.order)
        elif cmd in ("h", "help"):
            self.set_mode("help")
        else:
            self.note("unknown command: " + cmd)

    def center_on_selection(self):
        if not self.layout.cells:
            return
        c = self.layout.cells[self.sel]
        self.cam_x = c["x"] + c["w"] / 2 - (self.win_w / self.zoom) / 2
        self.cam_y = c["y"] + c["h"] / 2 - ((self.win_h - 28) / self.zoom) / 2

    def quit(self):
        raise SystemExit

    # -- events -----------------------------------------------------------
    def poll_events(self):
        for event in sdl2.ext.get_events():
            t = event.type
            if t == sdl2.SDL_QUIT:
                self.quit()
            elif t == sdl2.SDL_WINDOWEVENT and \
                    event.window.event == sdl2.SDL_WINDOWEVENT_RESIZED:
                self.win_w, self.win_h = event.window.data1, event.window.data2
            elif t == sdl2.SDL_KEYDOWN:
                sym, mod = event.key.keysym.sym, event.key.keysym.mod
                if self.mode in ("search", "command"):
                    self.handle_keydown_input(sym)
                elif self.mode == "preview":
                    if sym in (sdl2.SDLK_ESCAPE, sdl2.SDLK_RETURN,
                               sdl2.SDLK_q):
                        self.mode = "normal"
                elif self.mode == "help":
                    self.mode = "normal"  # any key closes help
                else:
                    if sym == sdl2.SDLK_ESCAPE:
                        self.filter_text = ""
                        self.apply_filter(quiet=True)
                    else:
                        self.handle_keydown_normal(sym, mod)
            elif t == sdl2.SDL_TEXTINPUT:
                if self.mode in ("search", "command"):
                    try:
                        self.input_buf += event.text.text.decode("utf-8")
                    except Exception:
                        pass
            elif t == sdl2.SDL_MOUSEBUTTONDOWN:
                if event.button.button == sdl2.SDL_BUTTON_LEFT:
                    self.dragging = True
                    sdl2.SDL_CaptureMouse(sdl2.SDL_TRUE)
            elif t == sdl2.SDL_MOUSEBUTTONUP:
                if event.button.button == sdl2.SDL_BUTTON_LEFT:
                    self.dragging = False
                    sdl2.SDL_CaptureMouse(sdl2.SDL_FALSE)
            elif t == sdl2.SDL_MOUSEMOTION:
                if self.dragging:
                    self.pan(-event.motion.xrel / self.zoom,
                             -event.motion.yrel / self.zoom)
            elif t == sdl2.SDL_MOUSEWHEEL:
                mx, my = ctypes.c_int(), ctypes.c_int()
                sdl2.SDL_GetMouseState(ctypes.byref(mx), ctypes.byref(my))
                if event.wheel.y > 0:
                    self.zoom_at(1.15, mx.value, my.value)
                elif event.wheel.y < 0:
                    self.zoom_at(1 / 1.15, mx.value, my.value)

    # -- render -----------------------------------------------------------
    def render(self):
        ren = self.renderer
        sdl2.SDL_SetRenderDrawColor(ren.sdlrenderer, 18, 18, 22, 255)
        sdl2.SDL_RenderClear(ren.sdlrenderer)

        if self.mode == "preview":
            self.render_preview()
        else:
            self.render_canvas()
        self.render_statusline()
        if self.mode == "help":
            self.render_help()
        sdl2.SDL_RenderPresent(ren.sdlrenderer)

    def render_canvas(self):
        ren = self.renderer
        vw, vh = self.win_w, self.win_h - 28
        status_top = self.win_h - 28

        if not self.layout.cells:
            t = self.text_cache.get("no images — press R to rescan, q to quit")
            if t:
                tex, w, h = t
                blit(ren, tex, (self.win_w - w) / 2, (self.win_h - h) / 2, w, h)
            return

        thumb = self.layout.thumb
        # date group headers
        for text, hx, hy, hw in self.layout.headers:
            sx, sy = self.world_to_screen(hx, hy)
            if -60 < sy < vh + 60:
                fill_rect(ren, 0, sy, self.win_w, 30 * self.zoom,
                          (38, 38, 48, 255))
                t = self.text_cache.get(text, size=16,
                                        color=(150, 180, 220, 255))
                if t:
                    tex, w, h = t
                    blit(ren, tex, sx + 8, sy + (30 * self.zoom - h) / 2, w, h)

        # thumbnails, visible only, budget loads per frame
        loads = 0
        for i, c in enumerate(self.layout.cells):
            sx, sy = self.world_to_screen(c["x"], c["y"])
            sw, sh = c["w"] * self.zoom, c["h"] * self.zoom
            if sx + sw < -50 or sx > vw + 50 or sy + sh < -50 or sy > vh + 50:
                continue
            entry = self.tex_cache.cache.get(c["item"].path)
            if entry is None and loads < 10:
                entry = self.tex_cache.get(c["item"].path, thumb)
                loads += 1
            if entry:
                tex, tw, th = entry
                # center the (possibly smaller) thumb in its cell
                dw, dh = tw * self.zoom, th * self.zoom
                blit(ren, tex, sx + (sw - dw) / 2, sy + (sh - dh) / 2, dw, dh)
            else:
                fill_rect(ren, sx, sy, sw, sh - self.layout.label_h *
                          self.zoom, (45, 45, 55, 255))
            # filename label
            name = c["item"].name
            if len(name) > 26:
                name = name[:24] + "…"
            t = self.text_cache.get(name, size=13)
            if t:
                tex, w, h = t
                scale = min(1.0, sw / max(w, 1))
                blit(ren, tex, sx + (sw - w * scale) / 2,
                     sy + sw + 2, w * scale, h * scale)
            if i == self.sel:
                draw_rect_outline(ren, sx - 3, sy - 3, sw + 6, sh + 6,
                                  (120, 200, 255, 255), 3)

        # drop textures that are nowhere near the viewport
        if len(self.tex_cache.cache) > self.tex_cache.capacity - 50:
            keep = set()
            for c in self.layout.cells:
                sx, sy = self.world_to_screen(c["x"], c["y"])
                if -200 < sx < vw + 200 and -200 < sy < vh + 200:
                    keep.add(c["item"].path)
            for path in [p for p in self.tex_cache.cache if p not in keep][:50]:
                tex, _, _ = self.tex_cache.cache.pop(path)
                sdl2.SDL_DestroyTexture(tex)

    def render_preview(self):
        ren = self.renderer
        if not self.visible:
            return
        item = self.visible[self.sel].path
        entry = self.full_cache.cache.get(item)
        if entry is None:
            try:
                img = Image.open(item).convert("RGBA")
                img.thumbnail((self.win_w * 2, self.win_h * 2), Image.LANCZOS)
                tex = pil_to_texture(ren, img)
                if tex:
                    entry = (tex, img.width, img.height)
                    self.full_cache.cache[item] = entry
            except Exception:
                entry = None
        if entry:
            tex, w, h = entry
            scale = min(self.win_w / w, (self.win_h - 28) / h)
            dw, dh = w * scale, h * scale
            blit(ren, tex, (self.win_w - dw) / 2, (self.win_h - 28 - dh) / 2,
                 dw, dh)
        name = self.visible[self.sel].name
        t = self.text_cache.get(f"{self.sel + 1}/{len(self.visible)}  {name}",
                                size=15)
        if t:
            tex, w, h = t
            blit(ren, tex, (self.win_w - w) / 2, self.win_h - 28 - h - 6, w, h)

    def render_statusline(self):
        ren = self.renderer
        y = self.win_h - 28
        fill_rect(ren, 0, y, self.win_w, 28, (30, 30, 38, 255))
        fill_rect(ren, 0, y, self.win_w, 1, (70, 70, 85, 255))

        if self.mode == "search":
            text = "/" + self.input_buf + "▌"
        elif self.mode == "command":
            text = ":" + self.input_buf + "▌"
        else:
            pos = f"{self.sel + 1}/{len(self.visible)}" if self.visible else "0/0"
            name = self.visible[self.sel].name if self.visible else "-"
            filt = f"  /{self.filter_text}" if self.filter_text else ""
            text = (f"  {self.order.upper()}  {pos}  {name}{filt}  "
                    f"{self.zoom:.2f}x")
            if self.message and time.time() - self.message_at < 3:
                text += f"   -- {self.message}"
        t = self.text_cache.get(text, size=15, color=(190, 200, 215, 255))
        if t:
            tex, w, h = t
            blit(ren, tex, 8, y + (28 - h) / 2, min(w, self.win_w - 16), h)

    def render_help(self):
        ren = self.renderer
        t = self.text_cache.get("vimview — keys  (any key closes)", size=17,
                                color=(140, 200, 255, 255))
        lines = [self.text_cache.get(l, size=15) for l in HELP_LINES]
        widths = [w for _, w, _ in [t] + [l for l in lines if l]]
        heights = [h for _, _, h in [t] + [l for l in lines if l]]
        bw, bh = max(widths) + 40, sum(heights) + 30
        bx, by = (self.win_w - bw) / 2, (self.win_h - bh) / 2
        sdl2.SDL_SetRenderDrawBlendMode(ren.sdlrenderer, sdl2.SDL_BLENDMODE_BLEND)
        fill_rect(ren, bx, by, bw, bh, (12, 12, 16, 235))
        draw_rect_outline(ren, bx, by, bw, bh, (90, 110, 150, 255), 2)
        yy = by + 14
        tex, w, h = t
        blit(ren, tex, bx + 20, yy, w, h)
        yy += h + 8
        for l in lines:
            if not l:
                continue
            tex, w, h = l
            blit(ren, tex, bx + 20, yy, w, h)
            yy += h + 4

    # -- main loop --------------------------------------------------------
    def run(self):
        try:
            while True:
                self.poll_events()
                self.render()
                sdl2.SDL_Delay(16)
        except SystemExit:
            pass
        finally:
            self.tex_cache.clear()
            self.full_cache.clear()
            self.text_cache.clear()
            sdl2.ext.quit()

    def selftest(self, frames=5):
        """Headless smoke test: layout, selection, zoom, render N frames."""
        assert self.items, "no images found for selftest"
        for order in ORDERS:
            self.order_idx = ORDERS.index(order)
            self.layout.build(self.visible, self.order)
            assert self.layout.cells, f"no cells in {order} mode"
            assert self.layout.rows, f"no rows in {order} mode"
        self.order_idx = 0
        self.layout.build(self.visible, self.order)
        n = len(self.visible)
        self.move_sel(n - 1)
        assert self.sel == n - 1
        self.move_sel(5)
        assert self.sel == n - 1  # clamped
        self.move_vert(1)
        self.move_vert(-1)
        cur_row = next(r for r in self.layout.rows if self.sel in r)
        self.row_edge(True)
        assert self.sel == cur_row[0]
        self.row_edge(False)
        assert self.sel == cur_row[-1]
        self.zoom_at(2.0, 100, 100)
        assert abs(self.zoom - 2.0) < 1e-6
        self.filter_text = self.visible[0].name[:4].lower()
        self.apply_filter(quiet=True)
        assert self.visible, "filter wiped everything"
        self.filter_text = ""
        self.apply_filter(quiet=True)
        self.set_mode("preview")
        for _ in range(frames):
            self.poll_events()
            self.render()
        self.set_mode("normal")
        for _ in range(frames):
            self.poll_events()
            self.render()
        print(f"selftest OK: {n} images, orders={ORDERS}, "
              f"cells={len(self.layout.cells)}, zoom={self.zoom:.2f}")


def main():
    ap = argparse.ArgumentParser(description="vimview — SDL2 image viewer")
    ap.add_argument("directory", nargs="?", default=".",
                    help="directory of images")
    ap.add_argument("--thumb", type=int, default=256,
                    help="thumbnail size in px (default 256)")
    ap.add_argument("--selftest", action="store_true",
                    help="headless smoke test, then exit")
    args = ap.parse_args()

    if not os.path.isdir(args.directory):
        print(f"not a directory: {args.directory}", file=sys.stderr)
        sys.exit(1)
    viewer = Viewer(args.directory, thumb=args.thumb)
    if args.selftest:
        try:
            viewer.selftest()
        finally:
            viewer.tex_cache.clear()
            viewer.full_cache.clear()
            viewer.text_cache.clear()
            sdl2.ext.quit()
    else:
        viewer.run()


if __name__ == "__main__":
    main()
