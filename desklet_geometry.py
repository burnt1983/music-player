"""Remember a GTK3 desklet's size and screen position across login.

GNOME Wayland does not let a native window place itself, so these desklets
open through XWayland (GDK_BACKEND=x11), where move() and resize() work.
Call prefer_xwayland() before importing Gdk or Gtk.

A restore is applied again for a few seconds after map, because the session
often places a new window once more after it first appears. A later drag or
resize is written out. A click that does not move the window is not.

The four desktop widgets stay in the normal layer: above the desktop icons
and behind ordinary windows. keep_below puts them under Desktop Icons NG,
so a real click never arrives. Mutter also ignores begin_move_drag and
begin_resize_drag for these windows, so move and resize follow the pointer
in the client. Utility and keep-above hints cover ordinary windows, and a
window that refuses focus cannot be dragged.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from typing import Callable, Optional, Tuple

Geo = Tuple[Optional[int], Optional[int], int, int]
LoadFn = Callable[[], Optional[Geo]]
SaveFn = Callable[[int, int, int, int], None]

RETRY_MS = (200, 400, 700, 1200, 2000, 3000, 4500)
SINK_MS = (50, 400, 1500, 5000)

# Windows whose own drag is in progress. The X core button mask is not a
# drag: XWayland's relative pointer can sit with button 1 down forever.
_gestures: set = set()

_WEST = {"west", "north-west", "south-west"}
_EAST = {"east", "north-east", "south-east"}
_NORTH = {"north", "north-west", "north-east"}
_SOUTH = {"south", "south-west", "south-east"}
_EDGE_NICKS = (
    ("north-west", "nwse-resize"),
    ("south-east", "nwse-resize"),
    ("north-east", "nesw-resize"),
    ("south-west", "nesw-resize"),
    ("east", "ew-resize"),
    ("west", "ew-resize"),
    ("north", "ns-resize"),
    ("south", "ns-resize"),
)


def prefer_xwayland(max_wait_s: float = 25.0) -> str:
    """Use XWayland so a desklet can reopen on the same pixels. Call first."""
    if os.environ.get("DESKLET_NATIVE_WAYLAND"):
        os.environ["GDK_BACKEND"] = "wayland"
        return "wayland"
    forced = os.environ.get("GDK_BACKEND")
    if forced and forced != "x11":
        return forced
    display = os.environ.get("DISPLAY") or ":0"
    os.environ["DISPLAY"] = display
    screen = display.split(":")[-1].split(".")[0]
    if not screen.isdigit():
        screen = "0"
    sock = f"/tmp/.X11-unix/X{screen}"
    deadline = time.time() + max_wait_s
    while not os.path.exists(sock):
        if time.time() >= deadline:
            sys.stderr.write(
                "desklet: XWayland is not up; this window cannot restore its position\n"
            )
            return forced or "wayland"
        time.sleep(0.2)
    os.environ["GDK_BACKEND"] = "x11"
    return "x11"


def user_moved(
    appeared: Optional[Tuple[int, int, int, int]],
    saved: Optional[Geo],
    rect: Optional[Tuple[int, int, int, int]],
    tol: int = 8,
) -> bool:
    """True when a configure left both the compositor's placement and our restore.

    A click, the first map, and our own move() must not be stored as a new
    position. Those are what were putting the desklets back in the wrong place
    after login.
    """
    if rect is None:
        return False
    if appeared is not None and _close(appeared, rect, tol):
        return False
    if saved is not None and saved[0] is not None and _close(saved, rect, tol):
        return False
    return True


def prepare_desktop_window(window) -> None:
    """Prepare a desklet before it is shown.

    The window type has to be NORMAL. UTILITY and DOCK are stacked above
    ordinary windows on GNOME, and a desktop-type window cannot be dragged.
    Focus must stay accepted or a drag never starts. Mapping does not take
    focus, so the desklet does not pop over the current window. keep_below
    stays off: that layer is under the desktop icons.
    """
    from gi.repository import Gdk

    window.set_decorated(False)
    window.set_type_hint(Gdk.WindowTypeHint.NORMAL)
    window.set_skip_taskbar_hint(True)
    window.set_skip_pager_hint(True)
    window.set_accept_focus(True)
    window.set_focus_on_map(False)
    window.set_keep_above(False)
    window.set_keep_below(False)
    try:
        window.stick()
    except Exception:
        pass


def edge_name_at(x: float, y: float, width: int, height: int, grip: int) -> str:
    """Edge or corner name under a pointer, or '' in the interior."""
    if width < 2 or height < 2:
        return ""
    left = x <= grip
    right = x >= width - grip
    top = y <= grip
    bottom = y >= height - grip
    if top and left:
        return "north-west"
    if top and right:
        return "north-east"
    if bottom and left:
        return "south-west"
    if bottom and right:
        return "south-east"
    if left:
        return "west"
    if right:
        return "east"
    if top:
        return "north"
    if bottom:
        return "south"
    return ""


def apply_rect(
    edge: str,
    ox: int,
    oy: int,
    ow: int,
    oh: int,
    dx: int,
    dy: int,
    min_w: int,
    min_h: int,
    square: bool = False,
) -> Tuple[int, int, int, int]:
    """Client-side move or resize. West and north drags move the origin."""
    if not edge:
        return int(ox + dx), int(oy + dy), int(ow), int(oh)
    x, y, w, h = ox, oy, ow, oh
    if edge in _EAST:
        w = ow + dx
    if edge in _SOUTH:
        h = oh + dy
    if edge in _WEST:
        w = ow - dx
    if edge in _NORTH:
        h = oh - dy
    w = max(int(min_w), int(w))
    h = max(int(min_h), int(h))
    if square:
        horizontal = edge in _EAST or edge in _WEST
        vertical = edge in _NORTH or edge in _SOUTH
        if horizontal and not vertical:
            h = w
        elif vertical and not horizontal:
            w = h
        else:
            side = max(w, h)
            w = h = side
        w = max(int(min_w), int(w))
        h = max(int(min_h), int(h))
    if edge in _WEST:
        x = ox + ow - w
    if edge in _NORTH:
        y = oy + oh - h
    return int(x), int(y), int(w), int(h)


_nudge = None


def _tooltip_nudge():
    """One off-screen tooltip. Mapping it makes the icon sheet drop.

    Do not destroy it. Destroying a window whose type is below a dropdown
    menu makes Desktop Icons NG focus whatever is on top.
    """
    global _nudge
    from gi.repository import Gdk, Gtk

    if _nudge is not None:
        return _nudge
    nudge = Gtk.Window(type=Gtk.WindowType.POPUP)
    nudge.set_type_hint(Gdk.WindowTypeHint.TOOLTIP)
    nudge.set_decorated(False)
    nudge.set_skip_taskbar_hint(True)
    nudge.set_skip_pager_hint(True)
    nudge.set_accept_focus(False)
    nudge.set_focus_on_map(False)
    nudge.set_can_focus(False)
    nudge.set_default_size(1, 1)
    nudge.set_keep_above(False)
    nudge.set_keep_below(False)
    _nudge = nudge
    return nudge


def sink_above_icons(window) -> bool:
    """Sit just above the desktop icons and under ordinary windows.

    Lowering parks the desklet at the bottom of the normal layer, which is
    still under the icon sheet. Mapping the invisible nudge makes that sheet
    lower itself. Safe as a GLib callback. Returns False always.
    """
    if id(window) in _gestures:
        return False
    try:
        window.set_keep_above(False)
        window.set_keep_below(False)
        gdkwin = window.get_window()
        if gdkwin is not None:
            gdkwin.set_keep_above(False)
            gdkwin.set_keep_below(False)
            gdkwin.set_urgency_hint(False)
        window.set_urgency_hint(False)
        # Gtk.Window has no lower(); the GDK window does.
        if gdkwin is not None:
            gdkwin.lower()
        display = window.get_display()
        display.sync()
        nudge = _tooltip_nudge()
        nudge.move(-40, -40)
        nudge.hide()
        display.sync()
        nudge.show()
        nudge.set_opacity(0)
        nudge_gdk = nudge.get_window()
        if nudge_gdk is not None:
            nudge_gdk.set_opacity(0)
        display.sync()
    except Exception:
        pass
    return False


def schedule_sinks(window) -> None:
    from gi.repository import GLib

    GLib.idle_add(sink_above_icons, window)
    for delay in SINK_MS:
        GLib.timeout_add(delay, sink_above_icons, window)


def repin_desktop(window) -> bool:
    """Restack above the icons and under ordinary windows."""
    return sink_above_icons(window)


def bind_desktop_pin(window) -> None:
    """Schedule the same restack. Does not set keep-below."""
    schedule_sinks(window)


class DeskletChrome:
    """Move, resize, and restack one desktop widget.

    Attach after prepare_desktop_window. Button 1 in the interior moves the
    window. The same button on an edge or corner resizes it. Other buttons
    are left for the widget (menus, next photo). A click that stays inside
    click_slop calls on_click instead of leaving the window shifted.
    """

    def __init__(
        self,
        window,
        *,
        min_w: int,
        min_h: int,
        grip: int = 12,
        click_slop: float = 0,
        square: bool = False,
        on_click: Optional[Callable] = None,
        hit_test: Optional[Callable] = None,
        constrain: Optional[Callable] = None,
        on_done: Optional[Callable] = None,
        placement: Optional["DeskletPlacement"] = None,
        manage_cursor: bool = True,
        trace: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.window = window
        self.min_w = int(min_w)
        self.min_h = int(min_h)
        self.grip = int(grip)
        self.click_slop = float(click_slop)
        self.square = bool(square)
        self.on_click = on_click
        self.hit_test = hit_test
        self.constrain = constrain
        self.on_done = on_done
        self.placement = placement
        self.manage_cursor = manage_cursor
        self.trace = trace
        self._gesture = None
        self._edges = None
        window.connect("map-event", self._on_map)
        window.connect("window-state-event", self._on_state)
        self.watch(window)

    def restack(self) -> None:
        from gi.repository import GLib

        GLib.idle_add(self._sink)
        for delay in SINK_MS:
            GLib.timeout_add(delay, self._sink)

    def watch(self, widget) -> None:
        from gi.repository import Gdk

        widget.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.BUTTON1_MOTION_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        widget.connect("button-press-event", self._press)
        widget.connect("button-release-event", self._release)
        widget.connect("motion-notify-event", self._motion)
        widget.connect("leave-notify-event", self._leave)

    def _edge_table(self):
        if self._edges is not None:
            return self._edges
        from gi.repository import Gdk

        self._edges = {
            "north": Gdk.WindowEdge.NORTH,
            "south": Gdk.WindowEdge.SOUTH,
            "east": Gdk.WindowEdge.EAST,
            "west": Gdk.WindowEdge.WEST,
            "north-west": Gdk.WindowEdge.NORTH_WEST,
            "north-east": Gdk.WindowEdge.NORTH_EAST,
            "south-west": Gdk.WindowEdge.SOUTH_WEST,
            "south-east": Gdk.WindowEdge.SOUTH_EAST,
        }
        return self._edges

    def _edge_at(self, widget, event):
        width = int(widget.get_allocated_width())
        height = int(widget.get_allocated_height())
        if width < 2 or height < 2:
            width, height = self.window.get_size()
        name = edge_name_at(float(event.x), float(event.y), int(width), int(height), self.grip)
        if not name:
            return None
        return self._edge_table().get(name)

    def _nick(self, edge) -> str:
        if edge is None:
            return ""
        for name, value in self._edge_table().items():
            if edge == value:
                return name
        return ""

    def _set_cursor(self, widget, edge) -> None:
        name = "default"
        nick = self._nick(edge)
        for candidate, cursor_name in _EDGE_NICKS:
            if candidate == nick:
                name = cursor_name
                break
        display = self.window.get_display()
        if display is None:
            return
        from gi.repository import Gdk

        cursor = Gdk.Cursor.new_from_name(display, name)
        for owner in (widget, self.window):
            gdkwin = owner.get_window()
            if gdkwin is not None:
                gdkwin.set_cursor(cursor)

    def _press(self, widget, event) -> bool:
        from gi.repository import Gdk

        if event.type != Gdk.EventType.BUTTON_PRESS or int(event.button) != 1:
            return False
        if self.placement is not None:
            self.placement.note_press()
        edge = self._edge_at(widget, event)
        hit = None
        if edge is None and self.hit_test is not None:
            try:
                hit = self.hit_test(widget, event)
            except Exception:
                hit = None
        ox, oy = self.window.get_position()
        ow, oh = self.window.get_size()
        self._gesture = {
            "edge": edge,
            "xr": float(event.x_root),
            "yr": float(event.y_root),
            "ox": int(ox),
            "oy": int(oy),
            "ow": int(ow),
            "oh": int(oh),
            "hit": hit,
            "moved": False,
            "traced": False,
        }
        _gestures.add(id(self.window))
        if self.trace is not None:
            kind = "resize" if edge is not None else "move"
            self.trace(
                f"press {kind} xy={float(event.x):.1f},{float(event.y):.1f} "
                f"root={float(event.x_root):.1f},{float(event.y_root):.1f} "
                f"state={int(event.state)}"
            )
        return True

    def _motion(self, widget, event) -> bool:
        gesture = self._gesture
        if gesture is None:
            if self.manage_cursor:
                self._set_cursor(widget, self._edge_at(widget, event))
            return False
        dx = abs(float(event.x_root) - gesture["xr"])
        dy = abs(float(event.y_root) - gesture["yr"])
        if (
            gesture["edge"] is None
            and self.click_slop > 0
            and dx <= self.click_slop
            and dy <= self.click_slop
        ):
            return True
        self._apply(float(event.x_root), float(event.y_root))
        if self.trace is not None and not gesture["traced"]:
            gesture["traced"] = True
            kind = "resize" if gesture["edge"] is not None else "move"
            self.trace(f"motion {kind} state={int(event.state)}")
        return True

    def _apply(self, x_root: float, y_root: float) -> None:
        gesture = self._gesture
        if gesture is None:
            return
        dx = int(round(x_root - gesture["xr"]))
        dy = int(round(y_root - gesture["yr"]))
        nick = self._nick(gesture["edge"])
        x, y, w, h = apply_rect(
            nick,
            gesture["ox"],
            gesture["oy"],
            gesture["ow"],
            gesture["oh"],
            dx,
            dy,
            self.min_w,
            self.min_h,
            self.square,
        )
        if nick and self.constrain is not None:
            try:
                x, y, w, h = self.constrain(
                    x, y, w, h, gesture["edge"],
                    gesture["ox"], gesture["oy"], gesture["ow"], gesture["oh"],
                )
            except Exception:
                pass
        self.window.resize(int(w), int(h))
        self.window.move(int(x), int(y))
        gesture["moved"] = True

    def _release(self, widget, event) -> bool:
        if getattr(event, "button", None) != 1:
            return False
        gesture = self._gesture
        self._gesture = None
        _gestures.discard(id(self.window))
        if gesture is not None:
            dx = abs(float(event.x_root) - gesture["xr"])
            dy = abs(float(event.y_root) - gesture["yr"])
            stayed = dx <= self.click_slop and dy <= self.click_slop
            if gesture["edge"] is None and stayed and self.on_click is not None:
                try:
                    self.on_click(widget, event, gesture["hit"])
                except Exception:
                    pass
            elif gesture["moved"] and self.placement is not None:
                self.placement.note_geometry_change()
            if self.on_done is not None and gesture["moved"]:
                try:
                    self.on_done()
                except Exception:
                    pass
            if self.trace is not None and (gesture["moved"] or gesture["traced"]):
                self.trace("release")
        from gi.repository import GLib

        GLib.idle_add(self._sink)
        for delay in SINK_MS:
            GLib.timeout_add(delay, self._sink)
        return False

    def _leave(self, widget, _event) -> bool:
        if self._gesture is None and self.manage_cursor:
            self._set_cursor(widget, None)
        return False

    def _on_map(self, *_args) -> bool:
        if self.trace is not None:
            self.trace("map")
        self.restack()
        return False

    def _on_state(self, _widget, event) -> bool:
        from gi.repository import Gdk

        if int(event.new_window_state) & int(Gdk.WindowState.BELOW):
            self.window.set_keep_below(False)
            gdkwin = self.window.get_window()
            if gdkwin is not None:
                gdkwin.set_keep_below(False)
            self.restack()
        return False

    def _sink(self) -> bool:
        held = id(self.window) in _gestures
        if self.trace is not None:
            self.trace(f"sink-enter held={int(held)}")
        sink_above_icons(self.window)
        if self.trace is not None and not held:
            gdkwin = self.window.get_window()
            state = int(gdkwin.get_state()) if gdkwin is not None else 0
            self.trace(f"sink state={state}")
        return False


def _close(a: Geo, b: Tuple[int, int, int, int], tol: int) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if abs(aw - bw) > tol or abs(ah - bh) > tol:
        return False
    if ax is None or ay is None:
        return True
    return abs(ax - bx) <= tol and abs(ay - by) <= tol


class DeskletPlacement:
    def __init__(
        self,
        window,
        load: LoadFn,
        save: SaveFn,
        *,
        min_w: int = 80,
        min_h: int = 80,
        on_quit: Optional[Callable[[], None]] = None,
    ) -> None:
        from gi.repository import GLib

        self.win = window
        self._load = load
        self._save = save
        self.min_w = min_w
        self.min_h = min_h
        self._on_quit = on_quit
        self._GLib = GLib
        self.saved: Optional[Geo] = _valid(load(), min_w, min_h)
        self.hold = True
        self.force_save = False
        self._dirty = False
        self._stop_retry = False
        self._save_id = None
        self._press_until = 0.0
        self._appeared: Optional[Tuple[int, int, int, int]] = None
        window.connect("map-event", self._on_map)
        window.connect("configure-event", self._on_configure)
        window.connect_after("button-release-event", self._on_release)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGHUP, self._on_signal)

    def watch(self, widget) -> None:
        """Notice a press on a child that stops the event from reaching the window."""
        widget.connect("button-press-event", self._on_press)
        widget.connect("scroll-event", self._on_press)
        widget.connect("key-press-event", self._on_press)

    def hook_titlebar(self) -> None:
        bar = self.win.get_titlebar()
        if bar is not None:
            bar.connect("button-press-event", self._on_press)

    def apply_before_show(self) -> None:
        geo = self.saved
        if not geo:
            return
        _x, _y, w, h = geo
        self.win.set_default_size(int(w), int(h))
        if geo[0] is not None and geo[1] is not None:
            try:
                self.win.move(int(geo[0]), int(geo[1]))
            except Exception:
                pass

    def apply_now(self) -> None:
        geo = self.saved
        if not geo or self._stop_retry:
            return
        x, y, w, h = geo
        try:
            self.win.resize(int(w), int(h))
            if x is not None and y is not None:
                self.win.move(int(x), int(y))
        except Exception:
            pass

    def note_press(self) -> None:
        self._press_until = time.monotonic() + 3.0

    def note_geometry_change(self) -> None:
        """A keyboard or scroll resize, where no mouse button is held."""
        self.force_save = True
        self._stop_retry = True
        self.hold = False
        self._schedule_save()

    def _on_press(self, *_a) -> bool:
        self.note_press()
        return False

    def _on_release(self, *_a) -> bool:
        if self._dirty or time.monotonic() < self._press_until:
            self._schedule_save()
        return False

    def _gesture(self) -> bool:
        return self.force_save or time.monotonic() < self._press_until or self._button_held()

    def _button_held(self) -> bool:
        # A stuck X core button mask is not a drag of this window.
        return id(self.win) in _gestures

    def _on_map(self, *_a):
        # The first position is the compositor's, not the user's. Read it
        # before move(), or a later click gets stored as the saved corner.
        self._appeared = self._read()
        self.apply_now()
        self._arm(0)
        return False

    def _arm(self, index: int) -> None:
        if index >= len(RETRY_MS) or self._stop_retry:
            self.hold = False
            return
        self._GLib.timeout_add(RETRY_MS[index], self._step, index)

    def _step(self, index: int) -> bool:
        if self._stop_retry:
            self.hold = False
            return False
        if self._button_held() and user_moved(self._appeared, self.saved, self._read()):
            self._stop_retry = True
            self.hold = False
            self.note_press()
            self._schedule_save()
            return False
        self.apply_now()
        if self.matches():
            self.hold = False
            return False
        self._arm(index + 1)
        return False

    def matches(self, tol: int = 24) -> bool:
        geo = self.saved
        rect = self._read()
        if geo is None or rect is None:
            return False
        return _close(geo, rect, tol)

    def _on_configure(self, *_a) -> bool:
        rect = self._read()
        if self.force_save or (self._gesture() and user_moved(self._appeared, self.saved, rect)):
            self._dirty = True
            self._stop_retry = True
            self.hold = False
            self._schedule_save()
        return False

    def _schedule_save(self) -> None:
        if self._save_id is not None:
            self._GLib.source_remove(self._save_id)
        self._save_id = self._GLib.timeout_add(400, self._persist)

    def _read(self) -> Optional[Tuple[int, int, int, int]]:
        try:
            x, y = self.win.get_position()
            w, h = self.win.get_size()
            if int(w) < 2 or int(h) < 2:
                return None
            return int(x), int(y), int(w), int(h)
        except Exception:
            return None

    def _persist(self) -> bool:
        self._save_id = None
        rect = self._read()
        if rect is None or rect[2] < self.min_w or rect[3] < self.min_h:
            return False
        if not self._dirty and not self._gesture() and not self.force_save:
            return False
        if self.saved and self.saved[0] is not None and _close(self.saved, rect, 12):
            self.force_save = False
            self._dirty = False
            return False
        # Still on the compositor's first placement: a click, not a drag.
        if (
            self._appeared is not None
            and _close(self._appeared, rect, 12)
            and not self.force_save
        ):
            self._dirty = False
            return False
        x, y, w, h = rect
        try:
            self._save(x, y, w, h)
        except Exception:
            return False
        self.saved = (x, y, w, h)
        self.force_save = False
        self._dirty = False
        return False

    def _on_signal(self, *_a) -> None:
        def finish() -> bool:
            if self._on_quit is not None:
                self._on_quit()
            else:
                from gi.repository import Gtk

                Gtk.main_quit()
            return False

        try:
            self._GLib.idle_add(finish)
        except Exception:
            pass


def _valid(geo: Optional[Geo], min_w: int, min_h: int) -> Optional[Geo]:
    if not geo or len(geo) < 4:
        return None
    x, y, w, h = geo[0], geo[1], geo[2], geo[3]
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        return None
    if w < min_w or h < min_h:
        return None
    if x is not None:
        try:
            x = int(x)
        except (TypeError, ValueError):
            x = None
    if y is not None:
        try:
            y = int(y)
        except (TypeError, ValueError):
            y = None
    if x is not None and abs(x) > 20000:
        x = None
    if y is not None and abs(y) > 20000:
        y = None
    return x, y, w, h


if __name__ == "__main__":
    appeared = (10, 10, 100, 100)
    saved = (400, 200, 100, 100)
    assert user_moved(appeared, saved, None) is False
    assert user_moved(appeared, saved, (12, 11, 100, 100)) is False
    assert user_moved(appeared, saved, (402, 198, 100, 100)) is False
    assert user_moved(appeared, saved, (80, 90, 100, 100)) is True
    assert user_moved(None, None, (80, 90, 100, 100)) is True
    assert edge_name_at(3, 3, 100, 80, 12) == "north-west"
    assert edge_name_at(50, 40, 100, 80, 12) == ""
    assert edge_name_at(95, 40, 100, 80, 12) == "east"
    assert apply_rect("", 10, 20, 100, 80, 5, -3, 40, 40) == (15, 17, 100, 80)
    assert apply_rect("east", 10, 20, 100, 80, 15, 0, 40, 40) == (10, 20, 115, 80)
    assert apply_rect("west", 10, 20, 100, 80, 15, 0, 40, 40) == (25, 20, 85, 80)
    assert apply_rect("north", 10, 20, 100, 80, 0, 10, 40, 40) == (10, 30, 100, 70)
    assert apply_rect("west", 10, 20, 100, 50, 80, 0, 40, 40) == (70, 20, 40, 50)
    assert apply_rect("south-east", 0, 0, 100, 100, 20, 10, 50, 50, True) == (0, 0, 120, 120)
    assert apply_rect("west", 0, 0, 200, 200, 40, 0, 50, 50, True) == (40, 0, 160, 160)
    print("user_moved ok")
