[English](README.md) | [Deutsch](README.de.md)

# Raspberry Pi HMI for a UR30 Depowdering Cell

Touchscreen HMI software for a Raspberry Pi-based cobot depowdering cell. The
application provides role-based operator screens, preset and queued routines,
UR Dashboard Server communication, GPIO outputs, robot-status display, and an
optional media panel and Demo screensaver.

> **Safety:** This HMI is not a safety-rated control system and does not replace
> the robot's pendant, emergency-stop devices, safeguarding, or a documented
> risk assessment. Validate robot programs and safety configuration on the
> actual cell before use. Operate only with appropriate safeguards and
> supervision.

## Features

- Touch-friendly CustomTkinter interface for Raspberry Pi.
- User, Demo, Super User, and Developer Mode roles.
- Robot programs, presets, position-aware queue, and repeat controls.
- UR Dashboard Server commands and a polled safety-status display.
- Raspberry Pi GPIO outputs, with a simulated-output fallback when GPIO
  hardware is unavailable.
- Still-image/video panel and optional Demo-mode screensaver.
- On-device settings for role PINs and robot IP.

The application sends no payload data to the robot. Safety-state information is
for operator awareness only and is not a safety function.

## Requirements

- Raspberry Pi OS (the included installer targets Bookworm).
- Python 3, Tkinter, and the dependencies installed by `install.sh`.
- A Universal Robots controller reachable from the Pi over the network, with
  its Dashboard Server available. The robot programs referenced by
  `programs.py` must be installed on the controller. Update the controller
  paths in `programs.py` to match your own installation.
- Optional: supported GPIO hardware for physical output control.
- Optional: authorized media files in `media/`.

## Installation and startup

Clone or copy this repository to the Raspberry Pi, then run:

```bash
bash install.sh
bash start.sh
```

`start.sh` can also install missing dependencies on first launch. The installer
uses Raspberry Pi OS packages where available and requires internet access and
`sudo`.

Optional desktop launcher and autostart:

```bash
bash create_desktop_launcher.sh
bash start.sh --autostart
# Disable autostart:
bash start.sh --no-autostart
```

## Configure before connecting to a robot

1. Change the example role PINs in `settings.example.json` to unique 4–8 digit
   values and set `robot_ip` to the controller's address.
2. Save the configured file as `settings.json` beside the application. This
   local file is ignored by Git. Do not publish it.
3. If you do not use `settings.json`, update the defaults in `programs.py` and
   `robotControl.py` before deployment. Built-in defaults in public source are
   visible to everyone and are not suitable as production credentials.
4. Confirm the Dashboard Server connection and each referenced robot program
   before operating the cell.

`settings.example.json` contains placeholders, not ready-to-use credentials.
Replace every `CHANGE_ME` value before starting. Developer Mode can change PINs
and the robot IP in the running application; changes are saved to the local
`settings.json`. See `settings.py`.

## Demo behavior

The Demo role runs the `DEMO` preset for 200 passes, with a two-second delay
between robot-program snippets. During an active Demo run:

- Five taps on the top-right Cobot Control mark within 1.5 seconds unlock CANCEL.
- Three taps on the Ansicht idle-image area within 1.5 seconds unlock PAUSE.
- CANCEL waits for the current snippet to finish, turns off pending Demo
  outputs, and starts no further snippet.
- CONTINUE relocks PAUSE. A new Demo run starts from the beginning.

Outside Demo mode, CANCEL pauses the active program and clears the queued
steps; CONTINUE allows only the active program to finish.

The screensaver uses `media/screensaver.mp4` when available. See
[`media/README.md`](media/README.md) for media behavior and playback details.
Only add media you are authorized to distribute.

## Repository map

| Path | Purpose |
|---|---|
| `main.py` | Main application and UI layout |
| `gui_core.py` | Application logic and robot-run handling |
| `programs.py` | Robot program references, presets, roles, and PIN defaults |
| `robotControl.py` | UR Dashboard Server communication |
| `io_wrapper.py` | GPIO output abstraction |
| `settings.py` | Local settings persistence and validation |
| `cobot_theme.py` | Neutral theme and Cobot Control header mark |
| `media_panel.py` | Image/video panel and screensaver playback |
| `media/` | Idle image and media documentation |
| `settings.example.json` | Template for local settings |

Helper scripts for installation, network setup, desktop launch, and GPIO
diagnostics are included.

## Public-repository notes

- `settings.json`, runtime logs, generated Python bytecode, Demo run counts, and
  operator-provided video files are excluded by `.gitignore`.
- The repository currently has no license. Do not assume that public
  availability grants permission to reuse the code or assets. Confirm
  authorization for any third-party media before redistribution.
- The default role PINs in `programs.py` are visible in source. Change them
  before connecting the HMI to a robot; do not treat them as secure credentials.
- The controller program paths in `programs.py` are examples and must be
  configured to match the robot installation.
- This project is an HMI, not a certified safety component. Robot behavior,
  GPIO wiring, operator access, and cell safeguarding remain the deployer's
  responsibility.

## Documentation language

German documentation: [`README.de.md`](README.de.md).
