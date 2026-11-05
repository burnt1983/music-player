#!/usr/bin/env python3
"""Transparent desktop music player with a five-band equalizer.

Plays songs from ~/Music. Drag empty space to move it. The sliders are
Bass, Low, Mid, High, and Treble. Scroll a slider to nudge it, or scroll
anywhere else for volume. Right-click for songs, presets, and login.

Sits above the desktop icons and behind ordinary windows. Open through
XWayland so it can remember its place. The picture is only the controls:
the window itself is clear.
"""
from __future__ import annotations

import json
import math
import os
import random
import signal
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

sys.path[:0] = [
    str(Path(__file__).resolve().parent),
    str(Path.home() / ".local" / "lib"),
]
from desklet_geometry import (  # noqa: E402
    DeskletPlacement,
    bind_desktop_pin,
    prefer_xwayland,
    prepare_desktop_window,
)

prefer_xwayland()

import cairo  # noqa: E402
import gi  # noqa: E402

gi.require_version("Gdk", "3.0")
gi.require_version("Gst", "1.0")
gi.require_version("GstPbutils", "1.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gio, GLib, Gst, GstPbutils, Gtk, Pango, PangoCairo  # noqa: E402

HOME = Path.home()
MUSIC = HOME / "Music"
CFG_DIR = HOME / ".config" / "lee-player"
CFG = CFG_DIR / "config.json"
PID = HOME / ".cache" / "lee-player.pid"
LOG = Path("/tmp/player-widget.log")
ICON = HOME / ".local" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "lee-player.png"
AUTOSTART = HOME / ".config" / "autostart" / "lee-player.desktop"
APP_DESKTOP = HOME / ".local" / "share" / "applications" / "lee-player.desktop"
APP_ID = "uk.lee.player"
PRGNAME = "lee-player"
WIN_W = 460
WIN_H = 272
# playbin: audio only, software volume, and keep the equalizer in the chain.
AUDIO_FLAGS = (1 << 1) | (1 << 4) | (1 << 11)
AUDIO_EXT = {".mp3", ".wav", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".opus", ".aiff", ".aif"}
SKIP_DIRS = {
    "__pycache__",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "Trash",
}
BANDS = ("Bass", "Low", "Mid", "High", "Treble")
# Each slider drives a pair of equalizer-10bands centres.
BAND_PAIRS = ((0, 1), (2, 3), (4, 5), (6, 7), (8, 9))
PRESETS = (
    ("Flat", (0, 0, 0, 0, 0)),
    ("Bass", (8, 4, 0, 0, 0)),
    ("Warm", (4, 3, 1, -1, -2)),
    ("Vocal", (-2, 0, 5, 3, 0)),
    ("Bright", (0, -1, 1, 4, 6)),
    ("Night", (-2, 0, 0, -3, -6)),
)
DRAG_HITS = {"empty", "title", "meta"}
INK = (0.973, 0.949, 0.914)
GOLD = (0.910, 0.765, 0.420)
SKY = (0.620, 0.784, 0.910)
AMBER = (0.940, 0.640, 0.400)
CLICK_SLOP = 5

Gst.init(None)


def log(message: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%H:%M:%S") + " " + message + "\n")
    except OSError:
        pass


def clamp_db(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    number = max(-12.0, min(12.0, number))
    return math.floor(number * 2 + 0.5) / 2.0


def clamp_unit(value, default: float = 0.7) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def clean_gains(value) -> list[float]:
    if not isinstance(value, list) or len(value) != 5:
        return [0.0, 0.0, 0.0, 0.0, 0.0]
    return [clamp_db(item) for item in value]


def db_label(gain: float) -> str:
    gain = clamp_db(gain)
    if abs(gain) < 0.25:
        return "0"
    if abs(gain - round(gain)) < 0.05:
        return f"{gain:+.0f}"
    return f"{gain:+.1f}"


def format_time(seconds: float) -> str:
    try:
        whole = int(seconds)
    except (TypeError, ValueError):
        whole = 0
    if whole < 0:
        whole = 0
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def gain_from_y(y: float, top: float, bottom: float) -> float:
    span = bottom - top
    if span <= 1:
        return 0.0
    t = min(1.0, max(0.0, (y - top) / span))
    return clamp_db(12.0 - t * 24.0)


def y_from_gain(gain: float, top: float, bottom: float) -> float:
    t = (12.0 - clamp_db(gain)) / 24.0
    return top + t * (bottom - top)


def compute_layout(width: int, height: int) -> dict:
    left = 18.0
    right = max(left + 40.0, width - 18.0)
    inner = right - left
    cy = 104.0
    fader_top = cy + 38.0
    fader_bottom = height - 34.0
    gap = inner / 5.0
    faders = []
    for index, name in enumerate(BANDS):
        cx = left + gap * (index + 0.5)
        faders.append(
            {
                "name": name,
                "cx": cx,
                "top": fader_top,
                "bottom": max(fader_top + 24.0, fader_bottom),
                "hit": (cx - gap / 2.0, fader_top - 6.0, gap, height - (fader_top - 6.0)),
                "label_y": height - 26.0,
            }
        )
    vol_x = left + 198.0
    return {
        "title": (left, 8.0, inner, 26.0),
        "meta": (left, 36.0, inner, 18.0),
        "seek": (left, 58.0, inner, 18.0),
        "prev": (left + 28.0, cy, 16.0),
        "play": (left + 78.0, cy, 22.0),
        "next": (left + 128.0, cy, 16.0),
        "speaker": (left + 176.0, cy, 14.0),
        "volume": (vol_x, cy - 10.0, max(24.0, right - vol_x), 20.0),
        "faders": faders,
    }


def _in_rect(rect, x: float, y: float) -> bool:
    rx, ry, rw, rh = rect
    return rx <= x <= rx + rw and ry <= y <= ry + rh


def hit_test(layout: dict, x: float, y: float) -> str:
    for index, fader in enumerate(layout["faders"]):
        if _in_rect(fader["hit"], x, y):
            return f"fader:{index}"
    for name in ("play", "prev", "next", "speaker"):
        cx, cy, radius = layout[name]
        if (x - cx) ** 2 + (y - cy) ** 2 <= (radius + 4) ** 2:
            return name
    if _in_rect(layout["volume"], x, y):
        return "volume"
    if _in_rect(layout["seek"], x, y):
        return "seek"
    if _in_rect(layout["title"], x, y):
        return "title"
    if _in_rect(layout["meta"], x, y):
        return "meta"
    return "empty"


def scan_library(root: Path) -> list[Path]:
    root = root.expanduser()
    if not root.is_dir():
        return []
    found: list[Path] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [
            name
            for name in dirs
            if name not in SKIP_DIRS and not name.startswith(".")
        ]
        for name in files:
            if name.startswith(".") or name.startswith("._"):
                continue
            if Path(name).suffix.lower() not in AUDIO_EXT:
                continue
            found.append((Path(dirpath) / name).resolve())
            if len(found) >= 5000:
                break
        if len(found) >= 5000:
            break
    found.sort(key=lambda path: (path.name.casefold(), str(path).casefold()))
    return found


def load_cfg() -> dict:
    data = {}
    if CFG.exists():
        try:
            loaded = json.loads(CFG.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    repeat = data.get("repeat")
    try:
        position = max(0.0, float(data.get("position") or 0))
    except (TypeError, ValueError):
        position = 0.0
    # Positions saved before the clock was logical were pipeline timestamps.
    if data.get("clock") != "logical":
        position = 0.0
    library = data.get("library")
    if not isinstance(library, str) or not library:
        library = str(MUSIC)
    path = data.get("path") if isinstance(data.get("path"), str) else ""
    return {
        "x": data.get("x") if isinstance(data.get("x"), int) else None,
        "y": data.get("y") if isinstance(data.get("y"), int) else None,
        "volume": clamp_unit(data.get("volume", 0.7)),
        "mute": bool(data.get("mute", False)),
        "gains": clean_gains(data.get("gains")),
        "shuffle": bool(data.get("shuffle", False)),
        "repeat": repeat if repeat in {"off", "all", "one"} else "all",
        "on_top": bool(data.get("on_top", False)),
        "position": position,
        "path": path,
        "library": library,
        "clock": "logical",
    }


def save_cfg(cfg: dict) -> None:
    CFG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CFG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CFG)


def work_area() -> tuple[int, int, int, int]:
    display = Gdk.Display.get_default()
    if display is None or display.get_n_monitors() < 1:
        return 0, 0, 1920, 1080
    monitor = display.get_primary_monitor() or display.get_monitor(0)
    if monitor is None:
        return 0, 0, 1920, 1080
    box = monitor.get_workarea()
    return int(box.x), int(box.y), int(box.width), int(box.height)


def desktop_text(exec_path: str) -> str:
    return "\n".join(
        (
            "[Desktop Entry]",
            "Version=1.0",
            "Type=Application",
            "Name=Music Player",
            "Comment=Transparent desktop player with bass and equalizer",
            f"Exec={exec_path}",
            f"Icon={ICON}",
            "Terminal=false",
            "Categories=Audio;AudioVideo;Player;",
            "StartupNotify=false",
            "StartupWMClass=lee-player",
            "Keywords=music;player;equalizer;bass;desktop;transparent;",
            "MimeType=audio/mpeg;audio/x-wav;audio/flac;audio/ogg;audio/mp4;",
            "",
        )
    )


def install_commands() -> None:
    target = Path(__file__).resolve()
    bindir = HOME / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    named = bindir / "lee-player"
    if named.is_symlink() or not named.exists():
        if not named.is_symlink() or named.resolve() != target:
            named.unlink(missing_ok=True)
            named.symlink_to(target)
    generic = bindir / "player"
    if generic.exists() and not generic.is_symlink():
        return
    if generic.is_symlink() and generic.resolve() != target:
        return
    if not generic.is_symlink():
        generic.symlink_to(target)


def install_launchers() -> None:
    install_commands()
    body = desktop_text(str(Path(__file__).resolve()))
    for path in (APP_DESKTOP,):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != body:
            path.write_text(body, encoding="utf-8")
        try:
            Gio.File.new_for_path(str(path)).set_attribute_string(
                "metadata::trusted", "true", Gio.FileQueryInfoFlags.NONE, None
            )
        except Exception:
            pass
    if not AUTOSTART.exists():
        text = body + "X-GNOME-Autostart-enabled=false\nX-GNOME-Autostart-Delay=5\nHidden=true\n"
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART.write_text(text, encoding="utf-8")


def autostart_enabled() -> bool:
    if not AUTOSTART.exists():
        return False
    hidden = False
    enabled = False
    for line in AUTOSTART.read_text(encoding="utf-8").splitlines():
        if line.startswith("Hidden="):
            hidden = line.split("=", 1)[1].strip().lower() == "true"
        elif line.startswith("X-GNOME-Autostart-enabled="):
            enabled = line.split("=", 1)[1].strip().lower() == "true"
    return enabled and not hidden


def set_autostart(enabled: bool) -> None:
    install_launchers()
    lines = AUTOSTART.read_text(encoding="utf-8").splitlines()
    found_enabled = False
    found_hidden = False
    rewritten = []
    for line in lines:
        if line.startswith("X-GNOME-Autostart-enabled="):
            rewritten.append("X-GNOME-Autostart-enabled=" + ("true" if enabled else "false"))
            found_enabled = True
        elif line.startswith("Hidden="):
            rewritten.append("Hidden=" + ("false" if enabled else "true"))
            found_hidden = True
        else:
            rewritten.append(line)
    if not found_enabled:
        rewritten.append("X-GNOME-Autostart-enabled=" + ("true" if enabled else "false"))
    if not found_hidden:
        rewritten.append("Hidden=" + ("false" if enabled else "true"))
    AUTOSTART.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def round_rect(cr: cairo.Context, x: float, y: float, w: float, h: float, r: float) -> None:
    r = min(r, w / 2.0, h / 2.0)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def fill_rgba(cr: cairo.Context, color) -> None:
    cr.set_source_rgba(*color)
    cr.fill()


def draw_text(
    cr: cairo.Context,
    text: str,
    x: float,
    y: float,
    w: float,
    h: float,
    size: float,
    color,
    weight=Pango.Weight.NORMAL,
    align: str = "left",
) -> None:
    layout = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription()
    desc.set_family("Sans")
    desc.set_absolute_size(size * Pango.SCALE)
    desc.set_weight(weight)
    layout.set_font_description(desc)
    layout.set_width(max(1, int(w * Pango.SCALE)))
    layout.set_ellipsize(Pango.EllipsizeMode.END)
    layout.set_text(text or "", -1)
    layout.set_alignment(
        {"left": Pango.Alignment.LEFT, "center": Pango.Alignment.CENTER, "right": Pango.Alignment.RIGHT}[align]
    )
    cr.save()
    cr.set_source_rgba(0, 0, 0, 0.58)
    cr.move_to(x, y + 1)
    PangoCairo.show_layout(cr, layout)
    cr.set_source_rgba(*color)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    cr.restore()


def stroke_path(cr: cairo.Context, width: float, color) -> None:
    cr.save()
    cr.set_line_width(width + 1.5)
    cr.set_source_rgba(0, 0, 0, 0.48)
    cr.stroke_preserve()
    cr.set_line_width(width)
    cr.set_source_rgba(*color)
    cr.stroke()
    cr.restore()


def fill_path(cr: cairo.Context, color) -> None:
    cr.save()
    cr.set_source_rgba(0, 0, 0, 0.48)
    cr.fill_preserve()
    cr.set_source_rgba(*color)
    cr.fill()
    cr.restore()


def draw_ring(cr: cairo.Context, cx: float, cy: float, radius: float, hot: bool) -> None:
    cr.new_path()
    cr.arc(cx, cy, radius, 0, math.tau)
    color = (*GOLD, 1.0) if hot else (*INK, 0.94)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    stroke_path(cr, 1.45, color)


def draw_triangle(cr: cairo.Context, cx: float, cy: float, size: float, pointing: float, color) -> None:
    cr.new_path()
    cr.move_to(cx - pointing * size * 0.38, cy - size)
    cr.line_to(cx - pointing * size * 0.38, cy + size)
    cr.line_to(cx + pointing * size * 0.78, cy)
    cr.close_path()
    fill_path(cr, color)


def draw_pause(cr: cairo.Context, cx: float, cy: float, color) -> None:
    for dx in (-5.2, 2.2):
        cr.new_path()
        cr.rectangle(cx + dx, cy - 7.2, 2.8, 14.4)
        fill_path(cr, color)


def draw_skip(cr: cairo.Context, cx: float, cy: float, forward: bool, hot: bool) -> None:
    color = (*GOLD, 1.0) if hot else (*INK, 0.94)
    sign = 1.0 if forward else -1.0
    cr.new_path()
    cr.rectangle(cx + sign * 5.2, cy - 6.2, 2.2, 12.4)
    fill_path(cr, color)
    draw_triangle(cr, cx - sign * 1.6, cy, 6.4, sign, color)


def draw_speaker(cr: cairo.Context, cx: float, cy: float, mute: bool, hot: bool) -> None:
    color = (*GOLD, 1.0) if hot else (*INK, 0.94)
    cr.new_path()
    cr.move_to(cx - 7, cy - 3)
    cr.line_to(cx - 3, cy - 3)
    cr.line_to(cx + 2, cy - 7)
    cr.line_to(cx + 2, cy + 7)
    cr.line_to(cx - 3, cy + 3)
    cr.line_to(cx - 7, cy + 3)
    cr.close_path()
    fill_path(cr, color)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    if mute:
        cr.new_path()
        cr.move_to(cx - 8, cy - 8)
        cr.line_to(cx + 8, cy + 8)
        cr.move_to(cx + 8, cy - 8)
        cr.line_to(cx - 8, cy + 8)
        stroke_path(cr, 1.4, color)
        return
    for radius in (4.5, 7.4):
        cr.new_path()
        cr.arc(cx + 2, cy, radius, -0.85, 0.85)
        stroke_path(cr, 1.25, color)


def draw_bar(cr: cairo.Context, rect, frac: float, hot: bool, dim: bool = False) -> None:
    x, y, w, h = rect
    cy = y + h / 2.0
    frac = min(1.0, max(0.0, frac))
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.new_path()
    cr.move_to(x, cy)
    cr.line_to(x + w, cy)
    cr.set_line_width(3.6)
    cr.set_source_rgba(0, 0, 0, 0.45)
    cr.stroke()
    cr.new_path()
    cr.move_to(x, cy)
    cr.line_to(x + w, cy)
    cr.set_line_width(2.0)
    cr.set_source_rgba(*INK, 0.22 if not dim else 0.14)
    cr.stroke()
    if frac > 0.002:
        cr.new_path()
        cr.move_to(x, cy)
        cr.line_to(x + w * frac, cy)
        cr.set_line_width(2.0)
        cr.set_source_rgba(*(GOLD if hot else INK), 0.55 if dim else 0.95)
        cr.stroke()
    knob = x + w * frac
    cr.new_path()
    cr.arc(knob, cy + 0.6, 4.4 if hot else 3.5, 0, math.tau)
    cr.set_source_rgba(0, 0, 0, 0.45)
    cr.fill()
    cr.new_path()
    cr.arc(knob, cy, 4.0 if hot else 3.2, 0, math.tau)
    cr.set_source_rgba(*(GOLD if hot else INK), 0.98)
    cr.fill()


def draw_faders(cr: cairo.Context, layout: dict, gains, hover: str) -> None:
    faders = layout["faders"]
    if not faders:
        return
    zero = (faders[0]["top"] + faders[0]["bottom"]) / 2.0
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.new_path()
    cr.move_to(faders[0]["cx"] - 10, zero)
    cr.line_to(faders[-1]["cx"] + 10, zero)
    cr.set_line_width(1.0)
    cr.set_source_rgba(0, 0, 0, 0.35)
    cr.stroke()
    cr.new_path()
    cr.move_to(faders[0]["cx"] - 10, zero)
    cr.line_to(faders[-1]["cx"] + 10, zero)
    cr.set_source_rgba(*INK, 0.28)
    cr.stroke()
    for index, fader in enumerate(faders):
        gain = gains[index]
        hot = hover == f"fader:{index}"
        cx = fader["cx"]
        top = fader["top"]
        bottom = fader["bottom"]
        knob = y_from_gain(gain, top, bottom)
        cr.new_path()
        cr.move_to(cx, top)
        cr.line_to(cx, bottom)
        cr.set_line_width(3.4)
        cr.set_source_rgba(0, 0, 0, 0.42)
        cr.stroke()
        cr.new_path()
        cr.move_to(cx, top)
        cr.line_to(cx, bottom)
        cr.set_line_width(1.7)
        cr.set_source_rgba(*INK, 0.30)
        cr.stroke()
        if abs(gain) >= 0.25:
            cr.new_path()
            cr.move_to(cx, zero)
            cr.line_to(cx, knob)
            cr.set_line_width(2.4)
            cr.set_source_rgba(*(GOLD if gain > 0 else SKY), 0.96)
            cr.stroke()
        color = (*GOLD, 1.0) if hot else (*INK, 0.98)
        round_rect(cr, cx - 9, knob - 4.2, 18, 8.4, 4)
        fill_path(cr, color)
        label_color = (*GOLD, 0.98) if hot else (*INK, 0.78)
        draw_text(
            cr,
            fader["name"],
            cx - 36,
            fader["label_y"],
            72,
            16,
            11,
            label_color,
            Pango.Weight.NORMAL,
            "center",
        )
        if hot:
            draw_text(cr, db_label(gain), cx - 24, knob - 20, 48, 16, 11, (*INK, 0.95), Pango.Weight.SEMIBOLD, "center")


@dataclass
class View:
    title: str
    meta: str
    time_label: str
    position: float
    duration: float
    playing: bool
    volume: float
    mute: bool
    gains: tuple
    hover: str
    notice: str = ""


def draw_player(cr: cairo.Context, width: int, height: int, view: View) -> None:
    layout = compute_layout(width, height)
    title_color = (*INK, 0.98)
    meta_color = (*AMBER, 0.98) if view.notice else (*INK, 0.72)
    meta = view.notice or view.meta
    tx, ty, tw, th = layout["title"]
    draw_text(cr, view.title, tx, ty, tw, th, 16, title_color, Pango.Weight.SEMIBOLD, "left")
    mx, my, mw, mh = layout["meta"]
    draw_text(cr, meta, mx, my, mw * 0.56, mh, 11.5, meta_color, Pango.Weight.NORMAL, "left")
    draw_text(
        cr,
        view.time_label,
        mx + mw * 0.42,
        my,
        mw * 0.58,
        mh,
        11.5,
        (*INK, 0.78),
        Pango.Weight.NORMAL,
        "right",
    )
    frac = 0.0 if view.duration <= 0 else min(1.0, view.position / view.duration)
    draw_bar(cr, layout["seek"], frac, view.hover == "seek")
    for name in ("prev", "next"):
        cx, cy, radius = layout[name]
        hot = view.hover == name
        draw_ring(cr, cx, cy, radius, hot)
        draw_skip(cr, cx, cy, name == "next", hot)
    cx, cy, radius = layout["play"]
    hot = view.hover == "play"
    draw_ring(cr, cx, cy, radius, hot or view.playing)
    glyph = (*GOLD, 1.0) if hot or view.playing else (*INK, 0.96)
    if view.playing:
        draw_pause(cr, cx, cy, glyph)
    else:
        draw_triangle(cr, cx + 1.5, cy, 7.2, 1, glyph)
    sx, sy, _sr = layout["speaker"]
    draw_speaker(cr, sx, sy, view.mute, view.hover == "speaker")
    draw_bar(cr, layout["volume"], 0.0 if view.mute else view.volume, view.hover == "volume", view.mute)
    draw_faders(cr, layout, view.gains, view.hover)


def write_icon() -> None:
    ICON.parent.mkdir(parents=True, exist_ok=True)
    side = 256
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, side, side)
    cr = cairo.Context(surface)
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)
    round_rect(cr, 18, 18, 220, 220, 52)
    cr.set_source_rgba(11 / 255, 61 / 255, 61 / 255, 1)
    cr.fill()
    heights = (46, 78, 112, 86, 58)
    for index, bar in enumerate(heights):
        x = 58 + index * 32
        y = 168 - bar
        round_rect(cr, x, y, 16, bar, 6)
        cr.set_source_rgba(0.973, 0.949, 0.914, 1)
        cr.fill()
    surface.write_to_png(str(ICON))


def paint_clear(widget) -> None:
    context = widget.get_style_context()
    context.remove_class("background")
    clear = Gdk.RGBA(0, 0, 0, 0)
    for state in (
        Gtk.StateFlags.NORMAL,
        Gtk.StateFlags.ACTIVE,
        Gtk.StateFlags.PRELIGHT,
        Gtk.StateFlags.SELECTED,
        Gtk.StateFlags.INSENSITIVE,
        Gtk.StateFlags.BACKDROP,
    ):
        try:
            widget.override_background_color(state, clear)
        except Exception:
            pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        text = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    if "python" not in text:
        return False
    return "lee_player_widget.py" in text or "lee-player" in text or "/bin/player" in text


def claim_pid(replace: bool) -> None:
    PID.parent.mkdir(parents=True, exist_ok=True)
    if PID.exists():
        text = PID.read_text(encoding="utf-8").strip()
        if text.isdigit():
            old = int(text)
            if old != os.getpid() and pid_alive(old):
                if not replace:
                    print("Music player is already on the desktop.", file=sys.stderr)
                    raise SystemExit(0)
                os.kill(old, signal.SIGTERM)
                for _ in range(40):
                    if not pid_alive(old):
                        break
                    time.sleep(0.05)
    PID.write_text(str(os.getpid()) + "\n", encoding="utf-8")


def release_pid() -> None:
    try:
        if PID.exists() and PID.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID.unlink()
    except OSError:
        pass


def extra_paths(argv: list[str]) -> list[Path]:
    paths = []
    for arg in argv:
        if arg.startswith("-"):
            continue
        paths.append(Path(arg).expanduser())
    return paths


class PlayerWindow(Gtk.Window):
    def __init__(self, cfg: dict):
        super().__init__(title="Music")
        self.cfg = cfg
        self.gains = list(cfg["gains"])
        self.volume = float(cfg["volume"])
        self.mute = bool(cfg["mute"])
        self.shuffle = bool(cfg["shuffle"])
        self.repeat = cfg["repeat"]
        self.tracks: list[Path] = []
        self.index = 0
        self.history: list[int] = []
        self.position = float(cfg["position"])
        self.duration = 0.0
        self.playing = False
        self.hover = ""
        self.notice = ""
        self.notice_until = 0.0
        self.gesture = None
        self.pending_seek = None
        self._gen = 0
        self.clock_origin = 0.0
        self._clock_ready = False
        self.player = None
        self.eq = None
        self._save_id = None
        self._seek_at = 0.0
        self._last_flush = time.monotonic()
        self._closed = False
        self._screening = False

        self.set_name("lee-player")
        self.set_role("lee-player")
        self.set_app_paintable(True)
        self.set_resizable(False)
        try:
            self.set_icon_from_file(str(ICON))
        except Exception:
            pass
        self.connect("screen-changed", self._on_screen)
        self._on_screen()
        self.connect("draw", self._clear_window)
        prepare_desktop_window(self)
        paint_clear(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"""
            window, window.background, #lee-player, drawingarea {
              background-color: transparent;
              background-image: none;
              border: none;
              box-shadow: none;
            }
            """
        )
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.area = Gtk.DrawingArea()
        self.area.set_app_paintable(True)
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.set_can_focus(True)
        paint_clear(self.area)
        self.area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.SMOOTH_SCROLL_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
        )
        self.area.connect("draw", self._on_draw)
        self.add(self.area)
        self.set_default_size(WIN_W, WIN_H)
        self.set_size_request(WIN_W, WIN_H)
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self._on_drop)

        self._open_library()
        self._ensure_player()
        if not isinstance(self.cfg.get("x"), int) or not isinstance(self.cfg.get("y"), int):
            wx, wy, ww, wh = work_area()
            self.cfg["x"] = int(wx + 64)
            self.cfg["y"] = int(wy + max(8, wh - WIN_H - 36))
            self.snapshot()
        self._place = DeskletPlacement(
            self,
            self._load_geo,
            self._save_geo,
            min_w=WIN_W,
            min_h=WIN_H,
            on_quit=self.quit,
        )
        self._place.watch(self.area)
        self.area.connect("button-press-event", self._on_press)
        self.area.connect("button-release-event", self._on_release)
        self.area.connect("motion-notify-event", self._on_motion)
        self.area.connect("leave-notify-event", self._on_leave)
        self.area.connect("scroll-event", self._on_scroll)
        self.area.connect("grab-broken-event", self._on_break)
        self.connect("key-press-event", self._on_key)
        self.connect("map-event", self._on_map)
        self.connect("window-state-event", self._on_state)
        self.connect("delete-event", self._on_delete)
        self.connect("realize", self._on_realize)
        self._place.apply_before_show()
        GLib.timeout_add(200, self._tick)

    def _on_screen(self, *_args) -> None:
        if self._screening:
            return
        self._screening = True
        try:
            screen = self.get_screen()
            if screen is None:
                return
            visual = screen.get_rgba_visual()
            if visual is not None and self.get_visual() != visual:
                self.set_visual(visual)
        finally:
            self._screening = False

    def _clear_window(self, _widget, cr: cairo.Context) -> bool:
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        return False

    def _on_draw(self, widget, cr: cairo.Context) -> bool:
        width = max(1, int(widget.get_allocated_width()))
        height = max(1, int(widget.get_allocated_height()))
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        draw_player(cr, width, height, self.view())
        return True

    def _on_realize(self, *_args) -> bool:
        self.restack()
        return False

    def _on_map(self, *_args) -> bool:
        self.restack()
        shot = os.environ.get("LEE_PLAYER_SHOT")
        if shot:
            GLib.timeout_add(700, self._save_shot, shot)
        return False

    def _save_shot(self, path: str) -> bool:
        gdk = self.get_window()
        if gdk is None:
            return False
        width, height = self.get_size()
        try:
            pix = Gdk.pixbuf_get_from_window(gdk, 0, 0, int(width), int(height))
            if pix is not None:
                pix.savev(path, "png", [], [])
                log(f"shot {path} {width}x{height}")
        except Exception as exc:
            log(f"shot failed {exc}")
        return False

    def _on_state(self, _widget, event) -> bool:
        state = int(event.new_window_state)
        if self.cfg.get("on_top"):
            if not state & int(Gdk.WindowState.ABOVE):
                self.set_keep_above(True)
            return False
        if state & int(Gdk.WindowState.BELOW):
            self.set_keep_below(False)
            self.restack()
        return False

    def restack(self) -> None:
        if self.cfg.get("on_top"):
            self.set_keep_above(True)
            self.set_keep_below(False)
            return
        self.set_keep_above(False)
        self.set_keep_below(False)
        bind_desktop_pin(self)

    def view(self) -> View:
        if self.notice and time.monotonic() > self.notice_until:
            self.notice = ""
        if not self.tracks:
            title = "Music"
            meta = "Right-click to open a song"
        else:
            track = self.tracks[self.index]
            title = (track.stem or track.name).strip() or track.name
            parent = track.parent
            place = f"{self.index + 1} of {len(self.tracks)}"
            if parent != MUSIC and parent.name and parent.name != "Music":
                meta = f"{parent.name} · {place}"
            else:
                meta = place
        if self.duration > 0:
            clock = f"{format_time(self.position)} / {format_time(self.duration)}"
        else:
            clock = format_time(self.position)
        return View(
            title=title,
            meta=meta,
            time_label=clock,
            position=self.position,
            duration=self.duration,
            playing=self.playing,
            volume=self.volume,
            mute=self.mute,
            gains=tuple(self.gains),
            hover=self.hover if self.gesture is None else self.gesture.get("hit", self.hover),
            notice=self.notice,
        )

    def layout(self) -> dict:
        width, height = self.get_size()
        if width < 2 or height < 2:
            width, height = WIN_W, WIN_H
        return compute_layout(int(width), int(height))

    def hit(self, x: float, y: float) -> str:
        return hit_test(self.layout(), x, y)

    def flash(self, text: str) -> None:
        self.notice = text
        self.notice_until = time.monotonic() + 3.5
        log(text)
        self.queue_draw()

    def snapshot(self) -> None:
        self.cfg["volume"] = self.volume
        self.cfg["mute"] = self.mute
        self.cfg["gains"] = [float(gain) for gain in self.gains]
        self.cfg["shuffle"] = self.shuffle
        self.cfg["repeat"] = self.repeat
        self.cfg["on_top"] = bool(self.cfg.get("on_top"))
        if self.pending_seek is None:
            self.cfg["clock"] = "logical"
        self.cfg["position"] = round(float(self.position), 2)
        if self.tracks:
            self.cfg["path"] = str(self.tracks[self.index])
        save_cfg(self.cfg)

    def touch_save(self) -> None:
        if self._save_id is not None:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(400, self._flush)

    def _flush(self) -> bool:
        self._save_id = None
        self._last_flush = time.monotonic()
        try:
            self.snapshot()
        except Exception as exc:
            log(f"save failed {exc}")
        return False

    def _load_geo(self):
        if not isinstance(self.cfg.get("x"), int) or not isinstance(self.cfg.get("y"), int):
            return None
        return self.cfg["x"], self.cfg["y"], WIN_W, WIN_H

    def _save_geo(self, x: int, y: int, _w: int, _h: int) -> None:
        self.cfg["x"] = int(x)
        self.cfg["y"] = int(y)
        self.snapshot()

    def _open_library(self) -> None:
        root = Path(self.cfg.get("library") or MUSIC)
        if not root.is_dir():
            root = MUSIC
            self.cfg["library"] = str(MUSIC)
        self.tracks = scan_library(root)
        saved = Path(self.cfg["path"]) if self.cfg.get("path") else None
        self.index = 0
        if saved is not None:
            resolved = saved.expanduser()
            try:
                resolved = resolved.resolve()
            except OSError:
                pass
            found = False
            for index, track in enumerate(self.tracks):
                if track == resolved or str(track) == str(saved):
                    self.index = index
                    found = True
                    break
            if not found and resolved.is_file() and resolved.suffix.lower() in AUDIO_EXT:
                self.tracks.insert(0, resolved)
                self.index = 0

    def _ensure_player(self) -> None:
        if self.player is not None:
            return
        self.player = Gst.ElementFactory.make("playbin", "lee-player")
        self.eq = Gst.ElementFactory.make("equalizer-10bands", "eq")
        if self.player is None or self.eq is None:
            self.player = None
            self.eq = None
            self.flash("Equalizer is not available")
            return
        self.player.set_property("audio-filter", self.eq)
        self.player.set_property("flags", AUDIO_FLAGS)
        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)
        self.apply_eq()
        self.apply_volume()

    def apply_eq(self) -> None:
        if self.eq is None:
            return
        for gain, pair in zip(self.gains, BAND_PAIRS):
            for band in pair:
                self.eq.set_property(f"band{band}", float(gain))

    def apply_volume(self) -> None:
        if self.player is None:
            return
        self.player.set_property("mute", bool(self.mute))
        self.player.set_property("volume", 0.0 if self.mute else float(self.volume))

    def load_current(self, autoplay: bool, resume: float | None) -> None:
        if not self.tracks:
            self.playing = False
            self.flash("No songs")
            return
        self.index %= len(self.tracks)
        path = self.tracks[self.index]
        self._ensure_player()
        if self.player is None:
            return
        self.player.set_state(Gst.State.READY)
        self.player.set_property("uri", path.as_uri())
        self.apply_eq()
        self.apply_volume()
        self._gen += 1
        generation = self._gen
        self.duration = 0.0
        self.clock_origin = 0.0
        self._clock_ready = False
        self.position = max(0.0, float(resume or 0))
        self.pending_seek = self.position if self.position > 1 else None
        self._discover(path, generation)
        self.playing = bool(autoplay)
        # Stay in READY until play. Pausing a Bluetooth sink can still open it
        # and run the song, so the desktop must not preroll on startup.
        self.player.set_state(Gst.State.PLAYING if autoplay else Gst.State.READY)
        self.cfg["path"] = str(path)
        self.touch_save()
        self.queue_draw()
        log(("play " if autoplay else "ready ") + path.name)

    def _on_bus(self, _bus, msg) -> bool:
        kind = msg.type
        if kind == Gst.MessageType.ERROR:
            err, _debug = msg.parse_error()
            self.playing = False
            self.flash(err.message or "Can't play that song")
        elif kind == Gst.MessageType.EOS:
            self._on_eos()
        elif kind == Gst.MessageType.ASYNC_DONE:
            self._on_ready()
        elif kind == Gst.MessageType.STATE_CHANGED and msg.src == self.player:
            _old, new, _pending = msg.parse_state_changed()
            self.playing = new == Gst.State.PLAYING
            self.queue_draw()
        return True

    def _discover(self, path: Path, generation: int) -> None:
        def work() -> None:
            seconds = 0.0
            try:
                finder = GstPbutils.Discoverer.new(4 * Gst.SECOND)
                info = finder.discover_uri(path.as_uri())
                seconds = float(info.get_duration()) / Gst.SECOND
            except Exception as exc:
                log(f"duration {path.name}: {exc}")
            GLib.idle_add(self._apply_duration, generation, seconds)

        threading.Thread(target=work, daemon=True).start()

    def _apply_duration(self, generation: int, seconds: float) -> bool:
        if generation != self._gen or self._closed:
            return False
        if seconds > 1 and (self.duration <= 0 or abs(self.duration - seconds) > 1):
            self.duration = seconds
            self.queue_draw()
        return False

    def _note_clock(self, raw: float) -> None:
        """Turn the pipeline clock into seconds from the start of the song.

        A few files report the start many seconds along. That offset is the
        zero point, and it is not progress.
        """
        if not self._clock_ready:
            self._clock_ready = True
            self.clock_origin = raw if raw > 1.5 else 0.0
            if self.pending_seek is not None:
                resume = float(self.pending_seek)
                self.pending_seek = None
                if resume > 1:
                    self.seek_to(resume, user=False)
                    return
            self.position = 0.0
            return
        self.position = max(0.0, raw - self.clock_origin)

    def _on_ready(self) -> None:
        return

    def _on_eos(self) -> None:
        if not self.tracks:
            return
        if self.repeat == "one":
            self.seek_to(0)
            self.position = 0
            if self.player is not None:
                self.player.set_state(Gst.State.PLAYING)
            self.playing = True
            return
        at_end = (not self.shuffle) and self.index >= len(self.tracks) - 1
        if self.repeat == "off" and at_end:
            self.playing = False
            self.position = 0
            self.seek_to(0)
            if self.player is not None:
                self.player.set_state(Gst.State.PAUSED)
            self.queue_draw()
            return
        self.step(1, True)

    def seek_to(self, seconds: float, user: bool = True) -> None:
        if self.player is None:
            return
        seconds = max(0.0, float(seconds))
        raw = seconds + (self.clock_origin if self._clock_ready else 0.0)
        try:
            self.player.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                int(raw * Gst.SECOND),
            )
        except Exception as exc:
            log(f"seek failed {exc}")
        self.position = seconds
        if user:
            self.pending_seek = None

    def preview_seek(self, frac: float) -> None:
        if self.duration <= 0:
            return
        self.position = min(self.duration, max(0.0, frac * self.duration))
        now = time.monotonic()
        if now - self._seek_at >= 0.12:
            self._seek_at = now
            self.seek_to(self.position)

    def toggle(self) -> None:
        if not self.tracks:
            self.flash("No songs")
            return
        if self.player is None or not self.cfg.get("path"):
            self.load_current(True, self.position)
            return
        if self.playing:
            self.playing = False
            self.player.set_state(Gst.State.PAUSED)
            self.snapshot()
        else:
            self.playing = True
            state = self.player.get_state(0)[1]
            if state in (Gst.State.NULL, Gst.State.READY):
                self.load_current(True, self.position)
            else:
                self.player.set_state(Gst.State.PLAYING)
        self.queue_draw()

    def step(self, delta: int, autoplay: bool | None = None) -> None:
        if not self.tracks:
            self.flash("No songs")
            return
        if autoplay is None:
            autoplay = self.playing
        if delta > 0 and self.shuffle and len(self.tracks) > 1:
            self.history.append(self.index)
            self.history = self.history[-40:]
            pool = [index for index in range(len(self.tracks)) if index != self.index]
            self.index = random.choice(pool)
        else:
            if delta > 0:
                self.history.append(self.index)
                self.history = self.history[-40:]
            self.index = (self.index + delta) % len(self.tracks)
        self.load_current(bool(autoplay), 0)

    def prev(self) -> None:
        if not self.tracks:
            return
        if self.position > 3:
            self.position = 0
            self.seek_to(0)
            self.queue_draw()
            return
        if self.history:
            self.index = self.history.pop()
            self.load_current(self.playing, 0)
            return
        self.step(-1, self.playing)

    def set_volume(self, value: float) -> None:
        self.volume = clamp_unit(value, self.volume)
        if self.volume > 0 and self.mute:
            self.mute = False
        self.apply_volume()
        self.touch_save()
        self.queue_draw()

    def toggle_mute(self) -> None:
        self.mute = not self.mute
        self.apply_volume()
        self.touch_save()
        self.queue_draw()

    def set_gain(self, index: int, value: float) -> None:
        self.gains[index] = clamp_db(value)
        self.apply_eq()
        self.touch_save()
        self.queue_draw()

    def apply_preset(self, gains) -> None:
        self.gains = [clamp_db(gain) for gain in gains]
        self.apply_eq()
        self.snapshot()
        self.queue_draw()

    def play_path(self, path: Path, autoplay: bool) -> None:
        path = path.expanduser()
        try:
            path = path.resolve()
        except OSError:
            pass
        if path.is_dir():
            self.cfg["library"] = str(path)
            self.tracks = scan_library(path)
            self.index = 0
            self.history.clear()
            if not self.tracks:
                self.flash("That folder has no songs")
                self.queue_draw()
                return
            self.load_current(autoplay, 0)
            return
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXT:
            self.flash("That is not a song")
            return
        for index, track in enumerate(self.tracks):
            if track == path:
                self.index = index
                break
        else:
            self.tracks.insert(0, path)
            self.index = 0
        self.history.clear()
        self.load_current(autoplay, 0)

    def take_paths(self, paths: list[Path], autoplay: bool) -> None:
        files: list[Path] = []
        for raw in paths:
            path = raw.expanduser()
            try:
                path = path.resolve()
            except OSError:
                continue
            if path.is_dir():
                files.extend(scan_library(path))
            elif path.is_file() and path.suffix.lower() in AUDIO_EXT and not path.name.startswith("._"):
                files.append(path)
        if not files:
            self.flash("No songs there")
            return
        if len(files) == 1:
            self.play_path(files[0], autoplay)
            return
        self.tracks = files
        self.index = 0
        self.history.clear()
        self.load_current(autoplay, 0)

    def use_music_library(self) -> None:
        self.cfg["library"] = str(MUSIC)
        self._open_library()
        self.history.clear()
        self.load_current(self.playing, 0)
        self.flash(f"{len(self.tracks)} songs")

    def _tick(self) -> bool:
        if self._closed:
            return False
        seeking = self.gesture and self.gesture.get("hit") == "seek"
        if self.player is not None and not seeking:
            ok, pos = self.player.query_position(Gst.Format.TIME)
            if ok and (self.pending_seek is None or not self._clock_ready):
                self._note_clock(float(pos) / Gst.SECOND)
            ok, dur = self.player.query_duration(Gst.Format.TIME)
            if ok and dur > 0:
                self.duration = float(dur) / Gst.SECOND
        if self.playing and time.monotonic() - self._last_flush > 8:
            self.touch_save()
        self.queue_draw()
        return True

    def _pointer_target(self, hit: str, x: float, y: float) -> None:
        layout = self.layout()
        if hit.startswith("fader:"):
            index = int(hit.split(":", 1)[1])
            fader = layout["faders"][index]
            self.set_gain(index, gain_from_y(y, fader["top"], fader["bottom"]))
        elif hit == "volume":
            rect = layout["volume"]
            frac = (x - rect[0]) / max(1.0, rect[2])
            self.set_volume(frac)
        elif hit == "seek":
            rect = layout["seek"]
            frac = (x - rect[0]) / max(1.0, rect[2])
            self.preview_seek(frac)
            self.queue_draw()

    def _on_press(self, widget, event) -> bool:
        if event.type == Gdk.EventType._2BUTTON_PRESS and int(event.button) == 1:
            hit = self.hit(event.x, event.y)
            if hit.startswith("fader:"):
                self.set_gain(int(hit.split(":", 1)[1]), 0)
            return True
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if int(event.button) == 3:
            self._popup(event)
            return True
        if int(event.button) != 1:
            return False
        self.area.grab_focus()
        self._place.note_press()
        hit = self.hit(event.x, event.y)
        origin = self.get_position()
        self.gesture = {
            "hit": hit,
            "xr": float(event.x_root),
            "yr": float(event.y_root),
            "ox": int(origin[0]),
            "oy": int(origin[1]),
            "moved": False,
        }
        self.hover = hit
        widget.grab_add()
        if hit not in DRAG_HITS and hit not in {"play", "prev", "next", "speaker"}:
            self._pointer_target(hit, float(event.x), float(event.y))
        self._set_cursor(hit, dragging=False)
        self.queue_draw()
        return True

    def _on_motion(self, widget, event) -> bool:
        if self.gesture is None:
            hit = self.hit(event.x, event.y)
            if hit != self.hover:
                self.hover = hit
                self._set_cursor(hit, dragging=False)
                self.queue_draw()
            return False
        gesture = self.gesture
        dx = float(event.x_root) - gesture["xr"]
        dy = float(event.y_root) - gesture["yr"]
        if gesture["hit"] in DRAG_HITS:
            if abs(dx) > CLICK_SLOP or abs(dy) > CLICK_SLOP:
                gesture["moved"] = True
                self.move(int(gesture["ox"] + dx), int(gesture["oy"] + dy))
                self._set_cursor(gesture["hit"], dragging=True)
        elif gesture["hit"] in {"play", "prev", "next", "speaker"}:
            if abs(dx) > CLICK_SLOP or abs(dy) > CLICK_SLOP:
                gesture["moved"] = True
        else:
            self._pointer_target(gesture["hit"], float(event.x), float(event.y))
        return True

    def _on_release(self, widget, event) -> bool:
        if int(getattr(event, "button", 0)) != 1:
            return False
        gesture = self.gesture
        self.gesture = None
        if widget.has_grab():
            widget.grab_remove()
        if gesture is None:
            return False
        hit = gesture["hit"]
        if not gesture["moved"]:
            if hit == "play":
                self.toggle()
            elif hit == "prev":
                self.prev()
            elif hit == "next":
                self.step(1)
            elif hit == "speaker":
                self.toggle_mute()
        elif hit == "seek":
            self.seek_to(self.position)
        if gesture["moved"] and hit in DRAG_HITS:
            self._place.note_geometry_change()
        self.touch_save()
        self.queue_draw()
        return True

    def _on_break(self, widget, _event) -> bool:
        self.gesture = None
        if widget.has_grab():
            widget.grab_remove()
        return False

    def _on_leave(self, _widget, _event) -> bool:
        if self.gesture is None and self.hover:
            self.hover = ""
            self._set_cursor("", dragging=False)
            self.queue_draw()
        return False

    def _on_scroll(self, _widget, event) -> bool:
        direction = 0
        if event.direction == Gdk.ScrollDirection.UP:
            direction = 1
        elif event.direction == Gdk.ScrollDirection.DOWN:
            direction = -1
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            if event.delta_y < -0.35:
                direction = 1
            elif event.delta_y > 0.35:
                direction = -1
        if direction == 0:
            return False
        hit = self.hit(event.x, event.y)
        if hit.startswith("fader:"):
            index = int(hit.split(":", 1)[1])
            self.set_gain(index, self.gains[index] + direction)
        else:
            self.set_volume(self.volume + direction * 0.04)
        return True

    def _on_key(self, _widget, event) -> bool:
        name = Gdk.keyval_name(event.keyval) or ""
        if name == "space":
            self.toggle()
        elif name == "Left":
            self._nudge(-5)
        elif name == "Right":
            self._nudge(5)
        elif name == "Up":
            self.set_volume(self.volume + 0.05)
        elif name == "Down":
            self.set_volume(self.volume - 0.05)
        else:
            return False
        return True

    def _nudge(self, seconds: float) -> None:
        if self.duration <= 0 and self.player is None:
            return
        target = max(0.0, self.position + seconds)
        if self.duration > 0:
            target = min(self.duration, target)
        self.seek_to(target)
        self.queue_draw()

    def _set_cursor(self, hit: str, dragging: bool) -> None:
        if dragging and hit in DRAG_HITS:
            name = "grabbing"
        elif hit in {"play", "prev", "next", "speaker"}:
            name = "pointer"
        elif hit.startswith("fader"):
            name = "ns-resize"
        elif hit in {"seek", "volume"}:
            name = "ew-resize"
        elif hit in DRAG_HITS:
            name = "grab"
        else:
            name = "default"
        display = self.get_display()
        gdk = self.area.get_window()
        if display is None or gdk is None:
            return
        gdk.set_cursor(Gdk.Cursor.new_from_name(display, name))

    def _on_drop(self, _widget, context, _x, _y, data, _info, when) -> bool:
        paths = []
        for uri in data.get_uris() or []:
            file = Gio.File.new_for_uri(uri)
            raw = file.get_path()
            if raw:
                paths.append(Path(raw))
        self.take_paths(paths, True)
        try:
            Gtk.drag_finish(context, bool(paths), False, when)
        except Exception:
            pass
        return True

    def _popup(self, event) -> None:
        menu = Gtk.Menu()

        def add(item):
            menu.append(item)
            return item

        def command(label, callback):
            item = Gtk.MenuItem.new_with_label(label)
            item.connect("activate", lambda *_a: callback())
            return add(item)

        command("Pause" if self.playing else "Play", self.toggle)
        command("Previous", self.prev)
        command("Next", lambda: self.step(1))
        add(Gtk.SeparatorMenuItem())
        command("Open a song…", self._choose_file)
        command("Open a folder…", self._choose_folder)
        command("Music library", self.use_music_library)
        add(Gtk.SeparatorMenuItem())
        shuffle = Gtk.CheckMenuItem.new_with_label("Shuffle")
        shuffle.set_active(self.shuffle)

        def toggle_shuffle(item):
            self.shuffle = bool(item.get_active())
            self.touch_save()

        shuffle.connect("toggled", toggle_shuffle)
        add(shuffle)
        group = None
        for mode, label in (("off", "Repeat off"), ("all", "Repeat all"), ("one", "Repeat this song")):
            item = Gtk.RadioMenuItem.new_with_label([] if group is None else group.get_group(), label)
            group = item
            item.set_active(self.repeat == mode)

            def choose(radio, picked=mode):
                if radio.get_active():
                    self.repeat = picked
                    self.touch_save()

            item.connect("toggled", choose)
            add(item)
        add(Gtk.SeparatorMenuItem())
        eq = Gtk.MenuItem.new_with_label("Equalizer")
        eq_menu = Gtk.Menu()
        eq.set_submenu(eq_menu)
        for name, gains in PRESETS:
            preset = Gtk.MenuItem.new_with_label(name)
            preset.connect("activate", lambda *_a, values=gains: self.apply_preset(values))
            eq_menu.append(preset)
        add(eq)
        add(Gtk.SeparatorMenuItem())
        top = Gtk.CheckMenuItem.new_with_label("Stay above windows")
        top.set_active(bool(self.cfg.get("on_top")))

        def toggle_top(item):
            self.cfg["on_top"] = bool(item.get_active())
            self.restack()
            self.snapshot()

        top.connect("toggled", toggle_top)
        add(top)
        login = Gtk.CheckMenuItem.new_with_label("Open at login")
        login.set_active(autostart_enabled())
        login.connect("toggled", lambda item: set_autostart(bool(item.get_active())))
        add(login)
        hint = Gtk.MenuItem.new_with_label("Drag empty space · double-click a slider to zero it")
        hint.set_sensitive(False)
        add(hint)
        add(Gtk.SeparatorMenuItem())
        command("Quit", self.quit)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _choose_file(self) -> None:
        dialog = Gtk.FileChooserNative.new(
            "Open a song", self, Gtk.FileChooserAction.OPEN, "Play", "Cancel"
        )
        filt = Gtk.FileFilter()
        filt.set_name("Songs")
        for pattern in ("*.mp3", "*.wav", "*.flac", "*.ogg", "*.oga", "*.m4a", "*.aac", "*.opus", "*.aiff", "*.aif"):
            filt.add_pattern(pattern)
        dialog.add_filter(filt)
        dialog.set_current_folder(str(MUSIC if MUSIC.is_dir() else HOME))
        response = dialog.run()
        name = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT and name:
            self.play_path(Path(name), True)

    def _choose_folder(self) -> None:
        dialog = Gtk.FileChooserNative.new(
            "Open a folder of songs", self, Gtk.FileChooserAction.SELECT_FOLDER, "Play", "Cancel"
        )
        dialog.set_current_folder(str(MUSIC if MUSIC.is_dir() else HOME))
        response = dialog.run()
        name = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT and name:
            self.play_path(Path(name), True)

    def _on_delete(self, *_args) -> bool:
        self.quit()
        return True

    def quit(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.snapshot()
        except Exception:
            pass
        if self.player is not None:
            try:
                self.player.set_state(Gst.State.NULL)
            except Exception:
                pass
        release_pid()
        Gtk.main_quit()


def render_preview(path: Path) -> None:
    scale = 2
    width, height = WIN_W, WIN_H
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width * scale * 2, height * scale)
    cr = cairo.Context(surface)
    cr.scale(scale, scale)
    _paint_back(cr, 0, 0, width, height, light=False)
    _paint_back(cr, width, 0, width, height, light=True)
    view = View(
        title="Track",
        meta="2 of 8",
        time_label="0:42 / 3:10",
        position=42,
        duration=190,
        playing=True,
        volume=0.72,
        mute=False,
        gains=(8, 4, 1, -2, -5),
        hover="fader:0",
    )
    cr.save()
    draw_player(cr, width, height, view)
    cr.restore()
    cr.save()
    cr.translate(width, 0)
    draw_player(cr, width, height, view)
    cr.restore()
    path.parent.mkdir(parents=True, exist_ok=True)
    surface.write_to_png(str(path))


