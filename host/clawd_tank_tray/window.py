"""The Clawd Tank flyout (tkinter, main thread only).

A borderless popup like the ones Windows 11 opens from its own tray icons, not a
window: no title bar, no buttons and no taskbar button (a tool window), rounded
corners, a thin border and a shadow (DWM, see winutil.style_flyout), placed next to
the notification area (flyout.place_flyout). A click on the tray icon toggles it;
it closes when focus goes anywhere else, and on Escape (flyout.FlyoutGuard decides,
with the short guards that keep the click on the icon from closing and reopening
it). Quit is the only way out of the app.

Inside, like a menu flyout: the small Clawd mark, the title and the status dot;
the status lines (Bar, Hooks, Claude Code, Last event); a red message with a Retry
row when something is wrong; then full-width menu rows with a hover highlight (not
buttons). Light or dark like Windows (theme.py), read again each time it opens.

No timer runs while it is hidden or still. Opening it takes ~120 ms of fade and
slide (a short chain of ``after`` calls that ends by itself), skipped when Windows'
animation effects are off.
"""

import base64
import io
import logging
import time
import tkinter as tk
from tkinter import font as tkfont
from typing import Callable, Dict, List, Optional, Tuple

from . import APP_NAME
from . import flyout as fl
from . import theme as th
from . import winutil
from .icons import SIZES, IconSet
from .state import ICON_CONNECTED, ICON_ERROR, ViewState

logger = logging.getLogger("clawd-tank.tray.window")

BASE_WIDTH = 300        # px at 100 % scaling, border included; the height follows the content
PAD_X = 16              # left and right of the header and the status lines
LINE_NAME_PX = 80       # the column of line names (Bar, Hooks, ...)
ROW_PX = 36             # height of a menu row
ROW_INSET = 4           # a row's highlight stops this far from the flyout's sides
ROW_TEXT_PAD = 12       # from the highlight's edge to the row's text (and to its check mark)
MARK_PX = 20            # the Clawd mark in the header
DOT_PX = 10
ROW_CURSOR = "hand2"

RED = "#b3261e"
# The status dot: the same three states as the tray icon.
DOT_COLOURS = {ICON_CONNECTED: "#2e8b57", ICON_ERROR: RED}
DOT_OTHER = "#9aa0a6"

ANIM_STEPS = 7          # the opening animation: 7 steps of 16 ms, ~120 ms
ANIM_MS = 16

# Rows, top to bottom: (key, text, closes the flyout first). The ones that open a
# dialog or another program close it first, so nothing is left hanging over them;
# Start with Windows and Retry stay open so the change can be seen.
ACTION_ROWS = (("install", "Install or repair hooks", True),
               ("remove", "Remove hooks", True),
               ("open_log", "Open log", True),
               ("toggle_autostart", "Start with Windows", False))
QUIT_ROW = ("quit", "Quit", True)
RETRY_ROW = ("retry", "Retry", False)


def closest_size(want: int, sizes=SIZES) -> int:
    """The built icon size nearest to `want` px (the mark is pixel art: a size that
    is built is crisp, one that is stretched is not)."""
    return min(sizes, key=lambda s: (abs(s - want), s))


