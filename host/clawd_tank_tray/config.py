"""The app's own small state, ``%LOCALAPPDATA%\\ClawdTank\\state.json``.

Today that is one fact: the user said No to the hooks question (or removed the
hooks), so the first-run dialog is not shown again on every launch.
"""

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("clawd-tank.tray.config")


class AppConfig:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(self._data, f, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)
        except OSError:
            logger.warning("could not save %s", self.path, exc_info=True)

    @property
    def hooks_declined(self) -> bool:
        return bool(self._data.get("hooks_declined", False))

    @hooks_declined.setter
    def hooks_declined(self, value: bool) -> None:
        if bool(value) != self.hooks_declined:
            self._data["hooks_declined"] = bool(value)
            self._save()