def _paint_back(cr: cairo.Context, x: float, y: float, w: float, h: float, light: bool) -> None:
    cr.save()
    cr.rectangle(x, y, w, h)
    if light:
        cr.set_source_rgba(0.90, 0.91, 0.89, 1)
    else:
        cr.set_source_rgba(0.06, 0.10, 0.12, 1)
    cr.fill()
    cr.restore()


def transparent_corners() -> tuple[int, int]:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIN_W, WIN_H)
    cr = cairo.Context(surface)
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)
    draw_player(
        cr,
        WIN_W,
        WIN_H,
        View("Track", "2 of 8", "0:42 / 3:10", 42, 190, True, 0.7, False, (8, 3, 0, -2, -4), "play"),
    )
    surface.flush()
    data = surface.get_data()
    stride = surface.get_stride()

    def alpha(px: int, py: int) -> int:
        return data[py * stride + px * 4 + 3]

    ink = 0
    for py in range(8, 36):
        for px in range(18, 180):
            ink = max(ink, alpha(px, py))
    return alpha(2, 2), ink


def gst_smoke() -> str:
    player = Gst.ElementFactory.make("playbin", "lee-player-test")
    eq = Gst.ElementFactory.make("equalizer-10bands", "eq-test")
    if player is None or eq is None:
        return "missing playbin or equalizer"
    player.set_property("audio-filter", eq)
    player.set_property("flags", AUDIO_FLAGS)
    player.set_property("volume", 0.0)
    player.set_property("mute", True)
    for gain, pair in zip((6, 3, 0, -2, -4), BAND_PAIRS):
        for band in pair:
            eq.set_property(f"band{band}", float(gain))
    if abs(float(eq.get_property("band0")) - 6) > 0.01:
        return "band gain did not stick"
    player.set_state(Gst.State.NULL)
    return ""


