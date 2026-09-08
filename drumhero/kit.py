"""Kit configuration: which input note numbers belong to which instrument."""
import json
import os

from .chart import DEFAULT_KIT, INSTRUMENT_ZONES, PADS, ZONE, ZONE_KEYS

KIT_PATH = os.path.expanduser("~/.config/drumhero/kit.json")
SETTINGS_PATH = os.path.expanduser("~/.config/drumhero/settings.json")
DEFAULT_SETTINGS = {"audio_device": None, "fullscreen": True, "midi_trace": None, "offset_ms": 0.0, "drum_sounds": True}


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
    """The saved kit {zone key: [note numbers]}, or None if the wizard has never run."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return migrate(data)


def migrate(data):
    """Kits saved before zones existed had one list per instrument (kick/snare/hihat/crash).
    Spread those notes over the instrument's zones by their factory numbers; a note with no
    known home stays in the first zone (head/bow)."""
    kit = {k: [] for k in ZONE_KEYS}
    legacy = not any(k in data for k in ZONE_KEYS if k not in INSTRUMENT_ZONES)
    for k, notes in data.items():
        notes = [int(n) for n in notes] if isinstance(notes, list) else []
        if k in ZONE and not (legacy and k in INSTRUMENT_ZONES):
            kit[k].extend(notes)
        elif k in INSTRUMENT_ZONES:
            zones = INSTRUMENT_ZONES[k]
            for n in notes:
                home = next((z for z in zones if n in ZONE[z].defaults), zones[0])
                kit[home].append(n)
    return {k: sorted(set(v)) for k, v in kit.items()}


def save_kit(kit, path=KIT_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({k: sorted(kit.get(k, [])) for k in ZONE_KEYS}, f, indent=2)


def describe(kit):
    """Short: how many zones are set, and the first few missing ones."""
    missing = [ZONE[k].label for k in ZONE_KEYS if not kit.get(k)]
    n = len(ZONE_KEYS) - len(missing)
    if not missing:
        return f"all {n} zones assigned"
    more = f" +{len(missing) - 3}" if len(missing) > 3 else ""
    return f"{n}/{len(ZONE_KEYS)} zones · missing {', '.join(missing[:3])}{more}"


def describe_pads(kit):
    """One line per pad: zone parts with their note numbers."""
    lines = []
    for pad, zones in PADS:
        parts = []
        for zk in zones:
            notes = "/".join(map(str, kit.get(zk, []))) or "-"
            parts.append(f"{ZONE[zk].part or 'pad'} {notes}")
        lines.append(f"{pad}: " + "  ".join(parts))
    return lines


def default_kit():
    return {k: list(v) for k, v in DEFAULT_KIT.items()}
