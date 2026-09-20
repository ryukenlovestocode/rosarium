"""
Simple JSON-backed persistent storage.

Not a database — just a dict that survives bot restarts, saved to a
.json file on disk. Good enough for small-to-medium servers; if this
ever needs to handle heavy concurrent writes or huge datasets, swap
this module out for SQLite without changing the cogs that use it
(keep the same get/set/save interface).

Safety: writes go to a temp file first, then atomically replace the
real file. This means if the bot crashes or is killed mid-save, the
original data file is never left half-written/corrupted — worst case
you lose only the most recent unsaved change, not everything.
"""

import json
import os
import threading


class JSONStore:
    def __init__(self, filepath: str, default: dict | None = None):
        self.filepath = filepath
        self._lock = threading.Lock()
        self.data: dict = default.copy() if default else {}
        self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError):
                # File exists but is corrupt/unreadable — don't crash the bot,
                # start fresh instead but keep the bad file around for inspection.
                backup_path = self.filepath + ".corrupt"
                if os.path.exists(self.filepath):
                    os.replace(self.filepath, backup_path)
                self.data = {}
        else:
            self._save()  # create the file on first run

    def _save(self):
        """Atomically write self.data to disk."""
        with self._lock:
            directory = os.path.dirname(self.filepath) or "."
            os.makedirs(directory, exist_ok=True)
            tmp_path = self.filepath + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp_path, self.filepath)  # atomic on POSIX and Windows

    # --- Public interface ---

    def get(self, key: str, default=None):
        return self.data.get(str(key), default)

    def set(self, key: str, value):
        self.data[str(key)] = value
        self._save()

    def delete(self, key: str):
        self.data.pop(str(key), None)
        self._save()

    def all(self) -> dict:
        return self.data