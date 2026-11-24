"""What Developer Mode can change on the panel itself: PINs, robot IP, ESC key.

The defaults stay where they always were — the PINs in programs.py, the robot IP
in robotControl.py. What is changed on the Settings page is written to
settings.json next to this file and laid over those defaults at every start, so
the panel remembers it, and copying a new version of the program over the folder
keeps it (settings.json is not part of the program files).

Forgot a PIN and locked yourself out? Delete settings.json (over SSH or with a
keyboard) and the defaults from programs.py apply again:

    rm ~/cobot_depowdering_module/settings.json

setup_ur_link.sh reads the robot IP from here as well, so the network setup and
the GUI keep dialling the same address.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
from pathlib import Path

import programs as pg
import robotControl as rc

FILE = Path(__file__).resolve().with_name("settings.json")
DEMO_RUNS_FILE = FILE.with_name("demo_runs.json")

# Taken before anything is applied, so "what is the default" never changes
# under a running panel.
_DEFAULT_PINS = {label: role.pin for label, role in pg.ROLES.items()}
_DEFAULT_ROBOT_IP = rc.ROBOT_IP

# Roles that must always have a PIN: an empty one would let anybody in, and for
# the role that can reach Settings that would also let anybody change the PINs.
PIN_REQUIRED = {
    label
    for label, role in pg.ROLES.items()
    if role.settings or role.windowed or getattr(role, "pin_required", False)
}

_values: dict = {}


def defaults() -> dict:
    return {
        "pins": dict(_DEFAULT_PINS),
        "robot_ip": _DEFAULT_ROBOT_IP,
        "esc_windowed": False,
    }


def load() -> dict:
    """Read settings.json on top of the defaults and apply it. A missing or
    broken file is never fatal — the defaults apply and the reason is printed."""
    global _values
    values = defaults()
    if FILE.exists():
        try:
            stored = json.loads(FILE.read_text(encoding="utf-8"))
            for label, pin in (stored.get("pins") or {}).items():
                if label in values["pins"] and _pin_problem(label, pin) is None:
                    values["pins"][label] = pin
            ip = stored.get("robot_ip")
            if ip and ip_problem(ip) is None:
                values["robot_ip"] = ip
            values["esc_windowed"] = bool(stored.get("esc_windowed", False))
        except Exception as e:
            print(f"settings.json not read ({e}) - using the defaults")
    _values = values
    _apply()
    return values


def get(key: str):
    if not _values:
        load()
    return _values[key]


def pin(label: str) -> str:
    return get("pins")[label]


def demo_run_count() -> int:
    """Completed DEMO runs recorded on this panel."""
    try:
        value = json.loads(DEMO_RUNS_FILE.read_text(encoding="utf-8")).get("completed", 0)
    except Exception:
        return 0
    return value if type(value) is int and value >= 0 else 0


def record_demo_run() -> int:
    """Persist and return the next completed DEMO run count."""
    count = demo_run_count() + 1
    temporary = DEMO_RUNS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"completed": count}) + "\n", encoding="utf-8")
    temporary.replace(DEMO_RUNS_FILE)
    return count


def _save():
    FILE.write_text(json.dumps(_values, indent=2) + "\n", encoding="utf-8")


def _apply():
    """Push the values into the modules that use them."""
    for label, value in _values["pins"].items():
        if label in pg.ROLES:
            pg.ROLES[label] = dataclasses.replace(pg.ROLES[label], pin=value)
    rc.ROBOT_IP = _values["robot_ip"]


# ----- checks -----------------------------------------------------------------


def _pin_problem(label: str, value) -> str | None:
    if not isinstance(value, str):
        return "is not text"
    if value == "":
        return f"{label} needs a PIN" if label in PIN_REQUIRED else None
    if not value.isdigit():
        return "digits only"
    if len(value) < 4:
        return "at least 4 digits"
    if len(value) > 8:
        return "at most 8 digits"
    return None


def pin_problem(label: str, value: str) -> str | None:
    """Why `value` cannot become the PIN of `label`, or None if it can."""
    problem = _pin_problem(label, value)
    if problem:
        return problem
    if value:
        for other, other_pin in get("pins").items():
            if other != label and other_pin == value:
                return f"already the PIN of {other}"
    return None


def ip_problem(value: str) -> str | None:
    try:
        address = ipaddress.IPv4Address(value)
    except Exception:
        return "not an IPv4 address (four numbers 0-255 with dots)"
    if address.is_unspecified or address.is_multicast or str(address).endswith(".255"):
        return "not a usable device address"
    return None


# ----- changes (each one is saved at once) ------------------------------------


def set_pin(label: str, value: str) -> str | None:
    """Store a new PIN. Returns why not, or None when it was saved."""
    problem = pin_problem(label, value)
    if problem:
        return problem
    _values["pins"][label] = value
    _apply()
    _save()
    return None


def set_robot_ip(value: str) -> str | None:
    problem = ip_problem(value)
    if problem:
        return problem
    _values["robot_ip"] = value
    _apply()
    _save()
    return None


def set_esc_windowed(enabled: bool):
    _values["esc_windowed"] = bool(enabled)
    _save()


def reset() -> None:
    """Back to the defaults of programs.py / robotControl.py."""
    global _values
    _values = defaults()
    _apply()
    try:
        FILE.unlink()
    except FileNotFoundError:
        pass