def self_test() -> int:
    failures = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        if ok:
            print(f"ok  {name}")
        else:
            failures.append(name + (f" ({detail})" if detail else ""))
            print(f"FAIL {name} {detail}")

    check("time zero", format_time(0) == "0:00")
    check("time minute", format_time(65) == "1:05")
    check("time hour", format_time(3600) == "1:00:00")
    check("db clamp", clamp_db(20) == 12 and clamp_db(-40) == -12 and clamp_db(1.2) == 1.0)
    top, bottom = 140.0, 240.0
    roundtrip = True
    for gain in (-12, -3.5, 0, 4, 12):
        back = gain_from_y(y_from_gain(gain, top, bottom), top, bottom)
        if abs(back - gain) > 0.01:
            roundtrip = False
    check("gain roundtrip", roundtrip)
    check("gain ends", gain_from_y(top, top, bottom) == 12 and gain_from_y(bottom, top, bottom) == -12)
    layout = compute_layout(WIN_W, WIN_H)
    names = [hit_test(layout, *point) for point in (
        (layout["play"][0], layout["play"][1]),
        (layout["prev"][0], layout["prev"][1]),
        (layout["next"][0], layout["next"][1]),
        (layout["seek"][0] + 10, layout["seek"][1] + 8),
        (layout["volume"][0] + 10, layout["volume"][1] + 8),
        (layout["title"][0] + 8, layout["title"][1] + 8),
    )]
    check("hits", names == ["play", "prev", "next", "seek", "volume", "title"], str(names))
    fader_hits = []
    for index, fader in enumerate(layout["faders"]):
        rect = fader["hit"]
        fader_hits.append(hit_test(layout, rect[0] + rect[2] / 2, fader["top"] + 8))
    check("fader order", fader_hits == [f"fader:{i}" for i in range(5)] and layout["faders"][0]["name"] == "Bass", str(fader_hits))
    root = Path("/tmp/lee-player-scan-test")
    if root.exists():
        for child in sorted(root.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
    (root / "ok").mkdir(parents=True)
    (root / "ok" / "Keep Me.mp3").write_bytes(b"")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "skip.mp3").write_bytes(b"")
    (root / "venv").mkdir()
    (root / "venv" / "skip.wav").write_bytes(b"")
    (root / "._ghost.mp3").write_bytes(b"")
    found = [path.name for path in scan_library(root)]
    check("scan skip", found == ["Keep Me.mp3"], str(found))
    try:
        item = Gtk.RadioMenuItem.new_with_label([], "Repeat off")
        other = Gtk.RadioMenuItem.new_with_label(item.get_group(), "Repeat all")
        check("menu", other.get_group() is not None)
    except Exception as exc:
        check("menu", False, str(exc))
    check("db label", db_label(0) == "0" and db_label(8) == "+8" and db_label(-3.5) == "-3.5")
    corner, ink = transparent_corners()
    check("clear corner", corner == 0, str(corner))
    check("painted ink", ink > 0, str(ink))
    try:
        render_preview(Path("/tmp/lee-player-preview.png"))
        check("preview", Path("/tmp/lee-player-preview.png").stat().st_size > 1000)
    except Exception as exc:
        check("preview", False, str(exc))
    smoke = gst_smoke()
    check("gstreamer", smoke == "", smoke)
    if failures:
        print("FAILED " + ", ".join(failures))
        return 1
    print("all ok")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    if "--preview" in sys.argv:
        dest = Path("/tmp/lee-player-preview.png")
        if "--preview" in sys.argv:
            index = sys.argv.index("--preview")
            if index + 1 < len(sys.argv) and not sys.argv[index + 1].startswith("-"):
                dest = Path(sys.argv[index + 1])
        render_preview(dest)
        print(dest)
        return 0
    GLib.set_prgname(PRGNAME)
    GLib.set_application_name("Music Player")
    try:
        Gdk.set_program_class(PRGNAME)
    except Exception:
        pass
    claim_pid("--replace" in sys.argv)
    try:
        write_icon()
        install_launchers()
        cfg = load_cfg()
        win = PlayerWindow(cfg)
        paths = extra_paths(sys.argv[1:])
        win.show_all()
        if paths:
            win.take_paths(paths, True)
        elif win.tracks:
            win.load_current(False, cfg["position"])
        else:
            win.queue_draw()
        log(f"started pid {os.getpid()} {len(win.tracks)} songs")
        Gtk.main()
    finally:
        release_pid()
    return 0


if __name__ == "__main__":
    sys.exit(main())
