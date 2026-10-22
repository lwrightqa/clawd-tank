"""The Clawd Tank window (tkinter, main thread only).

About 360 x 300: Clawd at 3x on the left, status lines on the right, the buttons
underneath. Minimising or closing it hides it to the tray (withdrawn: no taskbar
button); only Quit ends the app.
"""

import base64
import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, Optional, Tuple

from . import APP_NAME
from .icons import IconSet
from .state import ViewState

logger = logging.getLogger("clawd-tank.tray.window")

BASE_WIDTH, BASE_HEIGHT = 360, 300
SPRITE_PX = 48     # the frame size shown, enlarged ZOOM times
ZOOM = 3
RED = "#b3261e"


class MainWindow:
    """`actions` maps button names to callables: install, open_log, hide, quit,
    retry. All run on the UI thread."""

    def __init__(self, root: tk.Tk, icons: IconSet, actions: Dict[str, Callable[[], None]],
                 scale: float = 1.0):
        self.root = root
        self.icons = icons
        self.actions = actions
        self.scale = max(1.0, scale)
        self._photos: Dict[Tuple[str, int], tk.PhotoImage] = {}
        self._shown: Tuple[str, int] = ("", -1)
        self._text: Dict[str, str] = {}
        self._retry_shown = False

        root.title(APP_NAME)
        w, h = round(BASE_WIDTH * self.scale), round(BASE_HEIGHT * self.scale)
        x = max(0, (root.winfo_screenwidth() - w) // 2)
        y = max(0, (root.winfo_screenheight() - h) // 3)
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.resizable(False, False)
        try:
            root.iconbitmap(default=str(icons.ico_path))
        except tk.TclError:
            logger.debug("could not set the window icon", exc_info=True)
        root.protocol("WM_DELETE_WINDOW", self.hide)   # the X button hides, never quits
        root.bind("<Unmap>", self._on_unmap)           # minimising hides to the tray

        self._build()

    # --- layout -------------------------------------------------------------

    def _build(self) -> None:
        s = self.scale
        pad = round(10 * s)
        outer = ttk.Frame(self.root, padding=pad)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x")
        left = ttk.Frame(top)
        left.pack(side="left", anchor="n")
        self.image = ttk.Label(left)
        self.image.pack()
        self.caption = ttk.Label(left, font=("Segoe UI", 9, "bold"), anchor="center",
                                 wraplength=SPRITE_PX * ZOOM)
        self.caption.pack(pady=(round(4 * s), 0))

        right = ttk.Frame(top)
        right.pack(side="left", fill="both", expand=True, padx=(pad, 0))
        wrap = round((BASE_WIDTH - SPRITE_PX * ZOOM - 4 * 10) * s)
        self._lines: Dict[str, ttk.Label] = {}
        for key, heading in (("bar", "Bar"), ("hooks", "Hooks"), ("claude", "Claude Code")):
            row = ttk.Frame(right)
            row.pack(fill="x", pady=(0, round(6 * s)))
            ttk.Label(row, text=heading, foreground="#555555").pack(anchor="w")
            label = ttk.Label(row, text="", wraplength=wrap, justify="left")
            label.pack(anchor="w")
            self._lines[key] = label
        self._detail = ttk.Label(right, text="", wraplength=wrap, justify="left",
                                 foreground="#555555")
        self._detail.pack(anchor="w")

        msg = ttk.Frame(outer)
        msg.pack(fill="x", pady=(round(6 * s), 0))
        self._problem = ttk.Label(msg, text="", foreground=RED, justify="left",
                                  wraplength=round((BASE_WIDTH - 2 * 10 - 70) * s))
        self._problem.pack(side="left", anchor="w")
        self._retry = ttk.Button(msg, text="Retry", width=7, command=self.actions["retry"])

        buttons = ttk.Frame(outer)
        buttons.pack(side="bottom", fill="x")
        grid = (("Install or repair hooks", "install"), ("Open log", "open_log"),
                ("Hide to tray", "hide"), ("Quit", "quit"))
        for i, (text, key) in enumerate(grid):
            ttk.Button(buttons, text=text, command=self.actions[key]).grid(
                row=i // 2, column=i % 2, sticky="ew",
                padx=round(2 * s), pady=round(2 * s))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)

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

    def render(self, view: ViewState, hooks_text: str, hooks_warning: str,
               now: Optional[float] = None) -> None:
        """Set the status lines. Only labels whose text changed are touched."""
        problem = view.problem() or hooks_warning
        values = {
            "bar": view.bar_text(),
            "hooks": hooks_text,
            "claude": view.sessions_text(),
            "detail": f"Last event: {view.last_event_text()}",
            "caption": view.caption(now),
            "problem": problem,
        }
        for key, text in values.items():
            if self._text.get(key) == text:
                continue
            self._text[key] = text
            widget = {"detail": self._detail, "caption": self.caption,
                      "problem": self._problem}.get(key) or self._lines[key]
            widget.configure(text=text)
        want_retry = view.can_retry
        if want_retry != self._retry_shown:
            self._retry_shown = want_retry
            if want_retry:
                self._retry.pack(side="right", padx=(6, 0))
            else:
                self._retry.pack_forget()

    def set_frame(self, sprite: str, index: int) -> None:
        """Show one frame of `sprite`, 3x, crisp. Same frame again: no work."""
        key = (sprite, index % self.icons.frame_count(sprite))
        if key == self._shown:
            return
        photo = self._photos.get(key)
        if photo is None:
            data = base64.b64encode(self.icons.png(sprite, key[1], SPRITE_PX))
            photo = tk.PhotoImage(data=data).zoom(ZOOM, ZOOM)
            self._photos[key] = photo
        self.image.configure(image=photo)
        self._shown = key
