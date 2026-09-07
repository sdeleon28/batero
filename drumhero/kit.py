"""Kit configuration: which input note numbers belong to which instrument."""
import json
import os

from .chart import DEFAULT_KIT, INSTRUMENTS, LABELS

KIT_PATH = os.path.expanduser("~/.config/drumhero/kit.json")
SETTINGS_PATH = os.path.expanduser("~/.config/drumhero/settings.json")
DEFAULT_SETTINGS = {"audio_device": None, "fullscreen": True}


def load_settings(path=SETTINGS_PATH):
    out = dict(DEFAULT_SETTINGS)
    try:
        with open(path) as f:
            out.update(json.load(f))
    except (OSError, ValueError):
        pass
    return out


def save_settings(settings, path=SETTINGS_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)


def load_kit(path=KIT_PATH):
    """The saved kit, or None if the wizard has never run."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return {k: [int(n) for n in data.get(k, [])] for k in INSTRUMENTS}


def save_kit(kit, path=KIT_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({k: sorted(kit.get(k, [])) for k in INSTRUMENTS}, f, indent=2)


def describe(kit):
    parts = []
    for k in INSTRUMENTS:
        notes = kit.get(k, [])
        parts.append(f"{LABELS[k]} {'/'.join(map(str, notes)) if notes else '-'}")
    return "  ".join(parts)


def default_kit():
    return {k: list(v) for k, v in DEFAULT_KIT.items()}