class _Row:
    """One menu row: a full-width strip with the text on the left (and, for Start
    with Windows, a check mark on the right). Hover and press colour the whole
    strip; releasing the button inside it runs the action."""

    def __init__(self, win: "FlyoutWindow", parent: tk.Misc, text: str, key: str, *,
                 closes: bool, check: bool = False):
        s = win.scale
        self.win, self.key, self.closes = win, key, closes
        self.state = "normal"      # "normal", "hover" (pointer or keys) or "pressed"
        self.checked = False
        self.frame = tk.Frame(parent, height=round(ROW_PX * s), bd=0, highlightthickness=0,
                              cursor=ROW_CURSOR)
        self.frame.pack_propagate(False)
        self.label = tk.Label(self.frame, text=text, font=win.fonts["row"], anchor="w", bd=0,
                              cursor=ROW_CURSOR)
        self.label.pack(side="left", padx=(round(ROW_TEXT_PAD * s), 0))
        self.check: Optional[tk.Label] = None
        if check:
            self.check = tk.Label(self.frame, text=win.check_glyph, font=win.fonts["check"], bd=0,
                                  cursor=ROW_CURSOR)
        self.widgets = [w for w in (self.frame, self.label, self.check) if w is not None]
        for w in self.widgets:
            w.bind("<Enter>", self._enter)
            w.bind("<Leave>", self._leave)
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<ButtonRelease-1>", self._release)

    def pack(self) -> None:
        self.frame.pack(fill="x", padx=round(ROW_INSET * self.win.scale), pady=1)

    def pack_forget(self) -> None:
        self.frame.pack_forget()

    @property
    def shown(self) -> bool:
        return bool(self.frame.winfo_manager())

    def set_checked(self, on: bool) -> None:
        if self.check is None or on == self.checked:
            return
        self.checked = on
        if on:
            self.check.pack(side="right", padx=(0, round(ROW_TEXT_PAD * self.win.scale)))
        else:
            self.check.pack_forget()

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.paint()

    def paint(self) -> None:
        theme = self.win.theme
        bg = {"normal": theme.background, "hover": theme.hover, "pressed": theme.pressed}[self.state]
        self.frame.configure(bg=bg)
        self.label.configure(bg=bg, fg=theme.text)
        if self.check is not None:
            self.check.configure(bg=bg, fg=theme.text)

    def invoke(self) -> None:
        self.win._activate(self)

    # --- the pointer -----------------------------------------------------------------------

    def _inside(self, event) -> bool:
        """Is the pointer still over this row (the frame or any of its parts)? Tk sends
        Leave to the frame when the pointer moves onto its own label, so the widget
        under the pointer is looked up instead of trusting the event."""
        under = self.win.widget_at(event.x_root, event.y_root)
        return under is not None and any(under is w for w in self.widgets)

    def _enter(self, _event) -> None:
        self.win._set_current(self)

    def _leave(self, event) -> None:
        if not self._inside(event):
            self.win._clear_current(self)

    def _press(self, _event) -> None:
        self.set_state("pressed")

    def _release(self, event) -> None:
        w = event.widget
        inside = 0 <= event.x < w.winfo_width() and 0 <= event.y < w.winfo_height()
        self.set_state("hover" if inside else "normal")
        if inside:
            self.invoke()


