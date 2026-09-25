"""Disposable, atomic disk cache of references extracted from item JSON."""
import json
import logging
import os
import tempfile
from contextlib import suppress
from pathlib import Path


class ReferenceCache:
    def __init__(self, path, signature):
        self.path = Path(path)
        self.signature = signature
        self.entries = {}
        self.current = {}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved["signature"] == signature and isinstance(saved["entries"], dict):
                self.entries = saved["entries"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            logging.getLogger("dependencies_monitor").warning("Ignoring unreadable reference cache %s", self.path)

    def get(self, item_id, modified, item_type):
        entry = self.entries.get(item_id)
        if not isinstance(entry, dict) or modified is None:
            return None
        if entry.get("modified") != modified or entry.get("type") != item_type:
            return None
        for field in ("paths", "references", "urls"):
            values = entry.get(field)
            if not isinstance(values, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in values.items()
            ):
                return None
        self.current[item_id] = entry
        return entry

    def put(self, item_id, modified, item_type, paths, references, urls):
        self.current[item_id] = dict(modified=modified, type=item_type, paths=paths,
                                     references=references, urls=urls)

    def save(self):
        # Only successful reads and cache hits survive; deleted items and failed reads disappear.
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             delete=False) as stream:
                temporary = stream.name
                json.dump(dict(signature=self.signature, entries=self.current), stream)
            os.replace(temporary, self.path)
        except OSError as error:
            logging.getLogger("dependencies_monitor").warning("Could not save reference cache: %s", error)
        finally:
            if temporary:
                with suppress(OSError):
                    os.unlink(temporary)
