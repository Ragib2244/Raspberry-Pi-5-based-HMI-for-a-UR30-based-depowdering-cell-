"""The programs on the robot controller, the sequences built from them, where
each one leaves the arm, and the PINs that guard the panel.

Every GUI reads its program list, its presets, its slots, its abort table and
its PINs from here and never spells out a .urp path, a sequence or a PIN itself,
so a program is added, renamed or re-sequenced — and a PIN changed — in exactly
one place. Run this file to check that everything still refers to something that
exists and still fits together:

    python3 programs.py

It exits 0 when everything is sound and 1 with a list of problems when it is
not. Run it after every edit to this file — it is the only thing standing
between a typo here and a robot that refuses to start (or, worse, starts).


WHAT A "STEP" IS
----------------
Presets and queues are lists of *steps*. A step is always a plain string, and
there are three kinds:

    "w90"                 a program from the PROGRAMS table below — the arm moves
    "io:IO3:on"           a digital output on the Pi — the arm does not move
    "media:demo.mp4"      a picture or clip on the panel — the arm does not move

Only the first kind moves the robot. The other two are "tokens": they are done
instantly, in passing, and they leave the arm exactly where it was. That is why
a token may sit anywhere in a sequence without breaking the position chain.

The full token grammar is in the HOW TO ADD THINGS block below.


POSITIONS
---------
Besides its path, each program records the position the arm must be in before it
runs (start), the position it leaves the arm in (end), and roughly how long it
takes (duration_s). Two programs may only follow one another when the first
one's end is the second one's start — main.py, gui_locking.py and
gui_timing.py enforce that when building a queue; gui.py and gui_simple.py
ignore it and run whatever they are given.

The positions are just names (see POSITIONS): this module knows nothing about
joint angles, it only compares the labels, so keeping them honest is a matter of
filling in the table below to match what the .urp files actually do. Nothing
here ever reads the arm's real pose.

The durations are planning estimates, not measurements: they fill a progress bar
and count a run down, and nothing else depends on them. A wrong number only
makes the bar lie, it never makes the robot misbehave.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

# Only for the channel names, so an I/O step cannot name an output that does not
# exist. io_wrapper touches no GPIO until something actually switches a pin, so
# importing it here is free and works on a machine with no GPIO at all.
import io_wrapper as iow

# The named places the arm can be between two programs. Add one here first, then
# use it in the table below — and give it a RETURN_TO_MAIN entry.
POSITIONS = ("Home", "Vor der Zelle", "In der Zelle")
HOME, VOR_DER_ZELLE, IN_DER_ZELLE = POSITIONS

# Not a position: what start() reports for a token. A token fits anywhere, so it
# has no start of its own. It is never shown to an operator.
ANY = "*"


# --- PINs --------------------------------------------------------------------
# The PINs the panel asks for, in the one file that is meant to be edited.
# main.py asks for them on its login screen (see ROLES below); the older GUIs
# still ask for EXPERT_PIN and ADMIN_PIN at the button, as described here.
# Every GUI reads them from here, so changing a PIN is changing it once.
#
# EXPERT_PIN opens the queue (called expert, extended or queue mode depending on
# which GUI is running). The presets on the main view are all a passer-by can
# reach without it.
#
# ADMIN_PIN is a step further down: it takes main.py out of fullscreen,
# which is the only way to reach the rest of the machine. Going back into
# fullscreen needs no PIN — it only ever locks more, never less. The older GUIs
# do not run fullscreen and ignore this one.
#
# Digits only: the on-screen keypad (keypad.py) has no letters, so a PIN with
# anything else in it can never be typed and locks that mode out for good.
# python3 programs.py checks that.
EXPERT_PIN = "1234"
ADMIN_PIN = "1235"
USER_PIN = "0000"
DEMO_PIN = "9999"


# --- Users -------------------------------------------------------------------
# main.py starts on a login screen: pick a role from the dropdown, type its PIN.
# What each role may do afterwards is this table — nothing else in the program
# decides it. The order here is the order in the dropdown; the first is
# preselected.
#
#   pin       the PIN for this role. "" = no PIN, the role logs in at once.
#   expert    the queue (Expert Mode) — stringing programs, I/O and video steps
#   io        the manual I/O view — switching outputs by hand
#   windowed  leaving fullscreen (Windows Mode), the way to the rest of the Pi
#   settings  the Settings page: change PINs and robot IP, allow the ESC key
#   presets   allowed preset labels; None means all presets
#   abort     whether Cancel is available
#   adjust_run whether slot and repeat controls are available
#   slot_wrap whether slot pickup/place programs wrap a preset
#   pin_required  a PIN cannot be cleared in Settings for this role
#   demo      whether the completed DEMO run count is shown
#
# PINs and the robot IP changed on the Settings page are kept in settings.json
# and override the values in this file (see settings.py). Delete settings.json
# to get back to the PINs written here.
#
# Presets, slot, loops, payload, PAUSE and ABORT are open to every role.
# Super User and Developer use EXPERT_PIN and ADMIN_PIN, so the older GUIs,
# which still read those two, keep working with the same numbers.


@dataclass(frozen=True)
class Role:
    pin: str
    expert: bool = False
    io: bool = False
    windowed: bool = False
    settings: bool = False
    presets: tuple[str, ...] | None = None
    abort: bool = True
    adjust_run: bool = True
    slot_wrap: bool = True
    pin_required: bool = False
    demo: bool = False


ROLES: dict[str, Role] = {
    "User": Role(pin=USER_PIN, io=True),
    "Demo": Role(
        pin=DEMO_PIN,
        presets=("DEMO",),
        abort=True,
        adjust_run=False,
        slot_wrap=False,
        pin_required=True,
        demo=True,
    ),
    "Super User": Role(
        pin=EXPERT_PIN, expert=True, io=True, pin_required=True
    ),
    "Developer Mode": Role(
        pin=ADMIN_PIN, expert=True, io=True, windowed=True, settings=True
    ),
}

# How long a mode stays open after leaving it, in seconds. 0 asks for the PIN
# every single time, which is the safe default on a shared panel.
UNLOCK_GRACE_S = 0


@dataclass(frozen=True)
class Program:
    """One .urp on the controller, with the positions it moves between."""

    path: str
    start: str = HOME  # where the arm has to be before this program runs
    end: str = HOME  # where this program leaves the arm
    # Roughly how long the program runs, in seconds. Only used to drive the
    # progress bar and the remaining-time readout. Measure it once with a
    # stopwatch (or off the pendant log) and put the real number in.
    duration_s: float = 60.0


@dataclass(frozen=True)
class Slot:
    """One part station: how to fetch a part from it and how to put it back."""

    label: str  # what the picker on the main view shows
    pickup: str  # key in PROGRAMS
    place: str  # key in PROGRAMS


@dataclass(frozen=True)
class IoAction:
    """A parsed "io:…" step."""

    channel: str  # "IO5"
    action: str  # "on" | "off" | "pulse"
    seconds: float = 0.0  # pulse only


@dataclass(frozen=True)
class MediaCue:
    """A parsed "media:…" step. name is None for "back to the idle picture"."""

    name: str | None


# ===========================================================================
#  HOW TO ADD THINGS
#  =================
#
#  Everything below is a plain table. You do not need to read any other file to
#  extend it, and you do not need to touch any GUI — they all build themselves
#  from what is here. After every change, run:  python3 programs.py
#
#  ---------------------------------------------------------------------------
#  A NEW PROGRAM
#  ---------------------------------------------------------------------------
#  Add one line to PROGRAMS:
#
#      "Mein Programm": Program("/programs/ordner/mein.urp", HOME, HOME, 75),
#                        |      |                             |     |     |
#                        |      path exactly as it is on the  |     |     how many
#                        |      controller (the pendant shows |     |     seconds it
#                        |      it in the program's header)   |     |     roughly takes
#                        |                                    |     |
#                        |                 where the arm must be    where it is
#                        |                 BEFORE this program      left AFTER
#                        the name the GUI shows
#
#  Then, if it should appear as an add-button in the expert-mode queue, add its
#  name to DEPOWDER_PROGRAMS. Programs left out of that list still work fine
#  inside presets — that is exactly how the slot and return programs below stay
#  off the queue.
#
#  ---------------------------------------------------------------------------
#  A NEW POSITION
#  ---------------------------------------------------------------------------
#  1. Add the name to POSITIONS at the top of this file.
#  2. Unpack it into a constant on the line below POSITIONS, next to HOME.
#  3. Use that constant in the PROGRAMS table.
#  4. Give it a RETURN_TO_MAIN entry — the program that brings the arm home from
#     there, or None if the arm is already safe standing there.
#
#  Step 4 is not optional and the self-check fails without it. That is the
#  point: ABORT has to know how to get home from everywhere the arm can be.
#
#  ---------------------------------------------------------------------------
#  A NEW SLOT (a part station on the main view)
#  ---------------------------------------------------------------------------
#  1. Add its two programs to PROGRAMS — a pickup and a place. Make both of them
#     Home -> Home (see the comment above SLOTS for why).
#  2. Add one line to SLOTS.
#  3. Add its key to SLOT_ORDER, in the order the picker should show them.
#
#  The picker on the main view is built from SLOT_ORDER, so there is nothing to
#  change in any GUI.
#
#  ---------------------------------------------------------------------------
#  AN I/O STEP (switch an output from inside a sequence)
#  ---------------------------------------------------------------------------
#      "io:<channel>:on"                switch it on and carry on
#      "io:<channel>:off"               switch it off and carry on
#      "io:<channel>:pulse:<seconds>"   on, wait, off again
#
#  <channel> is one of the names in io_wrapper.IO_PINS: IO1 … IO12. Examples:
#
#      "io:IO3:on"          extraction fan on
#      "io:IO7:pulse:2.5"   blow off for 2.5 seconds
#
#  An I/O step never moves the arm, so it may go anywhere in a sequence without
#  upsetting the position chain. A pulse is the only kind that takes any real
#  time; on and off are treated as instant.
#
#  ---------------------------------------------------------------------------
#  A MEDIA STEP (put something on the panel from inside a sequence)
#  ---------------------------------------------------------------------------
#      "media:<filename>"   show that file from the media/ folder
#      "media:off"          turn the video off — back to the idle picture
#
#  Examples:  "media:anleitung.png"   "media:demo.mp4"   "media:off"
#
#  The filename is relative to the media/ folder (see media_panel.MEDIA_DIR).
#  The step switches the panel and the run carries straight on — it does not
#  wait for a video to finish, so it costs no time. A video loops by itself
#  until another media step replaces it.
#
#  "off" is a reserved name: a file actually called "off" cannot be selected.
#  A missing file is only a caption in the panel, never a failed run.
#
#  ---------------------------------------------------------------------------
#  A NEW PRESET (one button on the main view)
#  ---------------------------------------------------------------------------
#  A list of steps, mixing all three kinds freely:
#
#      "Mein Ablauf": ["Header", "io:IO3:on", "w90", "io:IO3:off", "Tail"],
#
#  Add it to DEPOWDER_PRESETS (the touch GUIs) or PROCESSING_PRESETS (gui.py).
#  The programs in it must chain: the first one starts at Home, and each one
#  starts where the previous one ended. To be repeatable (×2, ×3, ∞) it must
#  also end back at Home. python3 programs.py checks both.
#
#  NOTE: io: and media: steps are understood by main.py. The older GUIs
#  (gui.py, gui_simple.py, gui_locking.py, gui_timing.py) only know programs, so
#  a preset containing a token shows up there but reports an error if pressed.
#  Put token-bearing presets in DEPOWDER_PRESETS only if main.py is the one
#  being run — which it should be.
#
#  ---------------------------------------------------------------------------
#  A NEW PIN
#  ---------------------------------------------------------------------------
#  Change USER_PIN, EXPERT_PIN or ADMIN_PIN near the top of this file (User,
#  Super User, Developer Mode). Every GUI reads them from here, so there is
#  nothing to change anywhere else — but the running panel has to be restarted
#  before a new PIN takes effect. What each role may do is ROLES, right below.
#  USER_PIN = "" lets User log in without a PIN.
#
#  Digits only: the on-screen keypad cannot type anything else,
#  so a PIN with a letter in it locks that mode out with no way back in short of
#  editing this file again. python3 programs.py refuses one.
#
# ===========================================================================


# The atomic programs that live on the robot controller.
#
# TODO: most entries are still seeded Home -> Home, which lets anything follow
# anything. Fill in the real start/end per program (HOME / VOR_DER_ZELLE /
# IN_DER_ZELLE) for the queue locking to actually bite.
#
# TODO: the durations are guesses too — time each program once and replace them,
# otherwise the progress bar counts down against fiction.
PROGRAMS: dict[str, Program] = {
    "Greifer Auf": Program("/programs/demo13_10/Greifer_Auf_demo.urp", HOME, HOME, 2),
    "Greifer Zu": Program("/programs/demo13_10/Greifer_Zu_demo.urp", HOME, HOME, 2),
    "Home": Program("/programs/demo13_10/Home_demo_v1.urp", HOME, HOME, 2),
    "Tisch Pick": Program("/programs/Cobot_Depowdering/Vibra_Tisch_Pick_final_v4.urp", HOME, HOME, 31),
    "Tisch Place": Program("/programs/Cobot_Depowdering/Vibra_Tisch_Place_final_v4.urp", HOME, HOME, 34),
    "Tisch Rotate": Program("/programs/Cobot_Depowdering/Drehsequenz_Tisch_final_v6.urp", HOME, HOME, 255),
    "Slot 1 Pick": Program("/programs/Cobot_Depowdering/Slot1_Pick_final_v2.urp", HOME, HOME, 86),
    "Slot 1 Place": Program("/programs/Cobot_Depowdering/Slot1_Place_final_v2.urp", HOME, HOME, 75),
    "Slot 2 Pick": Program("/programs/Cobot_Depowdering/Slot2_Pick_final_v2.urp", HOME, HOME, 108),
    "Slot 2 Place": Program("/programs/Cobot_Depowdering/Slot2_Place_final_v2.urp", HOME, HOME, 97),
    "Load external START": Program("/programs/Cobot_Depowdering/Extern_Laden_v5.urp", HOME, HOME, 15),
    "Load external END": Program("/programs/Cobot_Depowdering/Extern_Beladen_Fertig_v5.urp", HOME, HOME, 10),
    "Entpulverungssequenz1": Program("/programs/Cobot_Depowdering/Entpulverungssequenz1_v1.urp", HOME, HOME, 90),
    "Mitte Pick": Program("/programs/Cobot_Depowdering/Mitte_Pick_v4.urp", HOME, HOME, 90),
    "Mitte Place": Program("/programs/Cobot_Depowdering/Mitte_Place_v3.urp", HOME, HOME, 90),
    "Mitte Meissel": Program("/programs/Cobot_Depowdering/Mitte_Meissel_v1.urp", HOME, HOME, 90),
    "Test_Main_Prog": Program("/programs/Cobot_Depowdering/Messe_Programm_v4.urp", HOME, HOME, 90),
    "Programm_von_Mehmet": Program(
        "/programs/Entpulvern/mehmet_masterarbeit/mehmet.urp", HOME, HOME, 300
    ),
    # Weighing preamble — only gui.py runs these. They are NOT dead code: gui.py
    # imports the four constants below at module level and will not start
    # without them. main.py does not use them at all.
    # TODO: real .urp names, positions and durations.
    "Program weight": Program("/programs/weight.urp", HOME, HOME, 25),
    "Pick up from scale": Program("/programs/pickup.urp", HOME, HOME, 40),
    "Move to workstation": Program("/programs/move_workstation.urp", HOME, HOME, 35),
    "Put down on scale": Program("/programs/putdown.urp", HOME, HOME, 40),
    "Header": Program("/programs/demo13_10/Header_demo_v3.urp", HOME, IN_DER_ZELLE, 29),
    "Turn 90": Program(
            "/programs/demo13_10/Turn_90_demo_v2.urp", IN_DER_ZELLE, IN_DER_ZELLE, 113
    ),
    "Turn 135": Program(
            "/programs/demo13_10/Turn_135_demo_v2.urp", IN_DER_ZELLE, IN_DER_ZELLE, 123
    ),
    "Turn 180": Program(
            "/programs/demo13_10/Turn_180_demo_v2.urp", IN_DER_ZELLE, IN_DER_ZELLE, 133
    ),
    "Footer": Program("/programs/demo13_10/Footer_demo_v3.urp", IN_DER_ZELLE, HOME, 20),
    # --- slots: fetching a part and putting it back -------------------------
    # Both halves of every slot are Home -> Home on purpose. A preset run is
    # wrapped as  pickup -> preset (all its loops) -> place, and every preset in
    # DEPOWDER_PRESETS both starts and ends at Home. So the pickup has to hand
    # the arm back at Home for the preset to chain, and the place has to accept
    # it at Home. It also keeps the whole wrapped run Home -> Home, which is
    # what makes it repeatable.
    #
    # If a pickup is ever changed to park the arm somewhere else, every preset's
    # first program has to start there too — python3 programs.py checks exactly
    # that combination and will tell you.
    #
    # TODO: real .urp paths and durations; these are placeholders.
    "Slot 1 pickup": Program("/programs/Cobot_Depowdering/Slot1_Pick_final_v2.urp", HOME, HOME, 30),
    "Slot 1 place": Program("/programs/Cobot_Depowdering/Slot1_Place_final_v2.urp", HOME, HOME, 30),
    "Slot 2 pickup": Program("/programs/Cobot_Depowdering/Slot2_Pick_final_v2.urp", HOME, HOME, 30),
    "Slot 2 place": Program("/programs/Cobot_Depowdering/Slot2_Place_final_v2.urp", HOME, HOME, 30),
    "Shaker pickup": Program("/programs/Cobot_Depowdering/Vibra_Tisch_Pick_final_v4.urp", HOME, HOME, 35),
    "Shaker place": Program("/programs/Cobot_Depowdering/Vibra_Tisch_Place_final_v4.urp", HOME, HOME, 35),
    # --- abort: getting back to the main pose from wherever the arm is ------
    # ABORT lets the program that is running finish, then runs the program below
    # for the position that program leaves the arm in. Every one of them must
    # therefore START at the position it is keyed by in RETURN_TO_MAIN and END
    # at Home — that is what makes the arm safe to walk away from. The
    # self-check enforces both.
    #
    # TODO: real .urp paths and durations; these are placeholders.
    "Return from Vor der Zelle": Program(
        "/programs/return/from_vor_der_zelle.urp", VOR_DER_ZELLE, HOME, 25
    ),
    "Return from In der Zelle": Program(
        "/programs/return/from_in_der_zelle.urp", IN_DER_ZELLE, HOME, 40
    ),
}


# --- groups: which programs each GUI offers ---------------------------------

# The depowdering programs, offered in the queue of main.py, gui_simple.py,
# gui_locking.py and the extended mode of gui_timing.py.
#
# The slot and return programs are deliberately NOT in here: they are run
# automatically around a preset and by ABORT, and must not be queued by hand.
DEPOWDER_PROGRAMS = [
    "Greifer Auf",
    "Greifer Zu",
    "Load external START",
    "Load external END",
    "Tisch Pick",
    "Tisch Place",
    "Tisch Rotate",
    "Slot 1 Pick",
    "Slot 1 Place",
    "Slot 2 Pick",
    "Slot 2 Place",
    "Test_Main_Prog",
    # "Programm_von_Mehmet",
    "Header",
    "Turn 90",
    "Turn 135",
    "Turn 180",
    "Footer",
    "Home"
]

###########################################################################################  Altlast evtl mit gui.py verwenden, wenn Waage existiert.
# What gui.py offers as preset / queue building blocks. The preamble programs
# below run automatically there and are not chosen from the queue.
PROCESSING_PROGRAMS = [
    "Toller Name",
    "Program 2",
    "Program 3",
    "Program 4",
    "Program 5",
    "Programm_von_Mehmet",
    "Header",
    "w90",
    "w135",
    "w180",
    "Tail",
]

# The fixed preamble gui.py runs before processing: weigh the part on the scale,
# pick it up, move to the workstation. PUTDOWN is used by its weigh routine.
# main.py does not use any of these.
WEIGH_PROGRAM = "Program weight"
PICKUP_PROGRAM = "Pick up from scale"
MOVE_PROGRAM = "Move to workstation"
PUTDOWN_PROGRAM = "Put down on scale"
####################################################################################  Altlast end

# --- slots -------------------------------------------------------------------
#
# The stations a part is fetched from and put back into. The picker on
# The main view of main.py is built from SLOT_ORDER; a preset run is wrapped in
# the chosen slot's two programs, once around the whole run (not once per loop).
# The expert-mode queue is never wrapped — it runs exactly what is in it.

SLOTS: dict[str, Slot] = {
    "slot1": Slot("Slot 1", "Slot 1 pickup", "Slot 1 place"),
    "slot2": Slot("Slot 2", "Slot 2 pickup", "Slot 2 place"),
    "shaker": Slot("Shaker", "Shaker pickup", "Shaker place"),
}
SLOT_ORDER = ("slot1", "slot2", "shaker")  # the order the picker shows them in
DEFAULT_SLOT = "slot1"


# --- abort: where to go from where -------------------------------------------
#
# Keyed by the position the aborted program left the arm in. None means there is
# nothing to run, which is not an error: at Home the arm is already parked.

RETURN_TO_MAIN: dict[str, str | None] = {
    HOME: None,
    VOR_DER_ZELLE: "Return from Vor der Zelle",
    IN_DER_ZELLE: "Return from In der Zelle",
}


# --- presets: the sequences a GUI offers as one button -----------------------
#
# A preset is a list of steps (see "WHAT A STEP IS" at the top), run
# back-to-back. They live here rather than in the GUIs so that renaming a
# program, or changing what "Mittel" means, is one edit in one file.
#
# Intensity is a matter of which programs run and in which order, nothing else.
# main.py, gui_locking.py and gui_timing.py additionally refuse a preset
# whose positions do not chain (see check_chain below), so a sequence put
# together here is checked before the arm moves.

###########################################################################----Hier die Programmreihenvolgen wenig/mittel/viel usw. aneinanderreihen.
# TODO: set which programs (and in which order) each intensity should run.

# What main.py, gui_simple.py, gui_locking.py and gui_timing.py offer on
# their main view.
DEPOWDER_PRESETS = {
    "IO Test": ["Greifer Auf", "io:IO4:on", "Greifer Zu", "Greifer Auf","Greifer Zu","Greifer Auf","Greifer Zu", "io:IO4:off"],
    "DEMO": ["Home", "Slot 1 Pick","io:IO1:on","io:IO4:on", "Entpulverungssequenz1","io:IO1:off","io:IO4:off", "Slot 1 Place","Home","Slot 2 Pick","io:IO1:on","io:IO4:on","Tisch Rotate", "Entpulverungssequenz1","io:IO1:off","io:IO4:off","Slot 2 Place","Home","Mitte Pick","io:IO9:on","Mitte Meissel","io:IO9:off","Mitte Place"],
    "Demo": ["Demo Sequenz"],
    "Demo Mittel": ["Demo Sequenz2"],
    "Test 2.11": ["Test_Main_Prog"],
    "IO Test": ["Greifer Auf", "io:IO4:on", "Greifer Zu", "Greifer Auf","Greifer Zu","Greifer Auf","Greifer Zu", "io:IO4:off"],
    # A worked example of all three step kinds in one sequence. Only
    # main.py can run this one — see the note in HOW TO ADD THINGS.  
    "Demo mit Video": [
        "media:demo.mp4",  # put the clip on the panel, carry straight on
        "Header",  # Home -> In der Zelle
        "io:IO3:on",  # extraction fan on; does not move the arm
        "w90",
        "io:IO3:pulse:2.5",  # 2.5 s blast
        "Tail",  # In der Zelle -> Home
        "io:IO3:off",
        "media:off",  # video off, panel back to the idle picture
    ],
}

# What gui.py offers. Same intensities, but only processing programs: every run
# there is wrapped in the weighing preamble above.
PROCESSING_PRESETS = {
    "Wenig": ["Toller Name", "Program 2"],
    "Mittel": ["Toller Name", "Program 2", "Program 3"],
    "Stark": ["Toller Name", "Program 2", "Program 3", "Program 4", "Program 5"],
}

# Every group and preset in this file, for the self-check at the bottom.
_GROUPS = {
    "DEPOWDER_PROGRAMS": DEPOWDER_PROGRAMS,
    "PROCESSING_PROGRAMS": PROCESSING_PROGRAMS,
    "WEIGH_PROGRAM": [WEIGH_PROGRAM],
    "PICKUP_PROGRAM": [PICKUP_PROGRAM],
    "MOVE_PROGRAM": [MOVE_PROGRAM],
    "PUTDOWN_PROGRAM": [PUTDOWN_PROGRAM],
}
_PRESET_SETS = {
    "DEPOWDER_PRESETS": DEPOWDER_PRESETS,
    "PROCESSING_PRESETS": PROCESSING_PRESETS,
}


# --- steps: programs, I/O tokens and media tokens ----------------------------

IO_PREFIX = "io:"
MEDIA_PREFIX = "media:"
IO_ACTIONS = ("on", "off", "pulse")
# What a plain on/off is worth on the progress bar. Switching a pin is instant;
# this is only so a run made of nothing but I/O steps has a non-zero total.
IO_SWITCH_S = 0.2
# The media step that means "back to the idle picture". Reserved: a file of this
# name cannot be selected.
MEDIA_OFF = "off"


def is_io(step: str) -> bool:
    return step.startswith(IO_PREFIX)


def is_media(step: str) -> bool:
    return step.startswith(MEDIA_PREFIX)


def is_token(step: str) -> bool:
    """True for anything that is not a program — an I/O or a media step."""
    return is_io(step) or is_media(step)


def parse_io(step: str) -> IoAction:
    """"io:IO5:pulse:1.5" -> IoAction("IO5", "pulse", 1.5). Raises ValueError."""
    grammar = "expected io:<channel>:on|off|pulse[:seconds]"
    parts = step[len(IO_PREFIX) :].split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"{step!r} is not a valid I/O step — {grammar}")
    channel = parts[0].upper()
    action = parts[1].lower()
    if channel not in iow.CHANNELS:
        raise ValueError(
            f"{step!r}: unknown channel {parts[0]!r} — "
            f"expected one of {', '.join(iow.CHANNELS)}"
        )
    if action not in IO_ACTIONS:
        raise ValueError(
            f"{step!r}: unknown action {parts[1]!r} — expected {', '.join(IO_ACTIONS)}"
        )
    if action == "pulse":
        if len(parts) != 3:
            raise ValueError(f"{step!r}: a pulse needs a length in seconds — {grammar}")
        try:
            seconds = float(parts[2])
        except ValueError:
            raise ValueError(f"{step!r}: {parts[2]!r} is not a number of seconds") from None
        if seconds <= 0:
            raise ValueError(f"{step!r}: a pulse of {seconds}s does nothing")
        return IoAction(channel, action, seconds)
    if len(parts) != 2:
        raise ValueError(f"{step!r}: {action} takes no further arguments — {grammar}")
    return IoAction(channel, action)


def parse_media(step: str) -> MediaCue:
    """"media:demo.mp4" -> MediaCue("demo.mp4"); "media:off" -> MediaCue(None)."""
    name = step[len(MEDIA_PREFIX) :].strip()
    if not name or name.lower() == MEDIA_OFF:
        return MediaCue(None)
    return MediaCue(name)


def io_token(channel: str, action: str, seconds: float | None = None) -> str:
    """Build an I/O step. The only place one is ever constructed."""
    if action == "pulse":
        return f"{IO_PREFIX}{channel}:pulse:{seconds:g}"
    return f"{IO_PREFIX}{channel}:{action}"


def media_token(name: str | None) -> str:
    """Build a media step. None means "back to the idle picture"."""
    return f"{MEDIA_PREFIX}{name if name else MEDIA_OFF}"


def describe_step(step: str) -> str:
    """A step as it reads on a button or in the queue listing. Never raises."""
    if is_io(step):
        try:
            action = parse_io(step)
        except ValueError:
            return f"{step}  (invalid)"
        if action.action == "pulse":
            return f"{action.channel} pulse {action.seconds:g} s"
        return f"{action.channel} → {action.action.upper()}"
    if is_media(step):
        cue = parse_media(step)
        return "video off" if cue.name is None else f"show {cue.name}"
    return step


def step_end(step: str, position: str) -> str:
    """Where the arm stands after this step, given where it stood before.

    A token leaves it exactly where it was — that is what makes I/O and media
    steps transparent to the position chain.
    """
    if is_token(step):
        return position
    return end(step)


def validate_step(step: str) -> str | None:
    """Why this step cannot be used, or None when it is fine."""
    if is_io(step):
        try:
            parse_io(step)
        except ValueError as e:
            return str(e)
        return None
    if is_media(step):
        return None  # a missing file is a caption, not an error
    if step not in PROGRAMS:
        return f"unknown program {step!r}"
    return None


# --- lookups ----------------------------------------------------------------


def get(key: str) -> Program:
    """The Program behind a name. Raises KeyError with the known names listed."""
    try:
        return PROGRAMS[key]
    except KeyError:
        if is_token(key):
            raise KeyError(
                f"{key!r} is an I/O or media step, not a program — "
                f"it has no .urp and no position"
            ) from None
        raise KeyError(
            f"Unknown program {key!r} — expected one of {list(PROGRAMS)}"
        ) from None


def path(key: str) -> str:
    """The .urp path to hand to robotControl.load_program()."""
    if is_token(key):
        raise ValueError(f"{key!r} is not a program — it has no .urp to load")
    return get(key).path


def start(key: str) -> str:
    """Where the arm must be before this step. ANY for a token: it fits anywhere."""
    if is_token(key):
        return ANY
    return get(key).start


def end(key: str) -> str:
    """Where this program leaves the arm.

    Deliberately raises for a token: a token's end is wherever the arm already
    was, which this function cannot know. Walk a sequence with step_end() or
    chain_end() instead — quietly answering "Home" here would be the easiest way
    to send the arm somewhere it cannot go.
    """
    if is_token(key):
        raise ValueError(
            f"{key!r} does not move the arm — use step_end(step, position) instead"
        )
    return get(key).end


def describe(key: str) -> str:
    """"Home → In der Zelle", for button labels and status messages.

    Never raises — it is on button captions and in hover text, including for
    steps that turned out to be invalid.
    """
    if is_token(key):
        return describe_step(key)
    try:
        program = get(key)
    except KeyError:
        return f"{key} (unknown)"
    return f"{program.start} → {program.end}"


# --- durations --------------------------------------------------------------


def duration(key: str) -> float:
    """How long this step is expected to take, in seconds.

    Never raises: the progress bar asks for every step in a run, valid or not.
    """
    if is_io(key):
        try:
            action = parse_io(key)
        except ValueError:
            return 0.0
        return action.seconds if action.action == "pulse" else IO_SWITCH_S
    if is_media(key):
        return 0.0  # switches the panel and carries straight on
    try:
        return float(get(key).duration_s)
    except KeyError:
        return 0.0


def total_duration(steps: list[str]) -> float:
    """How long a whole sequence is expected to take, in seconds."""
    return sum(duration(step) for step in steps)


def format_duration(seconds: float) -> str:
    """Seconds as "0:45", "2:30" or "1:05:00" — for buttons and countdowns.

    Rounds up, so a countdown never sits at 0:00 while the arm is still moving.
    """
    total = max(0, -(-int(seconds * 1000) // 1000))  # ceil, without float drift
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def describe_duration(key: str) -> str:
    """"ca. 2:30" — the estimate as it goes on a button."""
    return f"ca. {format_duration(duration(key))}"


# --- position matching ------------------------------------------------------


def can_follow(key: str, position: str) -> bool:
    """True when this step can be run with the arm standing at position.

    A token can always follow anything: it does not move the arm.
    """
    if is_token(key):
        return True
    return start(key) == position


def mismatch_reason(key: str, position: str) -> str | None:
    """Why this step cannot run from position, or None when it fits."""
    if is_token(key):
        return None
    if can_follow(key, position):
        return None
    return f"{key} starts at {start(key)} — the robot is at {position}"


def chain_end(steps: list[str], position: str = HOME) -> str | None:
    """Where a sequence leaves the arm, or None if it breaks somewhere."""
    for step in steps:
        if not can_follow(step, position):
            return None
        position = step_end(step, position)
    return position


def check_chain(steps: list[str], position: str = HOME) -> str | None:
    """The first break in a sequence as a message, or None when it is sound."""
    for index, step in enumerate(steps, 1):
        reason = mismatch_reason(step, position)
        if reason is not None:
            return f"step {index}: {reason}"
        position = step_end(step, position)
    return None


# --- slots ------------------------------------------------------------------


def slot(key: str) -> Slot:
    """The Slot behind a key. Raises KeyError with the known keys listed."""
    try:
        return SLOTS[key]
    except KeyError:
        raise KeyError(f"Unknown slot {key!r} — expected one of {list(SLOTS)}") from None


def slot_wrap(steps: list[str], slot_key: str | None) -> list[str]:
    """pickup + steps + place, for CHECKING a chain and QUOTING a total time.

    This is not what the worker runs: the pickup and the place happen once
    around all the loops, so the worker takes the slot separately and runs the
    two halves itself. Use this to validate a preset and to put a realistic
    number on its button.
    """
    if slot_key is None:
        return list(steps)
    chosen = slot(slot_key)
    return [chosen.pickup, *steps, chosen.place]


# --- abort ------------------------------------------------------------------


def return_program(position: str) -> str | None:
    """The program that brings the arm back to Home from position.

    None means there is nothing to run — the arm is already parked. An unknown
    position raises: an abort must never quietly do nothing because a position
    was renamed and its entry forgotten.
    """
    if position not in RETURN_TO_MAIN:
        raise KeyError(
            f"No return program for {position!r} — RETURN_TO_MAIN covers "
            f"{list(RETURN_TO_MAIN)}"
        )
    return RETURN_TO_MAIN[position]


# --- self-check -------------------------------------------------------------


def _media_folder() -> Path:
    """Where media steps look for their files — media_panel's MEDIA_DIR.

    Imported lazily and defensively: media_panel pulls in customtkinter, which
    is not worth requiring just to run this check.
    """
    folder = Path(__file__).resolve().parent / "media"
    try:
        import media_panel as mp

        named = Path(mp.MEDIA_DIR)
        folder = named if named.is_absolute() else Path(__file__).resolve().parent / named
    except Exception:
        pass
    return folder


if __name__ == "__main__":
    # Command-line check: print the tables and flag anything that cannot work.
    #   python3 programs.py
    #
    # The arrows and the "ca." lines are UTF-8. A Windows console still defaults
    # to cp1252, which cannot encode them, so ask for UTF-8 and fall back to
    # replacing what will not fit — a mangled arrow is better than a traceback
    # in the middle of the check.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass

    problems: list[str] = []
    warnings: list[str] = []

    print("Programs:")
    for name, program in PROGRAMS.items():
        marks = [
            f"unknown {field} {value!r}"
            for field, value in (("start", program.start), ("end", program.end))
            if value not in POSITIONS
        ]
        if program.duration_s <= 0:
            marks.append(f"duration {program.duration_s!r} is not positive")
        if not program.path.startswith("/"):
            # The dashboard server gets this string verbatim, so a relative path
            # is answered with "File not found" and the run dies at the load.
            marks.append(f"path {program.path!r} does not start with '/'")
        if is_token(name):
            # A program called "io:…" would make every step ambiguous.
            marks.append("name would be read as an I/O or media step")
        note = f"  <-- {', '.join(marks)}" if marks else ""
        problems.extend(f"{name}: {m}" for m in marks)
        print(f"  {name}: {program.path}")
        print(
            f"      {program.start} → {program.end}"
            f"   [{format_duration(program.duration_s)}]{note}"
        )

    # Every group and preset must name something that exists — a leftover name
    # here is what stops every GUI at startup.
    for group, names in _GROUPS.items():
        for name in names:
            bad = validate_step(name)
            if bad is not None:
                problems.append(f"{group}: {bad}")

    media_dir = _media_folder()
    for preset_set, presets in _PRESET_SETS.items():
        print()
        print(f"  {preset_set}:")
        for label, steps in presets.items():
            bad = [validate_step(s) for s in steps]
            unknown = [m for m in bad if m is not None]
            for message in unknown:
                problems.append(f"{preset_set}[{label!r}]: {message}")
            shown = ", ".join(describe_step(s) for s in steps)
            if unknown:
                print(f"      {label}: {shown}   <-- {'; '.join(unknown)}")
                continue
            broken = check_chain(steps)
            if broken is not None:
                problems.append(f"{preset_set}[{label!r}] does not chain — {broken}")
            note = f"   <-- {broken}" if broken else ""
            print(
                f"      {label}: {shown}"
                f"   [{format_duration(total_duration(steps))}]{note}"
            )
            # A media step naming a file that is not there is a caption in the
            # panel, not a failed run — the folder is deployment data, not
            # source, so this warns rather than fails.
            for step in steps:
                if not is_media(step):
                    continue
                cue = parse_media(step)
                if cue.name is not None and not (media_dir / cue.name).is_file():
                    warnings.append(
                        f"{preset_set}[{label!r}]: {cue.name} is not in {media_dir.name}/"
                    )

    # Slots: both halves must exist, and every preset must still chain once it
    # is wrapped in them. This is the combination the presets alone never test.
    print()
    print("  SLOTS:")
    for key in SLOT_ORDER:
        if key not in SLOTS:
            problems.append(f"SLOT_ORDER: unknown slot {key!r}")
            continue
        station = SLOTS[key]
        marks = [
            f"unknown program {name!r}"
            for name in (station.pickup, station.place)
            if name not in PROGRAMS
        ]
        problems.extend(f"SLOTS[{key!r}]: {m}" for m in marks)
        if marks:
            print(f"      {station.label}: <-- {', '.join(marks)}")
            continue
        wrapped = format_duration(duration(station.pickup) + duration(station.place))
        print(f"      {station.label}: {station.pickup} / {station.place}   [+{wrapped}]")
        for name in (station.pickup, station.place):
            if name in DEPOWDER_PROGRAMS or name in PROCESSING_PROGRAMS:
                problems.append(
                    f"SLOTS[{key!r}]: {name!r} is also offered as a queue add-button"
                )
        for label, steps in DEPOWDER_PRESETS.items():
            if any(validate_step(s) for s in steps):
                continue  # already reported above
            broken = check_chain(slot_wrap(steps, key))
            if broken is not None:
                problems.append(
                    f"{station.label} + DEPOWDER_PRESETS[{label!r}] does not chain — {broken}"
                )
    for key in SLOTS:
        if key not in SLOT_ORDER:
            problems.append(f"SLOTS[{key!r}] is missing from SLOT_ORDER — it is not shown")
    if DEFAULT_SLOT not in SLOTS:
        problems.append(f"DEFAULT_SLOT {DEFAULT_SLOT!r} is not a slot")

    # Abort: every position needs an answer, and every answer must start where
    # it is keyed and end at Home.
    print()
    print("  RETURN_TO_MAIN:")
    for position in POSITIONS:
        if position not in RETURN_TO_MAIN:
            problems.append(f"RETURN_TO_MAIN: no entry for position {position!r}")
            print(f"      {position}: <-- missing")
            continue
        name = RETURN_TO_MAIN[position]
        if name is None:
            print(f"      {position}: (nothing to do)")
            continue
        if name not in PROGRAMS:
            problems.append(f"RETURN_TO_MAIN[{position!r}]: unknown program {name!r}")
            print(f"      {position}: {name}   <-- unknown program")
            continue
        marks = []
        if PROGRAMS[name].start != position:
            marks.append(f"starts at {PROGRAMS[name].start}, not {position}")
        if PROGRAMS[name].end != HOME:
            marks.append(f"ends at {PROGRAMS[name].end}, not {HOME}")
        if name in DEPOWDER_PROGRAMS or name in PROCESSING_PROGRAMS:
            marks.append("is also offered as a queue add-button")
        problems.extend(f"RETURN_TO_MAIN[{position!r}]: {m}" for m in marks)
        note = f"   <-- {', '.join(marks)}" if marks else ""
        print(f"      {position}: {name}{note}")
    for position in RETURN_TO_MAIN:
        if position not in POSITIONS:
            problems.append(f"RETURN_TO_MAIN: {position!r} is not a known position")

    # PINs: the keypad has digits and nothing else, so a PIN that cannot be
    # typed locks its mode out with no way back in short of editing this file.
    print()
    print("  PINs:")
    for label, pin in (("EXPERT_PIN", EXPERT_PIN), ("ADMIN_PIN", ADMIN_PIN)):
        if not isinstance(pin, str) or not pin.isdigit():
            problems.append(f"{label} {pin!r} is not a non-empty string of digits")
            print(f"      {label}: {pin!r}   <-- not typeable on the keypad")
            continue
        print(f"      {label}: {'•' * len(pin)} ({len(pin)} digits)")
    print("  Users:")
    if not ROLES:
        problems.append("ROLES is empty — nobody can log in")
    seen_pins: dict[str, str] = {}
    for label, role in ROLES.items():
        rights = [r for r in ("expert", "io", "windowed", "settings") if getattr(role, r)]
        if role.pin == "":
            shown = "no PIN"
        elif isinstance(role.pin, str) and role.pin.isdigit():
            shown = f"{'•' * len(role.pin)} ({len(role.pin)} digits)"
        else:
            problems.append(f"ROLES[{label!r}] PIN {role.pin!r} is not typeable on the keypad")
            shown = f"{role.pin!r}   <-- not typeable on the keypad"
        print(f"      {label}: {shown}, rights: {', '.join(rights) or 'presets only'}")
        if role.pin and role.pin in seen_pins:
            warnings.append(
                f"ROLES {seen_pins[role.pin]!r} and {label!r} have the same PIN — "
                "the dropdown is then the only thing between them"
            )
        seen_pins.setdefault(role.pin, label)
    if not any(role.settings for role in ROLES.values()):
        warnings.append("no role has settings=True — PINs can only be changed in this file")
    settings_file = Path(__file__).resolve().with_name("settings.json")
    if settings_file.exists():
        warnings.append(
            f"{settings_file.name} exists — PINs/robot IP set on the Settings page "
            "override the values in this file"
        )
    if not any(role.windowed for role in ROLES.values()):
        warnings.append(
            "no role has windowed=True — the panel can then only be left by "
            "rebooting or over SSH"
        )
    if EXPERT_PIN == ADMIN_PIN:
        warnings.append(
            "EXPERT_PIN and ADMIN_PIN are the same — leaving fullscreen is then "
            "no harder than opening the queue"
        )
    if not isinstance(UNLOCK_GRACE_S, (int, float)) or UNLOCK_GRACE_S < 0:
        problems.append(f"UNLOCK_GRACE_S {UNLOCK_GRACE_S!r} is not a number >= 0")
    elif UNLOCK_GRACE_S > 0:
        warnings.append(
            f"UNLOCK_GRACE_S is {UNLOCK_GRACE_S}s — a mode stays open that long "
            "after it is left, without asking again"
        )

    # Tokens must be transparent to the chain in every slot of a sequence.
    if chain_end(["io:IO1:on", "Header", "media:x.mp4"]) != IN_DER_ZELLE:
        problems.append("I/O and media steps are not transparent to the position chain")
    if media_dir.is_dir() and (media_dir / MEDIA_OFF).exists():
        warnings.append(
            f"a file called {MEDIA_OFF!r} is in {media_dir.name}/ but cannot be "
            f"selected — 'media:off' means 'back to the idle picture'"
        )

    print(
        f"\n{len(PROGRAMS)} programs, {len(SLOTS)} slots, "
        f"{sum(1 for v in RETURN_TO_MAIN.values() if v)} return programs, "
        f"positions: {', '.join(POSITIONS)}"
    )
    print(
        "all depowdering programs back to back: "
        f"{format_duration(total_duration(DEPOWDER_PROGRAMS))}"
    )
    if warnings:
        print("\nWarnings (not fatal):")
        for warning in warnings:
            print(f"  {warning}")
    if problems:
        print("\nProblems:")
        for problem in problems:
            print(f"  {problem}")
        raise SystemExit(1)
    print("\nEverything checks out.")
