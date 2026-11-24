"""Digital outputs (IO1 … IO12) on the Raspberry Pi 5 that runs the GUI.

The GUI only ever talks to this module, never to a GPIO library directly, so a
development machine without GPIO hardware still starts: if the real backend
cannot be opened the module falls back to a simulated one that just remembers
the states. Check is_simulated() / describe() if you need to know which is live.

Pi 5 note: the old RPi.GPIO library does not work on the Pi 5 (its RP1 south
bridge needs the newer character-device interface). gpiozero on Raspberry Pi OS
Bookworm picks the lgpio factory automatically and does the right thing, so
that is the backend used here. Both are preinstalled on Raspberry Pi OS; on a
lean install: sudo apt install python3-gpiozero python3-lgpio

Pin numbers below are BCM (the GPIOn names), not physical header positions.
reset_gpio.sh reads IO_PINS and ACTIVE_HIGH out of this file to drive the same
pins to their off level at boot, so this module is the only place to edit them.
"""

from __future__ import annotations

import threading

# Channel name -> BCM pin. These twelve avoid the pins with a default alternate
# function: I2C (2, 3), SPI (7-11), UART (14, 15) and the HAT ID EEPROM (0, 1).
# That leaves GPIO 4, 5, 6, 12, 13, 16-27 free; the ones below are the twelve
# with the most convenient header positions. Adding more means taking a pin
# from that remaining pool (4, 24, 25, 26, 27) and disabling whatever bus you
# borrow beyond that.
IO_PINS: dict[str, int] = {
    "IO1": 5,    # header pin 29
    "IO2": 6,    # header pin 31
    "IO3": 13,   # header pin 33
    "IO4": 19,   # header pin 36
    "IO5": 12,   # header pin 35
    "IO6": 16,   # header pin 32
    "IO7": 20,   # header pin 11
    "IO8": 21,   # header pin 12
    "IO9": 17,   # header pin 38
    "IO10": 27,  # header pin 40
    "IO11": 22,  # header pin 15
    "IO12": 10,  # header pin 16
}

# True  = "on" drives the pin high (3.3 V), the usual case for a MOSFET/SSR input.
# False = "on" pulls the pin low, for relay boards with active-low inputs.
#
# This also decides which level "off" is, so reset_gpio.sh reads it from here:
# with False it drives the pins high at boot instead of low. Note neither the
# boot script nor this module covers the moment before the kernel is up — an
# external pull resistor to the off level (ground when True, the board's own
# supply when False) is what holds the outputs safe until then.
ACTIVE_HIGH = True

CHANNELS = tuple(IO_PINS)

# One lock around the whole module: the GUI toggles from the UI thread while a
# run worker may pulse an output from its own thread.
_lock = threading.RLock()
_backend: str | None = None  # "gpiozero" | "simulated", None until first use
_devices: dict[str, object] = {}
_sim_state: dict[str, bool] = {name: False for name in IO_PINS}
_init_error: str | None = None
_failed: dict[str, str] = {}  # channel -> why its pin could not be claimed


def _ensure_setup() -> str:
    """Open the outputs on first use and return the backend name.

    All channels start off. Safe to call repeatedly — later calls are no-ops.

    Each pin is claimed on its own: a single pin held by another process or by a
    kernel driver only costs that one channel, the rest still switch for real.
    Which ones failed and why is in failed_channels(); the backend only drops to
    "simulated" when no pin at all could be claimed.
    """
    global _backend, _init_error
    with _lock:
        if _backend is not None:
            return _backend
        try:
            from gpiozero import OutputDevice
        except Exception as e:  # library missing (e.g. Windows dev machine)
            _init_error = f"gpiozero unavailable ({type(e).__name__}: {e})"
            _backend = "simulated"
            return _backend
        _failed.clear()
        for name, pin in IO_PINS.items():
            try:
                _devices[name] = OutputDevice(
                    pin, active_high=ACTIVE_HIGH, initial_value=False
                )
            except Exception as e:
                _failed[name] = f"GPIO{pin}: {type(e).__name__}: {e}"
        if _devices:
            _backend = "gpiozero"
            _init_error = "; ".join(
                f"{name} {why}" for name, why in _failed.items()
            ) or None
        else:
            # No GPIO hardware at all, or every pin is taken.
            _init_error = "GPIO not usable — " + "; ".join(
                f"{name} {why}" for name, why in _failed.items()
            )
            _backend = "simulated"
        return _backend


