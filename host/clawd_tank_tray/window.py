"""The Clawd Tank window (tkinter, main thread only).

Plain text: a small coloured dot and the Bar line, then Hooks, Claude Code and
the last event, a red message with Retry when something is wrong, and the
buttons. No picture and no timer: it is redrawn only when a line changes.
Minimising or closing it hides it to the tray (withdrawn: no taskbar button);
only Quit ends the app.
"""

import logging
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable, Dict

from . import APP_NAME
from .icons import IconSet
from .state import ICON_CONNECTED, ICON_ERROR, ViewState

logger = logging.getLogger("clawd-tank.tray.window")

BASE_WIDTH = 340        # at 100 % scaling; the height follows the content
HEADING_PX = 84         # the column of line names (Bar, Hooks, ...)
RED = "#b3261e"
GREY_TEXT = "#555555"
# The status dot beside the Bar line: the same three states as the tray icon.
DOT_COLOURS = {ICON_CONNECTED: "#2e8b57", ICON_ERROR: RED}
DOT_OTHER = "#9aa0a6"
DOT_PX = 10


class MainWindow:
    """`actions` maps button names to callables: install, open_log, hide, quit,
    retry. All run on the UI thread."""

    def __init__(self, root: tk.Tk, icons: IconSet, actions: Dict[str, Callable[[], None]],
                 scale: float = 1.0):
        self.root = root
        self.icons = icons
        self.actions = actions
        self.scale = max(1.0, scale)
        self._text: Dict[str, str] = {}
        self._dot_colour = ""
        self._retry_shown = False

        root.title(APP_NAME)
        root.resizable(False, False)
        try:
            root.iconbitmap(default=str(icons.ico_path))
        except tk.TclError:
            logger.debug("could not set the window icon", exc_info=True)
        root.protocol("WM_DELETE_WINDOW", self.hide)   # the X button hides, never quits
        root.bind("<Unmap>", self._on_unmap)           # minimising hides to the tray

        self._build()
        self._place()

    # --- layout -------------------------------------------------------------

    def _build(self) -> None:
        s = self.scale
        pad = round(12 * s)
        gap = round(10 * s)
        value_px = round(BASE_WIDTH * s) - 2 * pad - round(HEADING_PX * s)
        wrap = value_px - round(18 * s)        # leaves room for the dot on the Bar line
        outer = ttk.Frame(self.root, padding=pad)
        outer.pack(fill="both", expand=True)
        self._outer = outer

        # line name | value; the two columns together fix the window's width
        rows = ttk.Frame(outer)
        rows.pack(fill="x")
        rows.columnconfigure(0, minsize=round(HEADING_PX * s))
        rows.columnconfigure(1, minsize=value_px)
        self._lines: Dict[str, ttk.Label] = {}
        for i, (key, heading) in enumerate((("bar", "Bar"), ("hooks", "Hooks"),
                                            ("claude", "Claude Code"),
                                            ("event", "Last event"))):
            ttk.Label(rows, text=heading, foreground=GREY_TEXT).grid(
                row=i, column=0, sticky="nw", padx=(0, gap), pady=round(3 * s))
            cell = ttk.Frame(rows)
            cell.grid(row=i, column=1, sticky="nw", pady=round(3 * s))
            if key == "bar":
                self._dot = self._make_dot(cell)
                self._dot.pack(side="left", padx=(0, round(6 * s)), pady=round(2 * s))
            label = ttk.Label(cell, text="", wraplength=wrap, justify="left")
            label.pack(side="left")
            self._lines[key] = label

        # The red message (two lines at most) and Retry: a fixed-height strip, so
        # the window does not jump when a message appears.
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        msg = ttk.Frame(outer, height=max(2 * line, round(30 * s)))
        msg.pack(fill="x", pady=(round(8 * s), 0))
        msg.pack_propagate(False)
        self._problem = ttk.Label(msg, text="", foreground=RED, justify="left",
                                  wraplength=value_px + round(HEADING_PX * s) - round(60 * s))
        self._problem.pack(side="left", anchor="w")
        self._retry = ttk.Button(msg, text="Retry", width=7, command=self.actions["retry"])

        buttons = ttk.Frame(outer)
        buttons.pack(side="bottom", fill="x", pady=(round(8 * s), 0))
        grid = (("Install or repair hooks", "install"), ("Open log", "open_log"),
                ("Hide to tray", "hide"), ("Quit", "quit"))
        for i, (text, key) in enumerate(grid):
            ttk.Button(buttons, text=text, command=self.actions[key]).grid(
                row=i // 2, column=i % 2, sticky="ew",
                padx=round(2 * s), pady=round(2 * s))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)

    def _make_dot(self, parent) -> tk.Canvas:
        """The only graphic: a small round status light, drawn, not an image."""
        d = round(DOT_PX * self.scale)
        bg = ttk.Style(self.root).lookup("TFrame", "background") or self.root.cget("bg")
        canvas = tk.Canvas(parent, width=d, height=d, highlightthickness=0, bd=0, bg=bg)
        self._dot_item = canvas.create_oval(1, 1, d - 1, d - 1, outline="", fill=DOT_OTHER)
        self._dot_colour = DOT_OTHER
        return canvas

    def _place(self) -> None:
        """Put the window in the upper middle of the screen. Only the position is
        set: the size is whatever the content asks for (the width is fixed by the
        columns, the height by the lines), so a long wrapped line grows the window
        instead of pushing a button out of it."""
        self.root.update_idletasks()
        w, h = self._outer.winfo_reqwidth(), self._outer.winfo_reqheight()
        x = max(0, (self.root.winfo_screenwidth() - w) // 2)
        y = max(0, (self.root.winfo_screenheight() - h) // 3)
        self.root.geometry(f"+{x}+{y}")

    # --- show / hide --------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self.root.state() == "normal"

    def show(self) -> None:
        """Bring the window forward from the tray (or from minimised)."""
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        # Windows may refuse to move focus to a process that was not clicked; the
        # brief topmost flip still puts the window on top of the others.
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def hide(self) -> None:
        self.root.withdraw()

    def _on_unmap(self, event) -> None:
        # Unmap fires for every child too; act only for the window being minimised.
        if event.widget is self.root and self.root.state() == "iconic":
            self.root.after_idle(self.hide)

    # --- content ------------------------------------------------------------

    def render(self, view: ViewState, hooks_text: str, hooks_warning: str) -> None:
        """Set the status lines and the dot. Only what changed is touched."""
        values = {
            "bar": view.bar_text(),
            "hooks": hooks_text,
            "claude": view.sessions_text(),
            "event": view.last_event_text(),
            "problem": view.problem() or hooks_warning,
        }
        for key, text in values.items():
            if self._text.get(key) == text:
                continue
            self._text[key] = text
            (self._problem if key == "problem" else self._lines[key]).configure(text=text)
        colour = DOT_COLOURS.get(view.icon_name(), DOT_OTHER)
        if colour != self._dot_colour:
            self._dot_colour = colour
            self._dot.itemconfigure(self._dot_item, fill=colour)
        want_retry = view.can_retry
        if want_retry != self._retry_shown:
            self._retry_shown = want_retry
            if want_retry:
                self._retry.pack(side="right", padx=(6, 0))
            else:
                self._retry.pack_forget()