class FlyoutWindow:
    """`actions` maps row names to callables: install, remove, open_log,
    toggle_autostart, quit, retry. All run on the UI thread.

    Everything that touches the system can be replaced, so the window runs in a test
    without moving, focusing or showing anything: `theme_source` (-> th.Theme),
    `autostart` (-> bool, Start with Windows is on), `placer` ((w, h), scale ->
    fl.Placement), `clock`, `animate` (None: follow Windows), `opacity` (the final
    alpha; 0 keeps the window invisible), `take_focus`."""

    def __init__(self, root: tk.Tk, icons: IconSet, actions: Dict[str, Callable[[], None]],
                 scale: float = 1.0, *,
                 theme_source: Optional[Callable[[], th.Theme]] = None,
                 autostart: Optional[Callable[[], bool]] = None,
                 placer: Optional[Callable[[Tuple[int, int], float], fl.Placement]] = None,
                 before_show: Optional[Callable[[], None]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 animate: Optional[bool] = None, opacity: float = 1.0, take_focus: bool = True):
        self.root = root
        self.icons = icons
        self.actions = actions
        self.scale = max(1.0, scale)
        self.guard = fl.FlyoutGuard()
        self.autohide = True              # False when there is no tray icon to bring it back
        self.theme: th.Theme = th.LIGHT
        self.style_result: Dict[str, int] = {}   # what DWM said the last time it was styled
        self.last_placement: Optional[fl.Placement] = None
        self._theme_source = theme_source or th.current_theme
        self._autostart = autostart
        self._placer = placer or (lambda size, scale: fl.place_flyout(
            winutil.Win32Shell(), size, scale))
        self._before_show = before_show
        self._clock = clock
        self._animate = animate
        self._opacity = opacity
        self._take_focus = take_focus
        self._text: Dict[str, str] = {}
        self._dot_colour = ""
        self._problem_shown = False
        self._pos = (0, 0)
        self._size = (0, 0)
        self._anim_job: Optional[str] = None
        self._anim_n = 0
        self._settle_job: Optional[str] = None
        self._current: Optional[_Row] = None
        self._themed: List[Tuple[tk.Misc, str]] = []

        root.title(APP_NAME)
        root.resizable(False, False)
        root.overrideredirect(True)                    # no title bar, no frame, no taskbar button
        try:
            root.iconbitmap(default=str(icons.ico_path))   # the icon of the message boxes
        except tk.TclError:
            logger.debug("could not set the window icon", exc_info=True)
        root.protocol("WM_DELETE_WINDOW", lambda: self.hide("close"))   # Alt+F4 hides, never quits
        root.bind("<FocusOut>", self._on_focus_out)
        root.bind("<FocusIn>", self._on_focus_in)
        root.bind("<Escape>", self._on_escape)
        root.bind("<Up>", lambda e: self._move(-1))
        root.bind("<Down>", lambda e: self._move(1))
        root.bind("<Return>", self._on_enter_key)
        root.bind("<space>", self._on_enter_key)

        available = set(tkfont.families(root))
        default_family = tkfont.nametofont("TkDefaultFont", root=root).actual("family")
        body = th.pick_font(available, th.BODY_FONTS, default_family)
        title = th.pick_font(available, th.TITLE_FONTS, body)
        check_family, self.check_glyph = th.pick_check(available)
        self.fonts = {
            "body": tkfont.Font(root=root, family=body, size=th.BODY_PT),
            "row": tkfont.Font(root=root, family=body, size=th.ROW_PT),
            "title": tkfont.Font(root=root, family=title, size=th.ROW_PT,
                                 weight="normal" if title != body else "bold"),
            "check": tkfont.Font(root=root, family=check_family, size=th.BODY_PT),
        }
        self._build()
        self._apply_theme(self._theme_source())
        root.withdraw()

    # --- layout -------------------------------------------------------------

    def _paint(self, widget: tk.Misc, role: str) -> tk.Misc:
        """Register `widget` to be coloured by `role` whenever the theme is applied."""
        self._themed.append((widget, role))
        return widget

    def _separator(self, parent: tk.Misc) -> tk.Frame:
        sep = tk.Frame(parent, height=1, bd=0, highlightthickness=0)
        sep.pack(fill="x", padx=round(ROW_TEXT_PAD * self.scale), pady=round(4 * self.scale))
        self._paint(sep, "border")
        return sep

    def _build(self) -> None:
        s = self.scale
        pad = round(PAD_X * s)
        self._width = round(BASE_WIDTH * s)
        body = tk.Frame(self.root, bd=0, highlightthickness=0)
        body.pack(fill="both", expand=True, padx=1, pady=1)    # the 1 px border is the root showing
        self._body = body
        self._paint(body, "background")
        # fixes the width: a strip as wide as the inside of the flyout (the text below
        # wraps, so nothing else asks for more)
        ruler = tk.Frame(body, width=self._width - 2, height=1, bd=0, highlightthickness=0)
        ruler.pack(side="top")
        self._paint(ruler, "background")

        # header: the Clawd mark, the title, the status dot
        header = tk.Frame(body, bd=0, highlightthickness=0)
        header.pack(fill="x", padx=pad, pady=(round(12 * s), round(6 * s)))
        self._paint(header, "background")
        self._mark = self._make_mark()
        if self._mark is not None:
            mark = tk.Label(header, image=self._mark, bd=0)
            mark.pack(side="left", padx=(0, round(8 * s)))
            self._paint(mark, "background")
        title = tk.Label(header, text=APP_NAME, font=self.fonts["title"], anchor="w", bd=0)
        title.pack(side="left")
        self._paint(title, "text")
        self._dot = self._make_dot(header)
        self._dot.pack(side="left", padx=(round(8 * s), 0), pady=(round(2 * s), 0))

        # the status lines: name | value
        lines = tk.Frame(body, bd=0, highlightthickness=0)
        lines.pack(fill="x", padx=pad, pady=(0, round(8 * s)))
        self._paint(lines, "background")
        lines.columnconfigure(0, minsize=round(LINE_NAME_PX * s))
        lines.columnconfigure(1, weight=1)
        value_wrap = self._width - 2 - 2 * pad - round(LINE_NAME_PX * s)
        self._lines: Dict[str, tk.Label] = {}
        for i, (key, heading) in enumerate((("bar", "Bar"), ("hooks", "Hooks"),
                                            ("claude", "Claude Code"), ("event", "Last event"))):
            name = tk.Label(lines, text=heading, font=self.fonts["body"], anchor="nw", bd=0)
            name.grid(row=i, column=0, sticky="nw", pady=round(2 * s))
            self._paint(name, "secondary")
            value = tk.Label(lines, text="", font=self.fonts["body"], anchor="nw", justify="left",
                             wraplength=value_wrap, bd=0, padx=0, pady=0)
            value.grid(row=i, column=1, sticky="nw", pady=round(2 * s))
            self._paint(value, "text")
            self._lines[key] = value

        # the red message and its Retry row: packed only while something is wrong
        self._problem_box = tk.Frame(body, bd=0, highlightthickness=0)
        self._paint(self._problem_box, "background")
        self._problem = tk.Label(self._problem_box, text="", font=self.fonts["body"],
                                 justify="left", anchor="w", bd=0, padx=0, pady=0,
                                 wraplength=self._width - 2 - 2 * pad)
        self._problem.pack(fill="x", padx=pad, pady=(0, round(4 * s)))
        self._paint(self._problem, "critical")
        self.retry_row = self._row(self._problem_box, RETRY_ROW)

        self._sep = self._separator(body)
        self.rows: Dict[str, _Row] = {}
        for spec in ACTION_ROWS:
            self.rows[spec[0]] = self._row(body, spec, pack=True,
                                           check=spec[0] == "toggle_autostart")
        self._sep_quit = self._separator(body)
        self.rows[QUIT_ROW[0]] = self._row(body, QUIT_ROW, pack=True)
        foot = tk.Frame(body, height=round(4 * s), bd=0, highlightthickness=0)
        foot.pack(side="bottom", fill="x")
        self._paint(foot, "background")
        self._rows_in_order = [self.retry_row] + list(self.rows.values())

    def _row(self, parent: tk.Misc, spec: Tuple[str, str, bool], pack: bool = False,
             check: bool = False) -> _Row:
        key, text, closes = spec
        row = _Row(self, parent, text, key, closes=closes, check=check)
        if pack:
            row.pack()
        return row

    def _make_dot(self, parent: tk.Misc) -> tk.Canvas:
        """The status light: a drawn circle, not an image."""
        d = round(DOT_PX * self.scale)
        canvas = tk.Canvas(parent, width=d, height=d, highlightthickness=0, bd=0)
        self._dot_item = canvas.create_oval(1, 1, d - 1, d - 1, outline="", fill=DOT_OTHER)
        self._dot_colour = DOT_OTHER
        self._paint(canvas, "background")
        return canvas

    def _make_mark(self) -> Optional[tk.PhotoImage]:
        """The 20 px Clawd in the header: the tray's own idle picture at the nearest
        size that was built, as a still image (no animation, no big picture)."""
        try:
            size = closest_size(round(MARK_PX * self.scale))
            buf = io.BytesIO()
            self.icons.icon(ICON_CONNECTED, size).save(buf, "PNG")
            return tk.PhotoImage(master=self.root, data=base64.b64encode(buf.getvalue()))
        except Exception:  # noqa: BLE001 - a missing picture must not stop the flyout
            logger.debug("could not load the Clawd mark", exc_info=True)
            return None

    # --- theme --------------------------------------------------------------

    def _apply_theme(self, theme: th.Theme) -> None:
        self.theme = theme
        self.root.configure(bg=theme.border)
        colours = {"background": dict(bg=theme.background),
                   "border": dict(bg=theme.border),
                   "text": dict(bg=theme.background, fg=theme.text),
                   "secondary": dict(bg=theme.background, fg=theme.secondary),
                   "critical": dict(bg=theme.background, fg=theme.critical)}
        for widget, role in self._themed:
            widget.configure(**colours[role])
        for row in self._rows_in_order:
            row.paint()

    def _sync_autostart(self) -> None:
        if self._autostart is not None and "toggle_autostart" in self.rows:
            try:
                on = bool(self._autostart())
            except Exception:  # noqa: BLE001
                on = False
            self.rows["toggle_autostart"].set_checked(on)

    # --- show / hide --------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self.guard.visible

    @property
    def hwnd(self) -> int:
        """The real top-level window (what Windows styles, moves and activates)."""
        return winutil.top_level_hwnd(self.root.winfo_id())

    def show(self) -> None:
        """Open it (from the Open menu item, a second launch, or startup). Already
        open: just bring it to the front."""
        if self.visible:
            self._focus()
        else:
            self._open()

    def toggle(self, at: Optional[float] = None) -> str:
        """A click on the tray icon: open it, or close it if it is open. `at` is when
        the click happened (the clock's time; the tray thread stamps it, so the delay
        before this runs on the UI thread does not count). Returns what was done:
        "show", "hide" or "ignore" (the click that has just closed it by taking focus
        away, or one right after it opened)."""
        action = self.guard.tray_click(self._clock() if at is None else at)
        if action == fl.SHOW:
            self._open()
        elif action == fl.HIDE:
            self.hide("toggle")
        return action

    def hide(self, why: str = "toggle") -> None:
        for job in (self._anim_job, self._settle_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        self._anim_job = self._settle_job = None
        if self.guard.visible:
            logger.info("Flyout closed: %s", why)
        self.guard.closed(self._clock(), why)
        self.root.withdraw()
        self._clear_current()

    def _open(self) -> None:
        if self._before_show is not None:
            self._before_show()                       # the app refreshes what it shows
        root = self.root
        self._apply_theme(self._theme_source())       # Windows may have switched since last time
        self._sync_autostart()
        self._clear_current()
        root.update_idletasks()                       # makes the real window, still hidden
        self._size = (root.winfo_reqwidth(), root.winfo_reqheight())
        placement = self._placer(self._size, self.scale)
        self.last_placement = placement
        self._pos = (placement.x, placement.y)
        animate = ((winutil.animations_enabled() if self._animate is None else self._animate)
                   and self._opacity > 0)
        dx, dy = fl.slide_offset(placement.edge, self.scale) if animate else (0, 0)
        hwnd = self.hwnd
        winutil.ensure_tool_window(hwnd)
        winutil.set_window_alpha(hwnd, 0.0 if animate else self._opacity)
        self.style_result = winutil.style_flyout(hwnd, self.theme.border)   # before it shows
        root.geometry(f"+{placement.x + dx}+{placement.y + dy}")
        self.guard.opened(self._clock())
        root.deiconify()
        root.attributes("-topmost", True)
        root.lift()
        winutil.ensure_tool_window(hwnd)
        if self._take_focus:
            self._focus()
        if animate:
            self._anim_n = 0
            self._anim_step((dx, dy))

    def _focus(self) -> None:
        """Take the keyboard focus, so Escape works and losing it can close the flyout."""
        if not self._take_focus:
            return
        try:
            self.root.focus_force()
        except tk.TclError:
            pass
        if winutil.foreground_window() != self.hwnd:
            winutil.force_foreground(self.hwnd)

    def _anim_step(self, offset: Tuple[int, int]) -> None:
        """One frame of the opening: rise out of the taskbar side while fading in. Each
        frame schedules the next; the last one does not, so nothing is left running."""
        self._anim_job = None
        if not self.visible:
            return
        self._anim_n += 1
        t = self._anim_n / ANIM_STEPS
        ease = 1 - (1 - t) ** 3
        x, y = self._pos
        self.root.geometry(f"+{x + round(offset[0] * (1 - ease))}+{y + round(offset[1] * (1 - ease))}")
        winutil.set_window_alpha(self.hwnd, self._opacity * ease)
        if self._anim_n < ANIM_STEPS:
            self._anim_job = self.root.after(ANIM_MS, self._anim_step, offset)

    # --- closing: focus lost, Escape ------------------------------------------

    def _has_focus(self) -> bool:
        try:
            return self.root.focus_displayof() is not None
        except tk.TclError:
            return False

    def _on_focus_out(self, event) -> None:
        if event.widget is not self.root or not self.autohide:
            return
        action = self.guard.focus_lost(self._clock())
        if action == fl.HIDE:
            self.hide("focus")
        elif action == fl.DEFER and self._settle_job is None:
            wait = self.guard.settle_in(self._clock())
            self._settle_job = self.root.after(max(1, round(wait * 1000) + 10), self._settle)

    def _on_focus_in(self, event) -> None:
        if event.widget is self.root:
            self.guard.focus_gained()

    def _settle(self) -> None:
        """The open guard is over: close if focus went away during it and has not come back."""
        self._settle_job = None
        if self.guard.settle(self._has_focus()) and self.autohide:
            self.hide("focus")

    def _on_escape(self, _event=None) -> None:
        if self.autohide and self.guard.escape():
            self.hide("escape")

    # --- rows: highlight, keys, actions -----------------------------------------

    def widget_at(self, x_root: int, y_root: int) -> Optional[tk.Misc]:
        """The widget under a screen point (None if it is not one of ours)."""
        try:
            return self.root.winfo_containing(x_root, y_root)
        except (tk.TclError, KeyError):
            return None

    def _shown_rows(self) -> List[_Row]:
        return [r for r in self._rows_in_order if r.shown]

    def _set_current(self, row: _Row) -> None:
        if self._current is not None and self._current is not row:
            self._current.set_state("normal")
        self._current = row
        row.set_state("hover")

    def _clear_current(self, row: Optional[_Row] = None) -> None:
        if row is not None and self._current is not row:
            row.set_state("normal")
            return
        if self._current is not None:
            self._current.set_state("normal")
        self._current = None

    def _move(self, step: int) -> None:
        rows = self._shown_rows()
        if not rows or not self.visible:
            return
        at = rows.index(self._current) if self._current in rows else (-1 if step > 0 else 0)
        self._set_current(rows[(at + step) % len(rows)])

    def _on_enter_key(self, _event=None) -> None:
        if self._current is not None and self.visible:
            self._current.invoke()

    def _activate(self, row: _Row) -> None:
        """Run a row's action. Rows that open a dialog or another program close the
        flyout first, so nothing is left hanging over or behind it."""
        if row.closes:
            self.hide("action")
        try:
            self.actions[row.key]()
        finally:
            if not row.closes:
                self._sync_autostart()

    # --- content ------------------------------------------------------------

    def render(self, view: ViewState, hooks_text: str, hooks_warning: str) -> None:
        """Set the status lines, the dot and the message. Only what changed is touched;
        if the flyout is open and its height changed it is placed again, so it keeps
        its place against the taskbar."""
        values = {
            "bar": view.bar_text(),
            "hooks": hooks_text,
            "claude": view.sessions_text(),
            "event": view.last_event_text(),
            "problem": view.problem() or hooks_warning,
        }
        changed = False
        for key, text in values.items():
            if self._text.get(key) == text:
                continue
            self._text[key] = text
            (self._problem if key == "problem" else self._lines[key]).configure(text=text)
            changed = True
        colour = DOT_COLOURS.get(view.icon_name(), DOT_OTHER)
        if colour != self._dot_colour:
            self._dot_colour = colour
            self._dot.itemconfigure(self._dot_item, fill=colour)
        want_problem = bool(values["problem"])
        if want_problem != self._problem_shown:
            self._problem_shown = want_problem
            changed = True
            if want_problem:
                self._problem_box.pack(fill="x", before=self._sep)
            else:
                self._problem_box.pack_forget()
        want_retry = view.can_retry and want_problem
        if want_retry != self.retry_row.shown:
            changed = True
            if want_retry:
                self.retry_row.pack()
            else:
                self.retry_row.pack_forget()
                self._clear_current(self.retry_row)
        if changed and self.visible:
            self._refit()

    def _refit(self) -> None:
        self.root.update_idletasks()
        size = (self.root.winfo_reqwidth(), self.root.winfo_reqheight())
        if size == self._size:
            return
        self._size = size
        placement = self._placer(size, self.scale)
        self.last_placement = placement
        self._pos = (placement.x, placement.y)
        if self._anim_job is None:
            self.root.geometry(f"+{placement.x}+{placement.y}")
