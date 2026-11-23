"""Preferences shared by all windows, including migration from 0.1."""

from __future__ import annotations

from copy import deepcopy
import json
from uuid import uuid4

from PySide6.QtCore import QObject, Signal

from .analysis_rules import DEFAULT_RULES
from .messages import localize
from .paths import write_json
from .translation import DEFAULT_PROFILE


DEFAULTS = {
    "langFrom": "en",
    "langTo": "ja",
    "language": "ja",
    "concurrency": 2,
    "backupCount": 5,
    "autoSave": True,
    "opacity": 40,
    "dialogSize": [486, 356],
    "rules": DEFAULT_RULES,
}


def bounded(value, default, low, high):
    return min(high, max(low, value)) if type(value) is int else default


def load_preferences(path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    settings = deepcopy(DEFAULTS)
    for key, default in DEFAULTS.items():
        if type(raw.get(key)) is type(default):
            settings[key] = raw[key]
    if settings["language"] not in ("ja", "en"):
        settings["language"] = "ja"
    settings["concurrency"] = bounded(settings["concurrency"], 2, 1, 8)
    settings["backupCount"] = bounded(settings["backupCount"], 5, 0, 200)
    settings["opacity"] = bounded(settings["opacity"], 40, 0, 100)
    dims = settings["dialogSize"]
    if len(dims) != 2 or any(type(value) is not int or value < 200 for value in dims):
        settings["dialogSize"] = DEFAULTS["dialogSize"].copy()
    rules = settings["rules"]
    settings["rules"] = {key: rules[key] if type(rules.get(key)) is type(default) else default
                         for key, default in DEFAULT_RULES.items()}
    for key, low, high in (("edge_band", 3, 25), ("small_text", 2, 30)):
        settings["rules"][key] = bounded(settings["rules"][key], DEFAULT_RULES[key], low, high)

    profiles = {}
    if isinstance(raw.get("profiles"), dict):
        for key, profile in raw["profiles"].items():
            if isinstance(profile, dict):
                profiles[key] = deepcopy(DEFAULT_PROFILE)
                for name, default in DEFAULT_PROFILE.items():
                    value = profile.get(name)
                    if type(value) is type(default) or (name == "temperature" and type(value) is int):
                        profiles[key][name] = value
    if not profiles:
        profile = deepcopy(DEFAULT_PROFILE)
        for key in ("endpoint", "model", "apiKey", "systemPrompt"):
            if isinstance(raw.get(key), str):
                profile[key] = raw[key]
        profiles["default"] = profile
    active = raw.get("activeProfile")
    settings["profiles"] = profiles
    settings["activeProfile"] = active if isinstance(active, str) and active in profiles else next(iter(profiles))
    return settings


class Preferences(QObject):
    changed = Signal()

    def __init__(self, directory):
        super().__init__()
        self.directory = directory
        self.path = directory / "settings.json"
        self.data = load_preferences(self.path)

    def save(self, data):
        write_json(self.path, data)
        self.data = data
        self.changed.emit()

    def tr_text(self, japanese, english):
        return english if self.data["language"] == "en" else japanese

    def localize(self, message):
        """A fixed error message (see messages.py) in the display language."""
        return localize(self.data["language"], message)

    @staticmethod
    def profile_id():
        return uuid4().hex