# Public alias — call this at startup to fail early instead of on first click.
setup = _ensure_setup


def _check(channel: str) -> str:
    if channel not in IO_PINS:
        raise ValueError(f"Unknown I/O channel {channel!r} — expected one of {CHANNELS}")
    return channel


def set_output(channel: str, value: bool) -> bool:
    """Switch one channel on (True) or off (False). Returns the new state."""
    _check(channel)
    with _lock:
        _ensure_setup()
        device = _devices.get(channel)  # missing = this one pin stayed simulated
        if device is not None:
            device.value = bool(value)
        _sim_state[channel] = bool(value)
        return _sim_state[channel]


def get_output(channel: str) -> bool:
    """Current state of one channel, as last commanded."""
    _check(channel)
    with _lock:
        _ensure_setup()
        return _sim_state[channel]


def toggle(channel: str) -> bool:
    """Flip one channel and return its new state."""
    _check(channel)
    with _lock:
        return set_output(channel, not get_output(channel))


def states() -> dict[str, bool]:
    """All channel states, in IO1 … IO12 order."""
    with _lock:
        _ensure_setup()
        return dict(_sim_state)


def all_off() -> None:
    """Switch every channel off. Used by the GUI's All OFF button and on exit."""
    with _lock:
        for name in IO_PINS:
            set_output(name, False)


def is_simulated() -> bool:
    """True when no channel switches a real pin. See failed_channels() for a mix."""
    return _ensure_setup() == "simulated"


def backend_name() -> str:
    return _ensure_setup()


def failed_channels() -> dict[str, str]:
    """Channels whose pin could not be claimed, mapped to the reason.

    Empty when everything is live. These channels still remember their state,
    they just do not drive a pin — the usual cause is another process or a
    device-tree overlay holding that GPIO.
    """
    with _lock:
        _ensure_setup()
        return dict(_failed)


def describe() -> str:
    """One-line backend summary for the GUI status bar.

    Only the pin numbers, not the full name=pin mapping: with twelve channels
    that would no longer fit on the status line. The mapping is printed by the
    command-line check below, together with the per-channel reasons.
    """
    if _ensure_setup() == "gpiozero":
        live = [name for name in IO_PINS if name not in _failed]
        pins = ", ".join(str(IO_PINS[name]) for name in live)
        if _failed:
            taken = ", ".join(f"{n} (GPIO{IO_PINS[n]})" for n in _failed)
            return (
                f"GPIO partly ready ({len(live)} outputs on GPIO {pins}) — "
                f"pin unavailable, not switched: {taken}"
            )
        return f"GPIO ready ({len(live)} outputs on GPIO {pins})"
    return f"SIMULATED — outputs are not switched: {_init_error}"


def close() -> None:
    """Switch everything off and release the pins.

    Releasing returns the pins to inputs, so whatever they drive should also be
    held in its safe state by an external pull resistor — to ground when
    ACTIVE_HIGH, to the board's own supply when not. Call on application exit.
    """
    global _backend, _init_error
    with _lock:
        if _backend == "gpiozero":
            for dev in _devices.values():
                try:
                    dev.off()
                    dev.close()
                except Exception:
                    pass
        _devices.clear()
        _failed.clear()
        for name in _sim_state:
            _sim_state[name] = False
        _backend = None
        _init_error = None


if __name__ == "__main__":
    # Command-line check on the Pi, e.g.:
    #   python3 io_wrapper.py            -> print backend and all states
    #   python3 io_wrapper.py IO1 on     -> switch IO1 on
    #   python3 io_wrapper.py off        -> switch everything off
    import sys

    args = [a.lower() for a in sys.argv[1:]]
    if args == ["off"]:
        all_off()
    elif len(args) == 2:
        name = args[0].upper()
        set_output(name, args[1] in ("on", "1", "true", "high"))
    elif args:
        print(f"usage: {sys.argv[0]} [IO1..IO12 on|off] | [off]")
        raise SystemExit(2)

    print(describe())
    problems = failed_channels()
    for channel, state in states().items():
        note = f"  <-- not switched: {problems[channel]}" if channel in problems else ""
        print(f"  {channel} (GPIO{IO_PINS[channel]}): {'ON' if state else 'OFF'}{note}")
