"""Everything a robot GUI does that is not layout.

RobotAppBase is a CTk window with no widgets in it: the run worker, the clock,
the pause/abort protocol, the position locking, the queue, the PIN gates and the
I/O handling all live here. A layout file subclasses it, builds its own views
out of the factories below, and writes no logic at all — see main.py.

Why a base class and not a separate controller object: the worker already needs
self.after() to get back onto the UI thread and self._set_status() to say
anything, so a controller would need a reference to the window regardless — the
same coupling with more indirection, plus a new way to fail when the controller
outlives the window it is posting to.

WHAT A LAYOUT MUST PROVIDE
--------------------------
* _build_views(self) — called at the end of __init__. Build the frames, assign
  self.main_frame, and call self._register_view(frame) for each one.
* _button(self, parent, text, command, **kw) — the layout's own button style.
* At least one pause button and one abort button, from the factories here.
* Every button that starts a run appended to self.run_buttons, so it can be
  greyed out while the robot is busy.
* If it has a queue view: self.queue_box, and self.add_buttons filled in via
  self._bind_explain(btn, step).
* If it has an I/O view: self.io_buttons, most easily via _make_io_grid().

One rule: do not assign instance state before super().__init__() — CTk is not
ready for it, and __init__ here sets everything up in order.

THE RUN MODEL
-------------
A run is a list of *steps* (see programs.py): programs that move the arm, plus
"io:…" and "media:…" tokens that do not. A run repeats `loops` times, and a
preset run is additionally wrapped in a slot's pickup and place programs —
once around the whole thing, not once per loop.

Demo CANCEL lets the current snippet finish, switches off pending Demo outputs,
then ends the run. In every other mode, CANCEL pauses the active program and
clears the visible queue immediately; CONTINUE lets that program finish.
In Demo, five quick taps on the Cobot Control mark unlock CANCEL; three quick
taps on the Ansicht idle-image area unlock PAUSE.

Nothing here ever calls robotControl.stop(). PAUSE is the way to hold the arm
now, and the physical e-stop is the way to halt it.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

import customtkinter as ctk

import cobot_theme as theme
import io_wrapper as iow
import keypad as kp
import settings as st
import programs as pg
import robotControl as rc

# --- behaviour (looks live in the layout files) ------------------------------

POLL_INTERVAL = 1.0  # seconds between programstate polls
PLAY_TIMEOUT_S = 5.0  # how long to wait for PLAYING after play
TICK_MS = 500  # how often the countdown redraws
INACTIVITY_TIMEOUT_S = 30
INACTIVITY_WARNING_S = 10
INACTIVITY_TICK_MS = 500
DEMO_SCREENSAVER_TIMEOUT_S = 15
SAFETY_POLL_INTERVAL_S = 1.0
SAFETY_STATES = {
    "NORMAL",
    "REDUCED",
    "PROTECTIVE_STOP",
    "RECOVERY",
    "SAFEGUARD_STOP",
    "SYSTEM_EMERGENCY_STOP",
    "ROBOT_EMERGENCY_STOP",
    "VIOLATION",
    "FAULT",
    "AUTOMATIC_MODE_SAFEGUARD_STOP",
}
EMERGENCY_STOP_STATES = {"SYSTEM_EMERGENCY_STOP", "ROBOT_EMERGENCY_STOP"}

# --- window -------------------------------------------------------------------
# Started from an autostart script, the window manager is not always up yet when
# __init__ runs, and it then drops the fullscreen request without an error — the
# panel sits in a window while the code still believes it is locked. So the
# request is re-asserted until it takes, or until the PIN says otherwise.
FULLSCREEN_RETRY_MS = 300
FULLSCREEN_RETRY_TRIES = 12  # ~3.6 s, then give up quietly

# Windowed mode: how much of the screen the window may take. A window as big as
# the screen ends up with its title bar pushed off the top, and on a touchscreen
# there is then no way to move it. WINDOW_SIZE is an upper bound, not a promise —
# a smaller panel wins.
WINDOWED_SCREEN_FRACTION = 0.9
WINDOWED_TOP_MARGIN = 32  # px of title bar that must stay on the screen

# --- users -------------------------------------------------------------------
# The panel starts on a login screen (role from a dropdown, then its PIN). Which
# role may open Expert Mode, the I/O view, Windows Mode or Settings is the ROLES table in
# programs.py — the one file meant to be edited. _can() is the only place here
# that reads it.

# --- loops -------------------------------------------------------------------
LOOP_CHOICES = ("×1", "×2", "×3", "×5", "×10", "∞")
LOOP_ENDLESS = "∞"
# Seconds to wait between two loops (0 = straight on). Useful when the powder
# needs a moment to settle before the next pass starts.
LOOP_PAUSE_S = 0.0
DEMO_REPEAT_COUNT = 200
DEMO_SNIPPET_DELAY_S = 2.0
DEMO_LOGO_CANCEL_UNLOCK_TAPS = 5
DEMO_IDLE_PAUSE_UNLOCK_TAPS = 3
DEMO_CONTROL_UNLOCK_WINDOW_S = 1.5

class AbortRequested(Exception):
    """Raised inside the worker when ABORT has been pressed.

    Carried as an exception rather than a return value on purpose: the worker
    has half a dozen ways out, and any one of them that forgot to distinguish
    "this failed" from "we are aborting" would leave the arm parked in the cell
    with the GUI back to Idle. An exception cannot be dropped by a `return`.
    """


@dataclass
class ResumePoint:
    """Where a run got to, so it can be picked up again after a pause.

    index is the position in steps of the step that is running (or about to
    run) and cycle which loop it belongs to, which together are where a
    continue restarts. loops is None for a run that repeats until it is
    aborted.
    """

    steps: list[str]
    label: str
    payload_kg: float | None
    index: int
    cycle: int = 0
    loops: int | None = 1
    slot: str | None = None


@dataclass(frozen=True)
class Progress:
    """What the progress widgets show, as of one snapshot of the timer."""

    fraction: float  # 0.0 … 1.0, for the bar
    remaining: str  # "≈ 4:20 remaining"
    step: str  # "Queue · loop 2/5 · step 2/3: Program 2"


class RunTimer:
    """Wall-clock progress of a run, measured against the programs.py estimates.

    The worker thread reports which step it has reached; the UI thread asks for
    a snapshot a few times a second. Everything is behind one lock because
    those are two different threads.

    A run with a fixed number of loops is measured whole: the bar crosses the
    screen once for all of them. An endless run has no total to measure
    against, so there the bar shows the loop it is in and starts over each time.

    A slot-wrapped run has a lead (the pickup) and a tail (the place) that
    happen once, outside the loops. They are counted in the total but are not
    part of the repeating body, which is why they are tracked separately rather
    than being pushed into the step list — in the list they would be repeated
    every loop and would throw off both the bar and the step text.

    States: idle (nothing shown), running (counting down, possibly held),
    halted (frozen where an abort or an error left it) and done (full bar).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    # ----- worker side -------------------------------------------------------

    def reset(self):
        with self._lock:
            self._state = "idle"
            self._steps: list[str] = []
            self._durations: list[float] = []
            self._cycle_total = 0.0  # planned seconds of one loop
            self._loops: int | None = 1  # None = until abort
            self._cycle = 0
            self._label = ""
            self._index = 0
            self._done = 0.0  # planned seconds already finished
            self._program_start: float | None = None
            self._paused_at: float | None = None
            self._paused_total = 0.0
            self._lead_s = 0.0
            self._tail_s = 0.0
            self._phase = "body"  # "lead" | "body" | "tail"
            self._phase_text = ""

    def start(
        self,
        steps: list[str],
        label: str,
        start_index: int = 0,
        loops: int | None = 1,
        start_cycle: int = 0,
        lead_s: float = 0.0,
        tail_s: float = 0.0,
    ):
        """Begin a run. Whatever came before start_cycle/start_index counts as
        already done, so a continued run shows the bar where the paused one
        left it."""
        durations = [pg.duration(step) for step in steps]
        with self._lock:
            self._state = "running"
            self._steps = list(steps)
            self._durations = durations
            self._cycle_total = sum(durations)
            self._loops = loops
            self._cycle = start_cycle
            self._label = label
            self._index = min(start_index, max(len(steps) - 1, 0))
            self._program_start = None
            self._paused_at = None
            self._paused_total = 0.0
            self._lead_s = lead_s
            self._tail_s = tail_s
            self._phase = "body"
            self._phase_text = ""
            self._recount_done()

    def begin_lead(self, text: str):
        """The slot pickup is starting — before any loop."""
        self._begin_phase("lead", text)

    def begin_tail(self, text: str):
        """The slot place is starting — after every loop."""
        self._begin_phase("tail", text)

    def _begin_phase(self, phase: str, text: str):
        with self._lock:
            if self._state != "running":
                return
            self._phase = phase
            self._phase_text = text
            self._recount_done()
            self._program_start = time.monotonic()
            self._paused_total = 0.0
            self._paused_at = None if self._paused_at is None else time.monotonic()

    def begin_step(self, index: int, cycle: int = 0):
        """The worker is about to run steps[index] in this loop."""
        with self._lock:
            if self._state != "running":
                return
            self._phase = "body"
            self._index = index
            self._cycle = cycle
            self._recount_done()
            self._program_start = time.monotonic()
            self._paused_total = 0.0
            # Only if someone managed to pause in the gap: keep the clock held.
            self._paused_at = None if self._paused_at is None else time.monotonic()

    def pause(self):
        with self._lock:
            if self._paused_at is None:
                self._paused_at = time.monotonic()

    def resume(self):
        with self._lock:
            if self._paused_at is not None:
                self._paused_total += time.monotonic() - self._paused_at
                self._paused_at = None

    def finish(self):
        with self._lock:
            self._state = "done"

    def halt(self):
        """An abort or an error ended the run — freeze the bar where it is."""
        with self._lock:
            if self._state != "running":
                return
            if self._paused_at is None:
                self._paused_at = time.monotonic()
            self._state = "halted"

    def is_running(self) -> bool:
        with self._lock:
            return self._state == "running"

    # ----- UI side -----------------------------------------------------------

    def _body_total(self) -> float:
        """Planned seconds of every loop together. Endless runs measure one."""
        if self._loops is None:
            return self._cycle_total
        return self._cycle_total * self._loops

    def _recount_done(self):
        """Planned seconds behind us. Endless runs measure one loop at a time,
        so there the loops already finished do not count."""
        if self._phase == "lead":
            self._done = 0.0
            return
        if self._phase == "tail":
            self._done = self._lead_s + self._body_total()
            return
        finished_loops = self._cycle if self._loops is not None else 0
        self._done = (
            self._lead_s
            + finished_loops * self._cycle_total
            + sum(self._durations[: self._index])
        )

    def _planned_total(self) -> float:
        return self._lead_s + self._body_total() + self._tail_s

    def _share(self) -> float:
        """How much of the total the thing running now is worth."""
        if self._phase == "lead":
            return self._lead_s
        if self._phase == "tail":
            return self._tail_s
        return self._durations[self._index] if self._durations else 0.0

    def _elapsed_in_program(self) -> float:
        """Seconds the current program has really been going, pauses removed."""
        if self._program_start is None:
            return 0.0
        now = self._paused_at if self._paused_at is not None else time.monotonic()
        return max(0.0, now - self._program_start - self._paused_total)

    def _step_text(self) -> str:
        # The lead and the tail are not in _steps, so they must never index it.
        if self._phase in ("lead", "tail"):
            return f"{self._label}  ·  {self._phase_text}"
        if not self._steps:
            return self._label
        loop = ""
        if self._loops != 1:
            total = "" if self._loops is None else f"/{self._loops}"
            loop = f"  ·  loop {self._cycle + 1}{total}"
        return (
            f"{self._label}{loop}  ·  step {self._index + 1}/{len(self._steps)}:  "
            f"{pg.describe_step(self._steps[self._index])}"
        )

    def snapshot(self) -> Progress:
        with self._lock:
            if self._state == "idle" or not self._steps:
                return Progress(0.0, "", "")

            current = self._elapsed_in_program()
            share = self._share()
            total = self._planned_total() or 1.0
            # Capped at this program's own share: the bar may not overtake the
            # robot just because the estimate was too short.
            fraction = min(1.0, (self._done + min(current, share)) / total)
            left = total - (self._done + current)
            step = self._step_text()
            # An endless run's countdown is for the loop it is in, not the run.
            scope = " in this loop" if self._loops is None else ""

            if self._state == "done":
                return Progress(1.0, "done", f"{self._label} complete")
            if self._state == "halted":
                return Progress(fraction, "stopped", f"stopped at {step}")
            if self._paused_at is not None:
                return Progress(fraction, f"paused · ≈ {self._left_text(left)}", step)
            return Progress(
                fraction, f"≈ {self._left_text(left)} remaining{scope}", step
            )

    @staticmethod
    def _left_text(left: float) -> str:
        # Past the estimate the honest answer is "any moment now", not a
        # countdown that has gone negative.
        return pg.format_duration(left) if left > 0 else "any moment"


class IoStepDialog(ctk.CTkToplevel):
    """Pick an output and what to do with it, for a queue step.

    Hands "io:IO5:pulse:1.5" (built by programs.io_token, the only place a
    token is constructed) to on_accept. The seconds field is only asked for
    when "pulse" is chosen.
    """

    def __init__(self, master, *, button=None, on_accept=None):
        super().__init__(master)
        self.title("Add an I/O step")
        self.resizable(False, False)
        self._on_accept = on_accept
        self._make = button or (
            lambda parent, text, command, **kw: ctk.CTkButton(
                parent, text=text, command=command, **kw
            )
        )
        self.channel_var = ctk.StringVar(value=iow.CHANNELS[0])
        self.action_var = ctk.StringVar(value="on")
        self.seconds_var = ctk.StringVar(value="1.0")

        ctk.CTkLabel(
            self, text="Add an I/O step", font=ctk.CTkFont(size=22, weight="bold")
        ).pack(padx=24, pady=(20, 10))

        ctk.CTkLabel(self, text="Output:", font=ctk.CTkFont(size=16)).pack()
        grid = ctk.CTkFrame(self, fg_color="transparent")
        grid.pack(padx=24, pady=(4, 12))
        self._channel_buttons: dict[str, ctk.CTkButton] = {}
        for index, name in enumerate(iow.CHANNELS):
            btn = self._make(
                grid,
                name,
                lambda key=name: self._pick_channel(key),
                width=110,
                height=52,
                size=16,
            )
            btn.grid(row=index // 4, column=index % 4, padx=4, pady=4)
            self._channel_buttons[name] = btn

        ctk.CTkLabel(self, text="Action:", font=ctk.CTkFont(size=16)).pack()
        ctk.CTkSegmentedButton(
            self,
            values=["on", "off", "pulse"],
            variable=self.action_var,
            height=52,
            font=ctk.CTkFont(size=18, weight="bold"),
            command=lambda _v: self._refresh(),
        ).pack(padx=24, pady=(4, 12))

        self.seconds_row = ctk.CTkFrame(self, fg_color="transparent")
        ctk.CTkLabel(
            self.seconds_row, text="Pulse (s):", font=ctk.CTkFont(size=16)
        ).grid(row=0, column=0, padx=(0, 8))
        entry = ctk.CTkEntry(
            self.seconds_row,
            textvariable=self.seconds_var,
            width=120,
            height=52,
            justify="center",
            font=ctk.CTkFont(size=20),
        )
        entry.grid(row=0, column=1, padx=(0, 8))
        entry.bind(
            "<Button-1>",
            lambda _e: kp.Keypad(
                self,
                title="Pulse (s)",
                initial=self.seconds_var.get().strip(),
                on_accept=self.seconds_var.set,
            ),
        )
        self._make(
            self.seconds_row,
            "123",
            lambda: kp.Keypad(
                self,
                title="Pulse (s)",
                initial=self.seconds_var.get().strip(),
                on_accept=self.seconds_var.set,
            ),
            width=80,
            height=52,
            size=16,
        ).grid(row=0, column=2)

        self.preview_var = ctk.StringVar()
        ctk.CTkLabel(
            self, textvariable=self.preview_var, font=ctk.CTkFont(size=18, weight="bold")
        ).pack(pady=(10, 4))

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(padx=24, pady=(8, 20))
        self._make(
            actions,
            "Cancel",
            self.destroy,
            width=140,
            height=58,
            size=17,
            **theme.SECONDARY,
        ).grid(row=0, column=0, padx=6)
        self._make(actions, "Add", self._accept, width=140, height=58, size=17).grid(
            row=0, column=1, padx=6
        )

        self._refresh()
        self.transient(master)
        self.attributes("-topmost", True)
        self._centre_on(master)
        self.after(60, lambda: self._centre_on(master))
        self.after(120, self._grab)

    def _pick_channel(self, name: str):
        self.channel_var.set(name)
        self._refresh()

    def _token(self) -> str | None:
        action = self.action_var.get()
        if action != "pulse":
            return pg.io_token(self.channel_var.get(), action)
        try:
            seconds = float(self.seconds_var.get().strip())
        except ValueError:
            return None
        if seconds <= 0:
            return None
        return pg.io_token(self.channel_var.get(), "pulse", seconds)

    def _refresh(self):
        if self.action_var.get() == "pulse":
            self.seconds_row.pack(padx=24, pady=(0, 8))
        else:
            self.seconds_row.pack_forget()
        for name, btn in self._channel_buttons.items():
            chosen = name == self.channel_var.get()
            btn.configure(border_width=3 if chosen else 0, border_color=theme.GREEN)
        token = self._token()
        self.preview_var.set(
            pg.describe_step(token) if token else "enter a pulse length in seconds"
        )

    def _accept(self):
        token = self._token()
        if token is None:
            self.preview_var.set("enter a pulse length in seconds")
            return
        self.destroy()
        if self._on_accept is not None:
            self._on_accept(token)

    def _centre_on(self, master):
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _grab(self, attempt: int = 0):
        try:
            self.grab_set()
            self.focus_force()
        except Exception:
            if attempt < 10:
                self.after(50, lambda: self._grab(attempt + 1))


class MediaStepDialog(ctk.CTkToplevel):
    """Pick a picture or clip to put on the panel, for a queue step.

    Offers whatever is in the media folder plus "video off", and hands
    "media:demo.mp4" / "media:off" to on_accept.
    """

    def __init__(self, master, *, files: list[str], button=None, on_accept=None):
        super().__init__(master)
        self.title("Add a video step")
        self.resizable(False, False)
        self._on_accept = on_accept
        make = button or (
            lambda parent, text, command, **kw: ctk.CTkButton(
                parent, text=text, command=command, **kw
            )
        )

        ctk.CTkLabel(
            self, text="Show on the panel", font=ctk.CTkFont(size=22, weight="bold")
        ).pack(padx=24, pady=(20, 10))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(padx=24)
        if files:
            for index, name in enumerate(files):
                make(
                    body,
                    name,
                    lambda n=name: self._choose(pg.media_token(n)),
                    width=330,
                    height=54,
                    size=15,
                ).grid(row=index, column=0, pady=3)
        else:
            ctk.CTkLabel(
                body,
                text="No files in the media folder.\nPut an image or an mp4 there.",
                font=ctk.CTkFont(size=15),
            ).grid(row=0, column=0, pady=8)

        make(
            self,
            "⏹  video off  (back to idle)",
            lambda: self._choose(pg.media_token(None)),
            width=330,
            height=54,
            size=15,
        ).pack(padx=24, pady=(10, 4))
        make(
            self,
            "Cancel",
            self.destroy,
            width=330,
            height=54,
            size=15,
            **theme.SECONDARY,
        ).pack(padx=24, pady=(4, 20))

        self.transient(master)
        self.attributes("-topmost", True)
        self._centre_on(master)
        self.after(60, lambda: self._centre_on(master))
        self.after(120, self._grab)

    def _choose(self, token: str):
        self.destroy()
        if self._on_accept is not None:
            self._on_accept(token)

    def _centre_on(self, master):
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _grab(self, attempt: int = 0):
        try:
            self.grab_set()
            self.focus_force()
        except Exception:
            if attempt < 10:
                self.after(50, lambda: self._grab(attempt + 1))


class RobotAppBase(ctk.CTk):
    """The logic half of a robot GUI — see this module's docstring."""

    # --- what a layout overrides, no code needed ----------------------------
    TITLE = "Cobot Control"
    WINDOW_SIZE = "1280x800"
    MIN_SIZE = (1270, 760)
    START_FULLSCREEN = True
    PRESETS = pg.DEPOWDER_PRESETS
    PROGRAM_LIST = pg.DEPOWDER_PROGRAMS

    BTN_W, BTN_H, BTN_FONT = 300, 76, 20
    HALT_W, HALT_H, HALT_FONT = 370, 120, 34
    SMALL_W, SMALL_H = 210, 68
    PROGRESS_W = 720

    COLOR_ABORT = "#b00020"
    COLOR_ABORT_HOVER = "#7f0017"
    COLOR_PAUSE = "#c77800"
    COLOR_PAUSE_HOVER = "#8f5600"
    # Text on the buttons whose colour a layout may change. The default is
    # CustomTkinter's own button text, so a layout that sets nothing looks as
    # it always did.
    COLOR_ABORT_TEXT = "#DCE4EE"
    COLOR_PAUSE_TEXT = "#DCE4EE"
    COLOR_RESUME = "#2e7d32"
    COLOR_RESUME_HOVER = "#1b5e20"
    COLOR_IO_ON = "#2e7d32"
    COLOR_IO_ON_HOVER = "#1b5e20"
    COLOR_IO_OFF = ("gray70", "gray30")
    COLOR_IO_OFF_HOVER = ("gray60", "gray40")
    COLOR_IO_ON_TEXT = "#DCE4EE"
    COLOR_IO_OFF_TEXT = "#DCE4EE"

    def __init__(self):
        super().__init__()
        self.title(self.TITLE)
        self.geometry(self.WINDOW_SIZE)
        self.minsize(*self.MIN_SIZE)
        # The fullscreen request itself is further down, once _admin exists:
        # _assert_fullscreen() reads that flag to know when to stop insisting.
        #
        # Deliberately no <Escape> binding: on the panel, on_window_mode() and
        # its PIN are the only way out of fullscreen, and a keyboard must not
        # bypass it.

        self._busy = False
        self._last_user_activity = time.monotonic()
        # Only on_close() sets this: "we are shutting down, do not move the arm
        # at all — not even the return program". Demo CANCEL is a different thing.
        self._stop_flag = threading.Event()
        # Demo CANCEL: finish the active snippet, then return to Home if needed.
        self._abort_flag = threading.Event()
        self._cancel_after_current = threading.Event()
        self._returning = False
        # Held by PAUSE: the robot pauses its program, and the worker parks
        # before starting the next one instead of running on.
        self._pause_flag = threading.Event()
        self._demo_logout_requested = threading.Event()
        self._demo_pause_enabled = False
        self._demo_cancel_enabled = False
        self._demo_logo_tap_count = 0
        self._demo_logo_last_tap_at = 0.0
        self._demo_idle_tap_count = 0
        self._demo_idle_last_tap_at = 0.0
        self.demo_pause_button: ctk.CTkButton | None = None
        self.demo_cancel_button: ctk.CTkButton | None = None
        self._resume: ResumePoint | None = None
        # Where we believe the arm is. A belief, not a reading: nothing here
        # ever asks the robot for its pose. None means we no longer know, and
        # Demo CANCEL will refuse to drive rather than guess.
        self._position: str | None = pg.HOME
        self._last_step: str | None = None
        self.timer = RunTimer()
        self._admin = False
        # Who is logged in: a key of programs.ROLES, or None on the login
        # screen. Everything a role may do is read from pg.ROLES via _can().
        self._role: str | None = None
        self._last_user_activity = time.monotonic()
        self._demo_screensaver_visible = False
        self._demo_screensaver_armed = False
        self.role_var = ctk.StringVar(value="")
        self.logout_var = ctk.StringVar(value="Log out")
        # The Settings view (Developer Mode only), filled by the layout.
        self.settings_frame: ctk.CTkFrame | None = None
        self.pin_vars: dict[str, ctk.StringVar] = {}
        self.robot_ip_var = ctk.StringVar(value="")
        self.esc_var = ctk.BooleanVar(value=False)
        # The view that was showing before Settings, for its Back button.
        self._before_settings = None
        # Buttons that only some roles may see, as (widget, right). The layout
        # grids them as usual and registers them; _apply_role() then hides the
        # ones the logged-in role does not have (grid_remove keeps their place).
        self.role_widgets: list[tuple[object, str]] = []
        self.role_pack_widgets: list[tuple[object, str, dict]] = []
        self.login_frame: ctk.CTkFrame | None = None
        # The windowed/fullscreen toggle, set by the layout if it has one; its
        # label is flipped by _refresh_window_mode_button(). Stays None for the
        # older GUIs, and every use of it is guarded.
        self.window_mode_button = None
        # What minsize() was told at startup, so leaving fullscreen may relax it
        # for a small screen and going back can put it straight again.
        self._min_size = tuple(self.MIN_SIZE)

        if self.START_FULLSCREEN:
            self._assert_fullscreen()

        self.queue: list[str] = []
        self.status_var = ctk.StringVar(value="Idle")
        self.safety_status_var = ctk.StringVar(value="Safety: checking")
        self.idle_countdown_var = ctk.StringVar(value="")
        self.inactivity_prompt_var = ctk.StringVar(value="")
        self._inactivity_prompt: ctk.CTkToplevel | None = None
        self._estop_dialog: ctk.CTkToplevel | None = None
        self._estop_message = None
        self._safety_error: str | None = None
        self._safety_stop = threading.Event()
        self.chain_var = ctk.StringVar(value="")
        self.total_var = ctk.StringVar(value="")
        self.payload_var = ctk.StringVar()
        self.loop_var = ctk.StringVar(value=LOOP_CHOICES[0])
        self.slot_var = ctk.StringVar(value=pg.SLOTS[pg.DEFAULT_SLOT].label)
        self.step_var = ctk.StringVar(value="")
        self.remaining_var = ctk.StringVar(value="")
        self.io_backend_var = ctk.StringVar(value="")

        # Registries the layout fills, so nothing here names a widget.
        self.run_buttons: list[ctk.CTkButton] = []
        self.pause_buttons: list[ctk.CTkButton] = []
        self.abort_buttons: list[ctk.CTkButton] = []
        self.progress_bars: list[ctk.CTkProgressBar] = []
        self.loop_pickers: list[ctk.CTkSegmentedButton] = []
        self.slot_pickers: list[ctk.CTkSegmentedButton] = []
        self.preset_buttons: dict[str, ctk.CTkButton] = {}
        self.add_buttons: dict[str, ctk.CTkButton] = {}
        self.io_buttons: dict[str, ctk.CTkButton] = {}
        self._views: list[ctk.CTkFrame] = []

        # Optional: a layout without a queue or a media panel leaves these.
        self.queue_box: ctk.CTkTextbox | None = None
        self.start_queue_button: ctk.CTkButton | None = None
        self.main_frame: ctk.CTkFrame | None = None
        self.media = None

        # What the status line said before a hover explained an add-button, so
        # leaving it puts the old text back without overwriting something a
        # running worker wrote meanwhile.
        self._hover_text: str | None = None
        self._hover_restore = ""

        self._build_views()
        # The number pad, built now and hidden, so a PIN prompt only has to show
        # it (keypad.py) — no window is created while someone waits to type.
        kp.prepare(self)
        self._refresh_queue_display()
        # A layout with a login view starts there; one without runs as before.
        if self.login_frame is not None:
            self.show_login()
        else:
            self.show_main()
        self._tick()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        # ESC to leave fullscreen — only if Developer Mode allowed it in
        # Settings. Bound on the main window only: the number pad has its own
        # window, where ESC still just closes the pad.
        self.bind("<Escape>", self._on_escape)
        if self.login_frame is not None:
            for sequence in (
                "<ButtonPress-1>",
                "<ButtonPress-2>",
                "<ButtonPress-3>",
                "<Motion>",
                "<KeyPress>",
            ):
                self.bind_all(sequence, self._on_user_activity, add="+")
            self.after(INACTIVITY_TICK_MS, self._tick_inactivity)
            self.after(0, self._start_safety_monitor)

    # ----- what the layout implements ----------------------------------------

    def _build_views(self):
        raise NotImplementedError("a layout must build its own views")

    def _button(self, parent, text, command, *, width=None, height=None, size=None, **kw):
        """A button in the layout's style. Layouts override this."""
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width or self.BTN_W,
            height=height or self.BTN_H,
            font=ctk.CTkFont(size=size or self.BTN_FONT, weight="bold"),
            **kw,
        )

    def _parse_safety_state(self, response: str) -> str:
        prefix, separator, value = response.partition(":")
        state = value.strip().upper()
        if (
            not separator
            or prefix.strip().lower() != "safetystatus"
            or state not in SAFETY_STATES
        ):
            raise ValueError(f"Unexpected Dashboard safety status: {response!r}")
        return state

    def _start_safety_monitor(self):
        if not self._safety_stop.is_set():
            threading.Thread(target=self._monitor_safety, daemon=True).start()

    def _monitor_safety(self):
        while not self._safety_stop.is_set():
            try:
                state = self._parse_safety_state(rc.safety_status())
            except Exception as exc:
                self._post(lambda error=str(exc): self._update_safety_status(None, error))
            else:
                self._post(lambda current=state: self._update_safety_status(current))
            self._safety_stop.wait(SAFETY_POLL_INTERVAL_S)

    def _update_safety_status(self, state: str | None, error: str | None = None):
        if state is None:
            self.safety_status_var.set("Safety: UNAVAILABLE")
            if error != self._safety_error:
                print(f"Robot safety status unavailable: {error}")
                self.status_var.set(f"Robot safety status unavailable: {error}")
                self._safety_error = error
            if self._estop_dialog is not None:
                self._estop_dialog.lift()
            return

        self._safety_error = None
        self.safety_status_var.set(f"Safety: {state}")
        if state in EMERGENCY_STOP_STATES:
            self._show_estop_dialog(state)
        else:
            self._close_estop_dialog()

    def _show_estop_dialog(self, state: str):
        dialog = self._estop_dialog
        if dialog is None or not dialog.winfo_exists():
            dialog = ctk.CTkToplevel(self)
            dialog.title("Emergency stop active")
            dialog.geometry("620x240")
            dialog.resizable(False, False)
            dialog.transient(self)
            dialog.protocol("WM_DELETE_WINDOW", lambda: None)
            dialog.attributes("-topmost", True)
            ctk.CTkLabel(
                dialog,
                text="EMERGENCY STOP",
                text_color=theme.BLUE_DARK,
                font=ctk.CTkFont(size=30, weight="bold"),
            ).pack(pady=(32, 12))
            self._estop_message = ctk.CTkLabel(
                dialog,
                text="",
                text_color=theme.BLUE_DARK,
                font=ctk.CTkFont(size=20),
                wraplength=560,
            )
            self._estop_message.pack(expand=True, padx=24, pady=(0, 24))
            self._estop_dialog = dialog
            dialog.update_idletasks()
            x = self.winfo_rootx() + max(0, (self.winfo_width() - dialog.winfo_width()) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - dialog.winfo_height()) // 2)
            dialog.geometry(f"+{x}+{y}")
            dialog.grab_set()

        source = "Teach pendant" if state == "ROBOT_EMERGENCY_STOP" else "External safety circuit"
        self._estop_message.configure(
            text=f"{state.replace('_', ' ')} detected ({source}). "
            "This message will close when the emergency-stop state clears."
        )
        dialog.lift()

    def _close_estop_dialog(self):
        dialog = self._estop_dialog
        self._estop_dialog = None
        if dialog is not None and dialog.winfo_exists():
            dialog.grab_release()
            dialog.destroy()

    # ----- widget factories --------------------------------------------------

    def _make_abort_button(self, parent):
        """ABORT. Registered so every copy shows the same state."""
        btn = self._button(
            parent,
            "CANCEL",
            self.on_abort,
            width=self.HALT_W,
            height=self.HALT_H,
            size=self.HALT_FONT,
            fg_color=self.COLOR_ABORT,
            hover_color=self.COLOR_ABORT_HOVER,
            text_color=self.COLOR_ABORT_TEXT,
        )
        self.abort_buttons.append(btn)
        if self.login_frame is not None:
            self.role_widgets.append((btn, "abort"))
        return btn

    def _make_pause_button(self, parent):
        """Pause/resume toggle. Every instance is registered so they all show
        the same state, whichever view it was pressed on."""
        btn = self._button(
            parent,
            "PAUSE",
            self.on_pause_resume,
            width=self.HALT_W,
            height=self.HALT_H,
            size=self.HALT_FONT,
            fg_color=self.COLOR_PAUSE,
            hover_color=self.COLOR_PAUSE_HOVER,
            text_color=self.COLOR_PAUSE_TEXT,
        )
        self.pause_buttons.append(btn)
        return btn

    def _make_progress_block(self, parent, compact: bool = False):
        """Current step, the bar, and the time left. One per view, all fed from
        the same timer by _tick().

        compact=True puts the step and the time left on one line above the bar
        — about half the height, for the views that are short of space."""
        block = ctk.CTkFrame(parent, fg_color="transparent")
        if compact:
            block.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                block, textvariable=self.step_var, font=ctk.CTkFont(size=17),
                height=26, anchor="w",
            ).grid(row=0, column=0, sticky="w")
            ctk.CTkLabel(
                block, textvariable=self.remaining_var,
                font=ctk.CTkFont(size=20, weight="bold"), height=26, anchor="e",
            ).grid(row=0, column=1, sticky="e", padx=(12, 0))
            bar = ctk.CTkProgressBar(block, width=self.PROGRESS_W, height=22)
            bar.set(0.0)
            bar.grid(row=1, column=0, columnspan=2, pady=(2, 0))
            self.progress_bars.append(bar)
            return block
        ctk.CTkLabel(
            block, textvariable=self.step_var, font=ctk.CTkFont(size=17), height=24
        ).pack()
        bar = ctk.CTkProgressBar(block, width=self.PROGRESS_W, height=26)
        bar.set(0.0)
        bar.pack(pady=6)
        self.progress_bars.append(bar)
        ctk.CTkLabel(
            block,
            textvariable=self.remaining_var,
            font=ctk.CTkFont(size=26, weight="bold"),
            height=34,
        ).pack()
        return block

    def _make_loop_row(self, parent):
        """How often the run repeats. All instances share self.loop_var."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(row, text="Repeat:", font=ctk.CTkFont(size=18)).grid(
            row=0, column=0, padx=(0, 10)
        )
        picker = ctk.CTkSegmentedButton(
            row,
            values=list(LOOP_CHOICES),
            variable=self.loop_var,
            height=56,
            font=ctk.CTkFont(size=19, weight="bold"),
            command=lambda _value: self._on_run_shape_changed(),
        )
        picker.grid(row=0, column=1)
        self.loop_pickers.append(picker)
        return row

    def _make_slot_row(self, parent):
        """Which station the part is taken from and put back into.

        A preset run is wrapped in the chosen slot's pickup and place programs.
        The queue is not wrapped — it runs exactly what is in it.
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(row, text="Slot:", font=ctk.CTkFont(size=18)).grid(
            row=0, column=0, padx=(0, 10)
        )
        picker = ctk.CTkSegmentedButton(
            row,
            values=[pg.SLOTS[key].label for key in pg.SLOT_ORDER],
            variable=self.slot_var,
            height=56,
            font=ctk.CTkFont(size=19, weight="bold"),
            command=lambda _value: self._on_run_shape_changed(),
        )
        picker.grid(row=0, column=1)
        self.slot_pickers.append(picker)
        return row

    def _make_payload_row(self, parent, compact: bool = False):
        """Show the retired payload control as disabled on every view."""
        entry_w, pad_w, submit_w = (130, 72, 116) if compact else (180, 90, 150)
        row = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(
            row, text="Payload (g) — disabled:", text_color=theme.TEXT_GREY,
            font=ctk.CTkFont(size=18),
        ).grid(
            row=0, column=0, padx=(0, 10)
        )
        entry = ctk.CTkEntry(
            row,
            textvariable=self.payload_var,
            placeholder_text="e.g. 500",
            width=entry_w,
            height=self.BTN_H,
            justify="center",
            font=ctk.CTkFont(size=24),
            state="disabled",
            text_color=theme.TEXT_GREY,
            fg_color=theme.PANEL,
        )
        entry.grid(row=0, column=1, padx=(0, 10))
        self._button(
            row, "123", lambda: None, width=pad_w, state="disabled",
            text_color=theme.TEXT_GREY,
        ).grid(
            row=0, column=2, padx=(0, 10)
        )
        self._button(
            row, "Submit", self.on_payload_submit, width=submit_w,
            state="disabled", text_color=theme.TEXT_GREY,
        ).grid(
            row=0, column=3, padx=(0, 0 if compact else 14)
        )
        if not compact:
            ctk.CTkLabel(
                row, text="Payload data is not sent", text_color=theme.TEXT_GREY,
                font=ctk.CTkFont(size=16),
            ).grid(row=0, column=4)
        return row

    def _make_io_grid(self, parent, columns: int = 3):
        """One toggle button per output channel, filling self.io_buttons."""
        grid = ctk.CTkFrame(parent, fg_color="transparent")
        for index, name in enumerate(iow.CHANNELS):
            btn = self._button(
                grid,
                name,
                lambda key=name: self.on_io_toggle(key),
                width=self.SMALL_W,
                height=self.SMALL_H,
                size=17,
            )
            btn.grid(row=index // columns, column=index % columns, padx=6, pady=6)
            self.io_buttons[name] = btn
        return grid

    def _status_label(self, parent):
        return ctk.CTkLabel(
            parent,
            textvariable=self.status_var,
            font=ctk.CTkFont(size=16),
            wraplength=1100,
        )

    # ----- view switching ----------------------------------------------------

    def _register_view(self, frame):
        """Every view is laid out once, all of them stacked on the same spot.

        Switching (_show) only raises one to the top. The old way — unpack the
        current view, pack the next — had Tk take down and put back the ~150
        small windows a page of CustomTkinter widgets is made of, and on the Pi
        you could watch the page assemble itself. Raised, a view is already
        complete underneath and simply comes to the front.
        """
        self._views.append(frame)
        frame.place(x=0, y=0, relwidth=1, relheight=1)
        return frame

    def _show(self, frame):
        frame.tkraise()
        self._current_view = frame
        if frame is getattr(self, "expert_frame", None):
            self._last_user_activity = time.monotonic()
        else:
            self.idle_countdown_var.set("")
            self._close_inactivity_prompt()

    def current_view(self):
        return getattr(self, "_current_view", None)

    def _inactivity_scope_active(self) -> bool:
        return bool(
            self.login_frame is not None
            and self._role
            and self._can("expert")
            and self.current_view() is getattr(self, "expert_frame", None)
        )

    def _on_user_activity(self, _event=None):
        if _event is not None and getattr(_event.widget, "tk", None) is not self.tk:
            return
        self._last_user_activity = time.monotonic()
        if self._demo_screensaver_visible:
            self._hide_demo_screensaver()
        if self._inactivity_scope_active():
            self.idle_countdown_var.set("")
            self._close_inactivity_prompt()

    def _show_inactivity_prompt(self, seconds_left: int):
        dialog = self._inactivity_prompt
        if dialog is None or not dialog.winfo_exists():
            dialog = ctk.CTkToplevel(self)
            dialog.title("Still using Expert Mode?")
            dialog.geometry("440x190")
            dialog.resizable(False, False)
            dialog.transient(self)
            dialog.protocol("WM_DELETE_WINDOW", self._on_user_activity)
            self._inactivity_prompt = dialog
            ctk.CTkLabel(
                dialog,
                textvariable=self.inactivity_prompt_var,
                font=ctk.CTkFont(size=19, weight="bold"),
                wraplength=390,
            ).pack(expand=True, padx=24, pady=(20, 8))
            ctk.CTkButton(
                dialog,
                text="Stay logged in",
                command=self._on_user_activity,
                width=260,
                height=60,
                font=ctk.CTkFont(size=18, weight="bold"),
                **theme.button_style("primary"),
            ).pack(pady=(0, 20))
            dialog.update_idletasks()
            x = self.winfo_rootx() + max(0, (self.winfo_width() - dialog.winfo_width()) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - dialog.winfo_height()) // 2)
            dialog.geometry(f"+{x}+{y}")
            dialog.lift()
        self.inactivity_prompt_var.set(
            f"This session will log out in {seconds_left} seconds."
        )

    def _close_inactivity_prompt(self):
        dialog = self._inactivity_prompt
        self._inactivity_prompt = None
        if dialog is not None:
            try:
                dialog.destroy()
            except Exception:
                pass

    def _inactivity_is_blocked(self) -> bool:
        if self._busy or self._returning or self._resume is not None or self._pause_flag.is_set():
            return True
        try:
            return any(iow.states().values())
        except Exception:
            return True

    def _tick_inactivity(self):
        if not self.winfo_exists():
            return
        if self._inactivity_scope_active():
            now = time.monotonic()
            if self._inactivity_is_blocked():
                self._last_user_activity = now
                self.idle_countdown_var.set("")
            else:
                remaining = INACTIVITY_TIMEOUT_S - (now - self._last_user_activity)
                if remaining <= 0:
                    self.idle_countdown_var.set("")
                    self.on_logout()
                elif remaining <= INACTIVITY_WARNING_S:
                    seconds_left = math.ceil(remaining)
                    self.idle_countdown_var.set(
                        f"Auto logout in {seconds_left} s"
                    )
                    self._show_inactivity_prompt(seconds_left)
                else:
                    self.idle_countdown_var.set("")
                    self._close_inactivity_prompt()
        else:
            self.idle_countdown_var.set("")
            self._close_inactivity_prompt()
            keypad = getattr(self, "_keypad_overlay", None)
            if (
                self._role == "Demo"
                and self._demo_screensaver_armed
                and not self._demo_screensaver_visible
                and not self._pause_flag.is_set()
                and not (keypad is not None and keypad.is_open())
                and time.monotonic() - self._last_user_activity
                >= DEMO_SCREENSAVER_TIMEOUT_S
            ):
                if not self._show_demo_screensaver():
                    self._last_user_activity = time.monotonic()
        self.after(INACTIVITY_TICK_MS, self._tick_inactivity)

    def show_main(self):
        self._show(self.main_frame)

    def show_expert(self):
        """Open the queue — if the logged-in role may. The login PIN is the
        gate now, so there is no second PIN prompt here."""
        if not self._can("expert"):
            self.status_var.set(f"Expert Mode is not available for {self._role or 'nobody'}")
            return
        self.status_var.set("Expert mode")
        self._show(self.expert_frame)

    # ----- users -------------------------------------------------------------

    def _can(self, right: str) -> bool:
        """Whether the logged-in role has `right` ("expert", "io", "windowed")."""
        role = pg.ROLES.get(self._role) if self._role else None
        if right.startswith("preset:"):
            return bool(
                role
                and (role.presets is None or right.removeprefix("preset:") in role.presets)
            )
        if right == "non_demo":
            return not bool(role and role.demo)
        return bool(role and getattr(role, right, False))

    def show_login(self):
        self.role_var.set("")
        self._show(self.login_frame)

    def on_login(self, role_label: str):
        """Log in as `role_label`: straight away if its PIN is empty, else ask."""
        role = pg.ROLES.get(role_label)
        if role is None:
            self.status_var.set(f"Unknown user {role_label!r}")
            return
        if not role.pin:
            self._login_ok(role_label)
            return
        kp.Keypad(
            self,
            title=f"{role_label} — PIN",
            mask=True,
            allow_dot=False,
            on_accept=lambda value, label=role_label: self._check_login_pin(label, value),
        )

    def _check_login_pin(self, role_label: str, value: str):
        if value == pg.ROLES[role_label].pin:
            self._login_ok(role_label)
        else:
            self.status_var.set(f"Wrong PIN for {role_label}")

    def _login_ok(self, role_label: str):
        self._role = role_label
        self._demo_pause_enabled = False
        self._demo_cancel_enabled = False
        self._demo_logo_tap_count = 0
        self._demo_logo_last_tap_at = 0.0
        self._demo_idle_tap_count = 0
        self._demo_idle_last_tap_at = 0.0
        self._last_user_activity = time.monotonic()
        self._demo_screensaver_armed = False
        self.role_var.set(role_label)
        # Square tile: one word per line ("Log out / Super / User").
        self.logout_var.set("Log out\n" + role_label.replace(" ", "\n"))
        self._apply_role()
        self.status_var.set(f"Logged in as {role_label}")
        self.show_main()

    def on_logout(self):
        """Return to the login screen, confirming the Demo PIN when needed.

        A Demo run may only be logged out after it is paused; confirmation then
        stops the paused Dashboard program before leaving the Demo session.
        """
        demo_paused = (
            self._role == "Demo"
            and self._busy
            and self._pause_flag.is_set()
        )
        if (self._busy or self._returning or self._resume is not None) and not demo_paused:
            self.status_var.set("Log out is possible once the run has ended or been cancelled")
            return
        if self._role == "Demo":
            kp.Keypad(
                self,
                title="Demo — confirm PIN to log out",
                mask=True,
                allow_dot=False,
                on_accept=self._check_demo_logout_pin,
            )
            return
        self._complete_logout()

    def _check_demo_logout_pin(self, value: str):
        if self._role != "Demo":
            return
        if value != pg.ROLES["Demo"].pin:
            self.status_var.set("Wrong Demo PIN — still logged in")
            return
        if self._busy:
            if not self._pause_flag.is_set():
                self.status_var.set("Demo is running — pause it before logging out")
                return
            try:
                state = rc.program_state().upper()
            except Exception as exc:
                self.status_var.set(f"Could not check the paused Demo program: {exc}")
                return
            if "PAUSED" in state:
                try:
                    answer = rc.stop()
                except Exception as exc:
                    self.status_var.set(f"Could not stop the paused Demo program: {exc}")
                    return
                if not answer.lower().startswith("stopped"):
                    self.status_var.set(f"Could not stop the paused Demo program: {answer}")
                    return
            elif "STOPPED" not in state:
                self.status_var.set(
                    f"Demo is not confirmed paused ({state}) — keep the session open"
                )
                return
            self._demo_logout_requested.set()
            self._pause_flag.clear()
            self._resume = None
            self._position = None
            self.timer.halt()
        self._complete_logout()

    def _complete_logout(self):
        if self._admin:  # a Developer left the panel windowed — lock it again
            self._enter_fullscreen()
        role = self._role
        self._role = None
        self._demo_pause_enabled = False
        self._demo_cancel_enabled = False
        self._demo_logo_tap_count = 0
        self._demo_logo_last_tap_at = 0.0
        self._demo_idle_tap_count = 0
        self._demo_idle_last_tap_at = 0.0
        self._demo_screensaver_armed = False
        self._apply_role()
        self.status_var.set(f"{role} logged out" if role else "Logged out")
        self.show_login()

    def _apply_role(self):
        """Show the buttons the logged-in role may use, hide the rest."""
        for widget, right in self.role_widgets:
            try:
                if self._can(right):
                    widget.grid()
                else:
                    widget.grid_remove()
            except Exception:
                pass
        for widget, right, options in self.role_pack_widgets:
            if self._can(right):
                widget.pack(**options)
            else:
                widget.pack_forget()
        if self.preset_buttons:
            self._on_run_shape_changed()
        self._refresh_pause_buttons()

    # ----- settings (Developer Mode) -----------------------------------------

    def show_settings(self):
        if not self._can("settings"):
            self.status_var.set(f"Settings are not available for {self._role or 'nobody'}")
            return
        self._refresh_settings()
        self._before_settings = self.expert_frame
        self.status_var.set("Settings — changes are saved at once")
        self._show(self.settings_frame)

    def settings_back(self):
        self._show(self._before_settings or self.main_frame)

    def _refresh_settings(self):
        for label, var in self.pin_vars.items():
            value = st.pin(label)
            var.set("•" * len(value) if value else "no PIN")
        self.robot_ip_var.set(st.get("robot_ip"))
        self.esc_var.set(bool(st.get("esc_windowed")))

    def on_change_pin(self, label: str):
        """New PIN, twice, on the number pad — then saved to settings.json."""
        kp.Keypad(
            self,
            title=f"New PIN — {label}",
            mask=True,
            allow_dot=False,
            on_accept=lambda value: self._confirm_pin(label, value),
        )

    def _confirm_pin(self, label: str, first: str):
        problem = st.pin_problem(label, first)
        if problem:
            self.status_var.set(f"PIN for {label} not changed: {problem}")
            return
        kp.Keypad(
            self,
            title=f"Repeat PIN — {label}",
            mask=True,
            allow_dot=False,
            on_accept=lambda second: self._save_pin(label, first, second),
        )

    def _save_pin(self, label: str, first: str, second: str):
        if first != second:
            self.status_var.set(f"PIN for {label} not changed: the two entries differ")
            return
        try:
            problem = st.set_pin(label, first)
        except Exception as e:
            problem = f"could not save ({e})"
        if problem:
            self.status_var.set(f"PIN for {label} not changed: {problem}")
        else:
            self.status_var.set(f"PIN for {label} changed and saved")
        self._refresh_settings()

    def on_change_robot_ip(self):
        kp.Keypad(
            self,
            title="Robot IP (UR30)",
            initial=st.get("robot_ip"),
            ip=True,
            on_accept=self._save_robot_ip,
        )

    def _save_robot_ip(self, value: str):
        if self._busy:
            self.status_var.set("Robot IP not changed while a run is going")
            return
        try:
            problem = st.set_robot_ip(value)
        except Exception as e:
            problem = f"could not save ({e})"
        if problem:
            self.status_var.set(f"Robot IP not changed: {value!r} is {problem}")
        else:
            self.status_var.set(
                f"Robot IP set to {value} — run 'sudo ./setup_ur_link.sh' if the Pi's own "
                "address has to move to the new subnet"
            )
        self._refresh_settings()

    def on_esc_toggle(self):
        enabled = bool(self.esc_var.get())
        try:
            st.set_esc_windowed(enabled)
        except Exception as e:
            self.status_var.set(f"Not saved: {e}")
            return
        self.status_var.set(
            "ESC now switches between Windows Mode and fullscreen"
            if enabled
            else "ESC disabled — the panel stays locked in fullscreen"
        )

    def on_settings_reset(self):
        st.reset()
        self._refresh_settings()
        self.status_var.set("Settings reset to the defaults in programs.py / robotControl.py")

    def pi_addresses(self) -> str:
        """The Pi's own IPv4 addresses, for display only."""
        try:
            import subprocess

            out = subprocess.run(
                ["hostname", "-I"], capture_output=True, text=True, timeout=2
            ).stdout.split()
            v4 = [a for a in out if a.count(".") == 3]
            return ", ".join(v4) or "none"
        except Exception:
            return "unknown"

    def show_io(self):
        if not self._can("io"):
            self.status_var.set(f"I/O is not available for {self._role or 'nobody'}")
            return
        # Opening the view is what first claims the pins, so the backend is
        # only known from here on.
        self.io_backend_var.set(iow.describe())
        self._refresh_io_buttons()
        self._show(self.io_frame)

    # ----- Windows Mode / fullscreen ------------------------------------------

    def on_window_mode(self):
        """Toggle between fullscreen and a normal window.

        The app runs fullscreen and ESC does nothing unless Settings allow it,
        so this is normally the only way to reach the desktop underneath. Only a role with
        windowed=True (Developer Mode) may — its login PIN is the gate. Going
        back into fullscreen is always allowed; it only ever locks more.
        """
        if self._admin:
            self._enter_fullscreen()
            return
        if not self._can("windowed"):
            self.status_var.set(f"Windows Mode is not available for {self._role or 'nobody'}")
            return
        self._leave_fullscreen()

    def _leave_fullscreen(self):
        try:
            self._go_windowed()
        except Exception as e:
            self.status_var.set(f"Could not leave fullscreen: {e}")
            return
        self._admin = True
        self._refresh_window_mode_button()
        self.status_var.set("Windows Mode — Fullscreen Mode (or ESC, if allowed) locks the panel again")

    def _on_escape(self, _event=None):
        """ESC toggles Windows Mode, but only with the box ticked in Settings.
        Without it the key does nothing and the panel stays as locked as before."""
        if not st.get("esc_windowed"):
            return
        if self._admin:
            self._enter_fullscreen()
        else:
            self._leave_fullscreen()

    # The button was called "Admin mode" before it could go both ways. Kept so
    # a layout that still wires that name keeps working.
    on_admin_mode = on_window_mode

    def _go_windowed(self):
        """Leave fullscreen for a window that fits on the screen it is on.

        Size and position are both spelled out on purpose. Asking only for a
        size leaves the placement to the window manager, and on a panel whose
        screen is no bigger than WINDOW_SIZE that puts the title bar off the top
        edge — with no keyboard and no way to drag it, the window is then stuck
        where it landed. So the window is placed by hand, and minsize is relaxed
        to match: minsize would otherwise veto a shrink and hand the same
        unreachable window back.

        Three sizes are weighed against each other: WINDOW_SIZE is the ceiling,
        WINDOWED_SCREEN_FRACTION of the screen is what we would like, and
        MIN_SIZE is the floor, because below it the bottom row of the expert
        view — the button that gets back to fullscreen — starts to clip. The
        floor wins wherever the screen can still hold it, and the screen wins
        over the floor when it cannot.
        """
        try:
            want_w, want_h = (int(n) for n in self.WINDOW_SIZE.split("+")[0].split("x"))
        except Exception:
            want_w, want_h = 1280, 800

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        # The band of screen left over once the title bar has its strip.
        avail_w, avail_h = sw, max(1, sh - WINDOWED_TOP_MARGIN)
        w = min(
            want_w,
            max(int(sw * WINDOWED_SCREEN_FRACTION), min(self._min_size[0], avail_w)),
        )
        h = min(
            want_h,
            max(int(sh * WINDOWED_SCREEN_FRACTION), min(self._min_size[1], avail_h)),
        )
        x = max(0, (sw - w) // 2)
        y = min(max(WINDOWED_TOP_MARGIN, (sh - h) // 2), max(0, sh - h))
        where = f"{w}x{h}+{x}+{y}"

        self.minsize(min(self._min_size[0], w), min(self._min_size[1], h))
        self.attributes("-fullscreen", False)
        self.geometry(where)
        # Several window managers ignore a position asked for in the same breath
        # as leaving fullscreen, so it is asked for once more a moment later.
        self.after(120, lambda: self._reapply_geometry(where))

    def _reapply_geometry(self, where: str):
        """Second attempt at a geometry, ignored if we are fullscreen again."""
        if not self._admin:
            return
        try:
            self.geometry(where)
        except Exception:
            pass

    def _enter_fullscreen(self):
        """Back to fullscreen, and locked again: the PIN is needed to get out."""
        # Before the request, not after: _assert_fullscreen() stops as soon as
        # it sees _admin, and a stale True would make it give up immediately.
        self._admin = False
        try:
            self.minsize(*self._min_size)
        except Exception:
            pass
        self._assert_fullscreen()
        self._refresh_window_mode_button()
        self.status_var.set("Fullscreen")

    def _assert_fullscreen(self, tries: int = FULLSCREEN_RETRY_TRIES):
        """Ask for fullscreen, and keep asking until it actually took.

        Launched from an autostart script, the window manager may not be up when
        the first request goes out; Tk raises nothing and the panel simply stays
        a window, while the toggle still claims to be locked. Re-asserting costs
        nothing once it has taken, and stops the moment the PIN says otherwise.
        """
        if self._admin:  # the PIN was entered meanwhile — do not fight the user
            return
        try:
            if self._looks_fullscreen():
                return
            # Cleared before it is set again, and not simply set: Tk only acts
            # on a *change* of the attribute, and it still believes the request
            # it made before the window manager was there to hear it.
            self.attributes("-fullscreen", False)
            self.attributes("-fullscreen", True)
        except Exception as e:
            # hasattr: the first call comes from __init__, before status_var
            # exists — there is nowhere to put the message yet, and the retry
            # is the answer to an early failure anyway.
            if tries <= 0 and hasattr(self, "status_var"):
                self.status_var.set(f"Could not go fullscreen: {e}")
        if tries > 0:
            self.after(FULLSCREEN_RETRY_MS, lambda: self._assert_fullscreen(tries - 1))

    def _looks_fullscreen(self) -> bool:
        """Whether the window really covers the screen.

        Deliberately not attributes("-fullscreen"): that reports the value Tk
        was last told, which stays 1 even when the window manager dropped the
        request — which is the exact case this whole retry exists for. The
        geometry is the only honest answer, and an unmapped window has none.
        """
        if not self.winfo_ismapped():
            return False
        return (
            self.winfo_width() >= self.winfo_screenwidth()
            and self.winfo_height() >= self.winfo_screenheight()
        )

    def _refresh_window_mode_button(self):
        """Label the toggle with what it does next, not with where we are."""
        if self.window_mode_button is None:
            return
        self.window_mode_button.configure(
            text="Fullscreen Mode" if self._admin else "Windows Mode"
        )

    # ----- helpers -----------------------------------------------------------

    def _post(self, fn):
        """Run fn on the UI thread, from wherever.

        Every hop out of the worker goes through here. The try/except is not
        decoration: once the window is being torn down, after() raises, and a
        worker still finishing a program would otherwise die on its next status
        line instead of winding up cleanly.
        """
        try:
            self.after(0, fn)
        except Exception:
            pass

    def _set_status(self, text: str):
        # Worker threads must not touch widgets; marshal to the UI thread.
        self._post(lambda: self.status_var.set(text))

    def _set_controls_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for btn in self.run_buttons:
            btn.configure(state=state)
        # Changing the repeat count or the slot mid-run would not do anything,
        # so they are out of reach until the run is over.
        for picker in self.loop_pickers + self.slot_pickers:
            picker.configure(state=state)

    def _tick(self):
        """Redraw the countdown. The only place the UI reads the timer."""
        if not self.winfo_exists():
            return  # the window is going away; stop rescheduling
        progress = self.timer.snapshot()
        # Only what changed: idle, nothing does, and a bar set to the value it
        # already has still redraws itself.
        shown = (progress.fraction, progress.step, progress.remaining)
        if shown != getattr(self, "_tick_shown", None):
            self._tick_shown = shown
            for bar in self.progress_bars:
                bar.set(progress.fraction)
            self.step_var.set(progress.step)
            self.remaining_var.set(progress.remaining)
        self.after(TICK_MS, self._tick)

    # ----- loops and slots ---------------------------------------------------

    def _loops(self) -> int | None:
        """How often the next run repeats — None for "until aborted"."""
        choice = self.loop_var.get()
        if choice == LOOP_ENDLESS:
            return None
        try:
            return max(1, int(choice.lstrip("×x")))
        except ValueError:
            return 1

    def _slot(self) -> str | None:
        """The key of the slot the picker is on, or None if there is no picker."""
        label = self.slot_var.get()
        for key in pg.SLOT_ORDER:
            if pg.SLOTS[key].label == label:
                return key
        return None

    def _loop_where(self, cycle: int, loops: int | None) -> str:
        """" (loop 2/5)" for a status line, "" for a single run."""
        if loops == 1:
            return ""
        total = "" if loops is None else f"/{loops}"
        return f" (loop {cycle + 1}{total})"

    def _repeat_refusal(self, steps: list[str], label: str) -> str | None:
        """Why this sequence cannot be repeated, or None when it can.

        A second loop starts the sequence over from its first program, and that
        program starts at Home — so a sequence that parks the arm anywhere else
        may be run once but not twice. Without this the position locking would
        hold for a single run and quietly break on the next loop.
        """
        end = pg.chain_end(steps)
        if end is None or end == pg.HOME:
            return None  # a broken chain is caught (and named) elsewhere
        return (
            f"{label} leaves the arm at {end}, and a second loop would start "
            f"at {pg.HOME} — run it once, or end it back at {pg.HOME}"
        )

    def _preset_text(self, label: str, steps: list[str]) -> str:
        """A preset button's caption, with the time the loops add up to.

        The slot's pickup and place happen once around the whole run, so they
        are added once, outside the multiplication.
        """
        role = pg.ROLES.get(self._role) if self._role else None
        is_demo_run = label == "DEMO" and bool(role and role.demo)
        loops = DEMO_REPEAT_COUNT if is_demo_run else (
            1 if role and not role.adjust_run else self._loops()
        )
        slot_key = self._slot() if role is None or role.slot_wrap else None
        one = pg.total_duration(steps)
        wrap = 0.0
        if slot_key is not None:
            station = pg.slot(slot_key)
            wrap = pg.duration(station.pickup) + pg.duration(station.place)
        if loops is None:
            return f"{label}\nca. {pg.format_duration(one)} per loop  {LOOP_ENDLESS}"
        total = wrap + one * loops
        if loops > 1:
            return (
                f"{label}\n{loops} × {pg.format_duration(one)} + Slot = "
                f"ca. {pg.format_duration(total)}"
            )
        return f"{label}\nca. {pg.format_duration(total)}"

    def _on_run_shape_changed(self):
        """Redraw everything that quotes a total time — the repeat count or the
        slot has changed."""
        for label, btn in self.preset_buttons.items():
            btn.configure(text=self._preset_text(label, self.PRESETS[label]))
        self._refresh_queue_display()

    # ----- queue position locking --------------------------------------------

    def _queue_end(self) -> str:
        """Where the queue leaves the robot — the position the next step has to
        start from.

        An empty queue means a robot parked at Home: the queue is always
        planned from there. chain_end() walks the whole queue rather than
        reading the last item, because the last item may be an I/O or media
        step, which does not move the arm and has no end of its own.
        """
        return pg.chain_end(self.queue) or pg.HOME

    def _refresh_add_buttons(self):
        """Grey out every program that cannot follow the end of the queue."""
        position = self._queue_end()
        for key, btn in self.add_buttons.items():
            state = "normal" if pg.can_follow(key, position) else "disabled"
            # configure() redraws the whole button, even to the same state.
            if btn.cget("state") != state:
                btn.configure(state=state)

    def _bind_explain(self, widget, key: str):
        """Explain an add-button in the status line — on hover for a mouse, on
        tap for the touchscreen, where there is no such thing as hovering.

        Bound recursively: a CTkButton is a frame around a canvas and a label,
        so the events land on those children, not on the button itself. Our own
        bindings still fire while the button is disabled, which is exactly the
        case where the explanation matters.
        """
        widget.bind("<Enter>", lambda _e, k=key: self._on_add_hover(k), add="+")
        widget.bind("<Leave>", self._on_add_leave, add="+")
        widget.bind("<Button-1>", lambda _e, k=key: self._on_add_tap(k), add="+")
        for child in widget.winfo_children():
            self._bind_explain(child, key)

    def _on_add_hover(self, key: str):
        text = pg.mismatch_reason(key, self._queue_end()) or (
            f"{key}: {pg.describe(key)} · {pg.describe_duration(key)}"
        )
        if self.status_var.get() != self._hover_text:
            # Not our own text — remember it so leaving can put it back.
            self._hover_restore = self.status_var.get()
        self._hover_text = text
        self.status_var.set(text)

    def _on_add_leave(self, _event=None):
        # Only take our own explanation back off; anything a run wrote in the
        # meantime stays.
        if self._hover_text is not None and self.status_var.get() == self._hover_text:
            self.status_var.set(self._hover_restore)
        self._hover_text = None

    def _on_add_tap(self, key: str):
        # A greyed-out button swallows its own command, so without this a tap
        # on one would do nothing at all and say nothing either.
        reason = pg.mismatch_reason(key, self._queue_end())
        if reason is not None:
            self._hover_text = None
            self.status_var.set(f"Cannot queue — {reason}")

    # ----- queue -------------------------------------------------------------

    def on_queue_add(self, step: str):
        # The button is greyed out in this case, so this only catches a stray
        # programmatic call — but a refused add must never silently pass.
        reason = pg.mismatch_reason(step, self._queue_end())
        if reason is not None:
            self._set_status(f"Cannot queue — {reason}")
            return
        self.queue.append(step)
        self._refresh_queue_display()

    def on_queue_add_io(self):
        """Put an output switch in the queue."""
        IoStepDialog(self, button=self._button, on_accept=self._queue_token)

    def on_queue_add_media(self):
        """Put a picture or clip in the queue."""
        MediaStepDialog(
            self,
            files=self._media_files(),
            button=self._button,
            on_accept=self._queue_token,
        )

    def _queue_token(self, token: str):
        self.queue.append(token)
        self._refresh_queue_display()
        self.status_var.set(f"Added {pg.describe_step(token)}")

    def _media_files(self) -> list[str]:
        """The names a media step can choose from. Empty without a panel."""
        if self.media is None:
            return []
        try:
            return self.media.available_files()
        except Exception:
            return []

    def on_queue_pop(self):
        if not self.queue:
            self._set_status("Queue is empty")
            return
        removed = self.queue.pop()
        self._refresh_queue_display()
        self._set_status(f"Removed {pg.describe_step(removed)}")

    def on_queue_clear(self):
        self.queue.clear()
        self._refresh_queue_display()

    def on_queue_start(self):
        if not self.queue:
            self._set_status("Queue is empty")
            return
        if self._loops() != 1:
            refusal = self._repeat_refusal(self.queue, "the queue")
            if refusal is not None:
                self._set_status(f"Cannot repeat — {refusal}")
                return
        # The queue is run exactly as written: no slot wrap. What is in it is
        # what someone put in it.
        self._start_worker(
            list(self.queue), label="Queue", loops=self._loops()
        )

    def _refresh_queue_display(self):
        if self.queue_box is None:
            return
        self.queue_box.configure(state="normal")
        self.queue_box.delete("1.0", "end")
        if self.queue:
            for i, step in enumerate(self.queue, 1):
                if pg.is_token(step):
                    # describe() and describe_step() say the same thing for a
                    # token, so only one of them goes on the line. The duration
                    # is only worth printing for a pulse, which is the one kind
                    # that takes any real time.
                    seconds = pg.duration(step)
                    tail = f"   ({pg.describe_duration(step)})" if seconds >= 1 else ""
                    self.queue_box.insert("end", f"{i}. {pg.describe_step(step)}{tail}\n")
                    continue
                self.queue_box.insert(
                    "end",
                    f"{i}. {step}   ({pg.describe(step)} · {pg.describe_duration(step)})\n",
                )
            self.chain_var.set(
                f"Queue ends at: {self._queue_end()}   "
                f"(after {pg.describe_step(self.queue[-1])})"
            )
            loops = self._loops()
            one = pg.total_duration(self.queue)
            head = f"{len(self.queue)} steps · ca. {pg.format_duration(one)}"
            if loops is None:
                self.total_var.set(f"{head} per loop · repeating until cancelled")
            elif loops > 1:
                self.total_var.set(
                    f"{head} per loop · {loops} loops = "
                    f"ca. {pg.format_duration(one * loops)} in total"
                )
            else:
                self.total_var.set(f"{head} in total")
        else:
            self.queue_box.insert("end", "(empty)\n")
            self.chain_var.set(f"Robot at: {pg.HOME}")
            self.total_var.set("")
        self.queue_box.configure(state="disabled")
        self._refresh_add_buttons()

    # ----- presets and payload -----------------------------------------------

    def on_run_preset(self, preset: str):
        role = pg.ROLES.get(self._role) if self._role else None
        if role and role.presets is not None and preset not in role.presets:
            self._set_status(f"{preset} is not available for {self._role}")
            return
        if preset not in self.PRESETS:
            self._set_status(f"Unknown preset {preset!r}")
            return
        steps = list(self.PRESETS[preset])
        if (
            preset == "DEMO"
            and self.media is not None
            and "demo.mp4" not in {name.lower() for name in self.media.available_files()}
            and "media:demo.mp4" in steps
        ):
            steps.remove("media:demo.mp4")
        slot_key = self._slot() if role is None or role.slot_wrap else None
        run_steps = pg.slot_wrap(steps, slot_key)
        # Check the preset the way it will actually run: inside the slot wrap.
        broken = pg.check_chain(run_steps)
        if broken is not None:
            # The preset itself does not add up — running it would move the arm
            # from somewhere it cannot be.
            self._set_status(f"{preset} does not fit together — {broken}")
            return
        is_demo_run = preset == "DEMO" and bool(role and role.demo)
        loops = DEMO_REPEAT_COUNT if is_demo_run else (
            1 if role and not role.adjust_run else self._loops()
        )
        if loops != 1:
            refusal = self._repeat_refusal(steps, preset)
            if refusal is not None:
                self._set_status(f"Cannot repeat — {refusal}")
                return
        if is_demo_run:
            self._demo_pause_enabled = False
            self._demo_cancel_enabled = False
            self._demo_logo_tap_count = 0
            self._demo_logo_last_tap_at = 0.0
            self._demo_idle_tap_count = 0
            self._demo_idle_last_tap_at = 0.0
            self._demo_screensaver_armed = True
            self._last_user_activity = time.monotonic()
            self._refresh_pause_buttons()
        self._start_worker(
            list(steps),
            label=preset,
            loops=loops,
            slot=slot_key,
        )

    def on_payload_submit(self):
        self._set_status("Payload controls are disabled; no payload data is sent")

    # ----- abort -------------------------------------------------------------

    def on_abort(self):
        """Cancel the active sequence according to the logged-in role."""
        if self.login_frame is not None and not self._can("abort"):
            self._set_status("Cancel is not available for this user")
            return
        if not self._busy:
            self._set_status("Nothing running to cancel")
            return
        if self._role != "Demo":
            self._cancel_queue_immediately()
            return
        if not self._demo_cancel_enabled:
            return
        if self._abort_flag.is_set():
            self._set_status("Already cancelling — the current program finishes first")
            return
        # Set before unpausing: the other order lets a worker waking out of
        # _wait_while_paused start the next program before it sees the flag.
        self._abort_flag.set()
        self._refresh_abort_buttons()
        if self._pause_flag.is_set():
            # A paused run would otherwise never reach its own end, and the
            # abort would hang waiting for a program that is held.
            if not self._unpause():
                self._set_status("Cancel — could not resume the paused program")
                return
        self._set_status("Cancel — finishing the current Demo snippet")

    def _cancel_queue_immediately(self):
        if self._cancel_after_current.is_set():
            self._set_status("Cancel already requested — the active program remains paused")
            return
        already_paused = self._pause_flag.is_set()
        self._cancel_after_current.set()
        self._pause_flag.set()
        self.timer.pause()
        self.queue.clear()
        self._refresh_queue_display()
        self._refresh_pause_buttons()
        self._refresh_abort_buttons()
        self._set_controls_enabled(True)
        where = self._resume_description()
        if already_paused:
            self._set_status(
                f"Queue cleared — robot remains paused at {where}; press CONTINUE "
                "to finish the active program"
            )
            return
        self._set_status(f"Cancel requested — pausing at {where} and clearing the queue")
        threading.Thread(
            target=self._pause_for_queue_cancel,
            args=(where,),
            daemon=True,
        ).start()

    def _pause_for_queue_cancel(self, where: str):
        try:
            answer = rc.pause()
        except Exception as exc:
            self._set_status(
                f"Queue cleared, but robot pause failed at {where}: {exc}"
            )
            return
        if answer.startswith("Pausing program"):
            self._set_status(
                f"Queue cleared — robot paused at {where}; press CONTINUE to finish "
                "the active program"
            )
        else:
            self._set_status(
                f"Queue cleared — no active robot program was paused ({answer}); "
                "the active sequence will not start another queued step"
            )

    def _refresh_abort_buttons(self):
        aborting = self._abort_flag.is_set()
        cancelled = self._cancel_after_current.is_set()
        for btn in self.abort_buttons:
            if btn is self.demo_cancel_button:
                enabled = (
                    self._role == "Demo"
                    and self._busy
                    and self._demo_cancel_enabled
                    and not aborting
                    and not cancelled
                )
                btn.configure(state="normal" if enabled else "disabled")
                continue
            btn.configure(
                text="CANCELLING …" if aborting else "CANCELLED" if cancelled else "CANCEL",
                state="disabled" if aborting or cancelled else "normal",
            )

    def _finish_abort(self, label: str):
        """Bring the arm back to the main pose after an abort. Worker thread."""
        if self._returning:
            return
        self._returning = True
        # An aborted run cannot be continued: the arm is being driven home, so
        # the saved position no longer describes where it is.
        self._resume = None
        self.timer.halt()
        if self._stop_flag.is_set():
            return  # the window is closing; move nothing
        if self._position is None:
            self._set_status(
                f"Cancelled — the arm's position is not known after "
                f"{pg.describe_step(self._last_step or '?')}; bring it back on the pendant"
            )
            return
        try:
            key = pg.return_program(self._position)
        except KeyError as e:
            self._set_status(f"Cancelled — {e}")
            return
        if key is None:
            self._set_status(f"{label} cancelled — the arm is at {self._position}")
            return
        self._set_status(
            f"{label} cancelled after {pg.describe_step(self._last_step or '?')} — "
            f"returning to main from {self._position}"
        )
        # allow_abort=False: the abort flag is still set, and this is the one
        # thing that must run in spite of it.
        if self._run_program_step(key, "Return", allow_abort=False):
            self._set_status(f"{label} cancelled — the arm is back at {pg.HOME}")

    # ----- pause / resume ----------------------------------------------------

    def on_pause_resume(self):
        if self._pause_flag.is_set():
            self.on_resume()
        else:
            self.on_pause()

    def on_pause(self):
        """Hold the run: pause the robot's program and stop the queue advancing.

        The flag is set first and independently of the robot's answer, so that
        a pause pressed in the gap between two programs (when there is nothing
        to pause) still keeps the next one from starting.
        """
        if not self._busy and self._resume is None:
            # Idle with nothing half-finished — holding here would only leave
            # the button stuck on CONTINUE with nothing to continue.
            self._set_status("Nothing running to pause")
            return
        self._pause_flag.set()
        self.timer.pause()
        self._refresh_pause_buttons()
        where = self._resume_description()
        try:
            answer = rc.pause()
        except Exception as e:
            self._set_status(f"Pause failed: {e} — queue held at {where}")
            return
        if answer.startswith("Pausing program"):
            self._set_status(f"Paused — {where}")
        else:
            # Nothing was playing; only the queue is held.
            self._set_status(f"Paused between programs — {where}")

    def _unpause(self, play_if_paused: bool = True) -> bool:
        """Clear the hold and, if a program is genuinely paused, restart it.

        Shared by RESUME and ABORT so the awkward test lives in one place. Only
        a program paused mid-way needs play(): it picks up at the node it
        stopped at. If the pause caught the worker *between* two programs then
        nothing is playing, and play() there would re-run the program that just
        finished — which is exactly the wrong thing.
        """
        self._pause_flag.clear()
        self._refresh_pause_buttons()
        if play_if_paused:
            try:
                paused_on_robot = "PAUSED" in rc.program_state()
            except Exception as e:
                self._set_status(f"Resume failed: {e}")
                self._pause_flag.set()
                self._refresh_pause_buttons()
                return False
            if paused_on_robot:
                try:
                    answer = rc.play()
                except Exception as e:
                    self._set_status(f"Resume failed: {e}")
                    return False
                if not answer.startswith("Starting program"):
                    self._set_status(f"Could not resume — {answer}")
                    return False
        self.timer.resume()
        return True

    def on_resume(self):
        """Continue from where PAUSE left off."""
        if self._role == "Demo" and self._pause_flag.is_set():
            self._demo_pause_enabled = False
            self._demo_idle_tap_count = 0
            self._demo_idle_last_tap_at = 0.0
            self._refresh_pause_buttons()
        if self._abort_flag.is_set():
            # The run is on its way home; there is nothing to go back to.
            self._set_status("Cancelling — the arm is returning to main")
            return
        if self._busy:
            if self._unpause():
                self._set_status(f"Resumed — {self._resume_description()}")
            return

        # The run itself ended while paused (an error, or the robot refused).
        # Start a fresh worker at the saved position; that program runs from
        # its start, since a stopped program cannot be picked up mid-way. The
        # loops that were still to come are picked up with it.
        self._pause_flag.clear()
        self._refresh_pause_buttons()
        if self._resume is None:
            self._set_status("Nothing to continue")
            return
        point = self._resume
        self._set_status(f"Continuing at {self._resume_description()}")
        self._start_worker(
            point.steps,
            label=point.label,
            payload_kg=point.payload_kg,
            start_index=point.index,
            loops=point.loops,
            start_cycle=point.cycle,
            slot=point.slot,
        )

    def _resume_description(self) -> str:
        """Human-readable "where we are", e.g. "Queue loop 2/5 step 2/3: w90"."""
        point = self._resume
        if point is None:
            return "nothing running"
        where = self._loop_where(point.cycle, point.loops)
        return (
            f"{point.label}{where} step {point.index + 1}/{len(point.steps)}: "
            f"{pg.describe_step(point.steps[point.index])}"
        )

    def _refresh_pause_buttons(self):
        paused = self._pause_flag.is_set()
        for btn in self.pause_buttons:
            btn.configure(
                text="▶ CONTINUE" if paused else "PAUSE",
                fg_color=self.COLOR_RESUME if paused else self.COLOR_PAUSE,
                hover_color=self.COLOR_RESUME_HOVER if paused else self.COLOR_PAUSE_HOVER,
            )
        if self.demo_pause_button is not None:
            enabled = self._role == "Demo" and self._busy and self._demo_pause_enabled
            text = "▶ CONTINUE" if paused else "PAUSE"
            self.demo_pause_button.configure(
                text=text,
                state="normal" if enabled else "disabled",
            )
        self._refresh_abort_buttons()

    def on_demo_logo_tap(self):
        if self._role != "Demo" or not self._busy:
            return
        now = time.monotonic()
        if now - self._demo_logo_last_tap_at > DEMO_CONTROL_UNLOCK_WINDOW_S:
            self._demo_logo_tap_count = 0
        self._demo_logo_last_tap_at = now
        self._demo_logo_tap_count += 1
        if self._demo_logo_tap_count < DEMO_LOGO_CANCEL_UNLOCK_TAPS:
            return
        self._demo_logo_tap_count = 0
        self._demo_cancel_enabled = True
        self._refresh_abort_buttons()

    def on_demo_idle_tap(self):
        if self._role != "Demo" or not self._busy:
            return
        now = time.monotonic()
        if now - self._demo_idle_last_tap_at > DEMO_CONTROL_UNLOCK_WINDOW_S:
            self._demo_idle_tap_count = 0
        self._demo_idle_last_tap_at = now
        self._demo_idle_tap_count += 1
        if self._demo_idle_tap_count < DEMO_IDLE_PAUSE_UNLOCK_TAPS:
            return
        self._demo_idle_tap_count = 0
        self._demo_pause_enabled = True
        self._refresh_pause_buttons()

    def _wait_while_paused(self, allow_abort: bool = True):
        """Park the worker while the run is paused, between two steps.

        Raises AbortRequested if ABORT was pressed. During the return-to-main
        program allow_abort is False, so a pause there still works but the
        already-set abort flag does not cut the return short.
        """
        while self._pause_flag.is_set() and not self._stop_flag.is_set():
            if allow_abort and self._abort_flag.is_set():
                break
            time.sleep(0.2)
        self._check_abort(allow_abort)

    def _check_abort(self, allow_abort: bool = True):
        if allow_abort and self._abort_flag.is_set():
            raise AbortRequested()

    def _wait_between_loops(self):
        """Sit out LOOP_PAUSE_S between two loops, ABORT still taking effect."""
        deadline = time.monotonic() + LOOP_PAUSE_S
        while time.monotonic() < deadline and not self._stop_flag.is_set():
            if self._abort_flag.is_set():
                break
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        self._check_abort()

    # ----- I/O handlers ------------------------------------------------------

    def on_io_toggle(self, name: str):
        try:
            state = iow.toggle(name)
        except Exception as e:
            self._set_status(f"{name} failed: {e}")
            return
        self._refresh_io_buttons()
        self._set_status(f"{name} → {'ON' if state else 'OFF'}")

    def on_io_all_off(self):
        try:
            iow.all_off()
        except Exception as e:
            self._set_status(f"I/O off failed: {e}")
            return
        self._refresh_io_buttons()
        self._set_status("All outputs off")

    def _refresh_io_buttons(self):
        """Colour each I/O button by its output state — green on, grey off."""
        if not self.io_buttons:
            return
        for name, state in iow.states().items():
            button = self.io_buttons.get(name)
            if button is None:
                continue
            button.configure(
                text=f"{name}   {'ON' if state else 'OFF'}",
                fg_color=self.COLOR_IO_ON if state else self.COLOR_IO_OFF,
                hover_color=self.COLOR_IO_ON_HOVER if state else self.COLOR_IO_OFF_HOVER,
                text_color=self.COLOR_IO_ON_TEXT if state else self.COLOR_IO_OFF_TEXT,
            )

    def on_close(self):
        """Leave no output energised behind when the GUI is closed."""
        self._safety_stop.set()
        self._close_estop_dialog()
        # Tells any worker still alive to move nothing further, not even a
        # return program.
        self._stop_flag.set()
        if self.media is not None:
            try:
                self.media.close()
            except Exception as e:
                print(f"Media shutdown failed: {e}")
        screensaver_media = getattr(self, "screensaver_media", None)
        if screensaver_media is not None:
            try:
                screensaver_media.close()
            except Exception as e:
                print(f"Screensaver shutdown failed: {e}")
        try:
            iow.close()
        except Exception as e:
            print(f"I/O shutdown failed: {e}")
        self.destroy()

    # ----- run worker --------------------------------------------------------

    def _start_worker(
        self,
        steps: list[str],
        label: str = "Run",
        payload_kg=None,
        start_index: int = 0,
        loops: int | None = 1,
        start_cycle: int = 0,
        slot: str | None = None,
    ):
        if self._busy:
            self._set_status("Busy — a program is already running")
            return
        self._busy = True
        self._stop_flag.clear()
        self._abort_flag.clear()
        self._demo_logout_requested.clear()
        self._cancel_after_current.clear()
        self._pause_flag.clear()
        self._returning = False
        self._refresh_pause_buttons()
        self._refresh_abort_buttons()
        self._set_controls_enabled(False)

        lead_s = tail_s = 0.0
        if slot is not None:
            station = pg.slot(slot)
            lead_s = pg.duration(station.pickup)
            tail_s = pg.duration(station.place)
        self.timer.start(steps, label, start_index, loops, start_cycle, lead_s, tail_s)
        threading.Thread(
            target=self._run_programs,
            args=(steps, label, payload_kg, start_index, loops, start_cycle, slot),
            daemon=True,
        ).start()

    def _wait_until_playing(self, timeout: float = PLAY_TIMEOUT_S) -> bool:
        """Wait for the controller to actually report PLAYING after play.

        Without this the STOPPED poll can fire in the gap between play being
        accepted and the program starting, and count the run as finished
        instantly. Deliberately blind to the abort flag: a program that is
        starting must be allowed to start, so that it can then finish.
        """
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop_flag.is_set():
            if "PLAYING" in rc.program_state():
                return True
            time.sleep(0.2)
        return False

    def _run_programs(
        self,
        steps: list[str],
        label: str,
        payload_kg=None,
        start_index: int = 0,
        loops: int | None = 1,
        start_cycle: int = 0,
        slot: str | None = None,
    ):
        """The whole run: slot pickup, every loop of the body, slot place.

        The pickup and the place happen once each, outside the loop — a part is
        fetched, worked on as many times as asked, and put back.

        start_cycle/start_index skip what is already done, so a continued run
        picks up where it left off instead of from the top; a continued run
        does not repeat the pickup, because the part is already in the gripper.
        """
        try:
            reason = rc.not_ready_reason()
            if reason is not None:
                # The preflight rejected the run before any robot command was
                # sent. Discard a stale continuation from an earlier failure.
                self._resume = None
                self._set_status(f"Cannot start — {reason}")
                return

            # Start from what the chain says, not from a belief left over from
            # an earlier run.
            self._position = pg.chain_end(steps[:start_index]) or pg.HOME

            resumed = start_index > 0 or start_cycle > 0
            station = pg.slot(slot) if slot is not None else None
            if station is not None and not resumed:
                self.timer.begin_lead(f"fetching from {station.label}")
                if not self._run_program_step(station.pickup, label):
                    return
                if self._cancel_after_current.is_set():
                    self._finish_queue_cancel(label)
                    return

            cycle = start_cycle
            index_from = start_index
            while loops is None or cycle < loops:
                if not self._run_one_pass(
                    steps, label, payload_kg, index_from, cycle, loops, slot
                ):
                    if self._cancel_after_current.is_set():
                        self._finish_queue_cancel(label)
                    return
                if self._cancel_after_current.is_set():
                    self._finish_queue_cancel(label)
                    return
                cycle += 1
                index_from = 0  # only the first loop starts part-way in
                if loops is not None and cycle >= loops:
                    break
                if LOOP_PAUSE_S > 0:
                    self._set_status(
                        f"{label}: loop {cycle} done — next one in "
                        f"{pg.format_duration(LOOP_PAUSE_S)}"
                    )
                    self._wait_between_loops()
                else:
                    self._set_status(
                        f"{label}: loop {cycle} done — starting loop {cycle + 1}"
                    )
                self._check_abort()

            if station is not None:
                self.timer.begin_tail(f"putting back into {station.label}")
                if not self._run_program_step(station.place, label):
                    return
                if self._cancel_after_current.is_set():
                    self._finish_queue_cancel(label)
                    return

            # Nothing left to continue.
            self._resume = None
            self.timer.finish()
            done = "" if loops in (1, None) else f" ({loops} loops)"
            count_note = ""
            if label == "DEMO":
                try:
                    run_count = st.record_demo_run()
                except Exception as e:
                    count_note = f"; run count could not be saved: {e}"
                else:
                    counter = getattr(self, "demo_runs_var", None)
                    if counter is not None:
                        self._post(
                            lambda count=run_count, var=counter: var.set(
                                f"DEMO runs completed: {count}"
                            )
                        )
            self._set_status(f"{label} complete{done}{count_note}")
        except AbortRequested:
            self._finish_abort(label)
        except Exception as e:
            self._position = None  # we no longer know where it stopped
            self._set_status(f"Error: {e}")
        finally:
            if label == "DEMO" and self.media is not None:
                self._post(lambda: self.media.show_file(None))
            if label == "DEMO":
                self._demo_pause_enabled = False
                self._demo_cancel_enabled = False
                self._demo_logo_tap_count = 0
                self._demo_logo_last_tap_at = 0.0
                self._demo_idle_tap_count = 0
                self._demo_idle_last_tap_at = 0.0
            self._busy = False
            # Cleared only here, after any return program has run.
            self._abort_flag.clear()
            self._cancel_after_current.clear()
            self._returning = False
            # Any path out of here that is not a clean finish leaves the bar
            # standing where the run got to, instead of counting down alone.
            self.timer.halt()
            self._post(self._refresh_abort_buttons)
            self._post(self._refresh_pause_buttons)
            self._post(lambda: self._set_controls_enabled(True))

    def _finish_queue_cancel(self, label: str):
        self._resume = None
        self.timer.halt()
        location = self._position or "unknown position"
        self._set_status(
            f"{label} cancelled — queue cleared; current program finished at {location}"
        )

    def _run_one_pass(
        self,
        steps: list[str],
        label: str,
        payload_kg,
        start_index: int,
        cycle: int,
        loops: int | None,
        slot: str | None,
    ) -> bool:
        """One pass through steps. False means: stop, something went wrong.

        An abort does not come back as False — it is raised, so that no caller
        can mistake it for a failure and skip the drive home.
        """
        where = self._loop_where(cycle, loops)
        demo_snippet_completed = False

        for index in range(start_index, len(steps)):
            step = steps[index]
            # Remember where we are before anything is sent, so PAUSE and a
            # later continue always name this step and this loop.
            self._resume = ResumePoint(steps, label, payload_kg, index, cycle, loops, slot)
            self._wait_while_paused()
            if self._demo_logout_requested.is_set():
                self._resume = None
                return False
            if self._cancel_after_current.is_set():
                self._resume = None
                return False
            if self._stop_flag.is_set():
                return False
            if label == "DEMO" and self._abort_flag.is_set():
                self._turn_off_pending_demo_outputs(steps[index:])
                self._check_abort()
            # Checked here, before the program is loaded, and nowhere between
            # load and play: a loaded program must always be played, never
            # abandoned half-way through being set up.
            self._check_abort()

            self.timer.begin_step(index, cycle)
            if pg.is_io(step):
                if not self._run_io_step(step, f"{label}{where}"):
                    return False
                if self._cancel_after_current.is_set():
                    self._resume = None
                    return False
                continue
            if pg.is_media(step):
                self._run_media_step(step, f"{label}{where}")
                if self._cancel_after_current.is_set():
                    self._resume = None
                    return False
                continue
            if label == "DEMO" and demo_snippet_completed:
                self._set_status(
                    f"{label}{where}: waiting {DEMO_SNIPPET_DELAY_S:g} s before next snippet"
                )
                if self._abort_flag.wait(DEMO_SNIPPET_DELAY_S):
                    self._turn_off_pending_demo_outputs(steps[index:])
                    self._check_abort()
                if self._stop_flag.is_set():
                    return False
            if not self._run_program_step(step, f"{label}{where}"):
                return False
            if label == "DEMO":
                demo_snippet_completed = True
            if self._cancel_after_current.is_set():
                self._resume = None
                return False
        return True

    def _turn_off_pending_demo_outputs(self, steps: list[str]):
        """Run later Demo output-off tokens before honoring Cancel."""
        switched_off = set()
        for step in steps:
            if not pg.is_io(step):
                continue
            try:
                action = pg.parse_io(step)
            except ValueError:
                continue
            if action.action != "off" or action.channel in switched_off:
                continue
            switched_off.add(action.channel)
            self._run_io_step(step, "DEMO cancel cleanup")

    def _run_program_step(self, key: str, label: str, allow_abort: bool = True) -> bool:
        """Load, play and wait out one program. False means it did not run.

        This is the only place self._position moves, and only once the program
        has actually been seen to finish — a belief that ran ahead of the arm
        is what would send the wrong return program at the arm later.
        """
        self._wait_while_paused(allow_abort)
        if self._stop_flag.is_set():
            return False
        self._check_abort(allow_abort)

        self._set_status(f"{label}: loading {key} …")
        try:
            response = rc.load_program(pg.path(key))
        except Exception as e:
            self._resume = None
            self._set_status(f"{label}: could not load {key} — {e}")
            return False
        if not response.startswith("Loading program"):
            self._resume = None
            self._set_status(f"{label}: could not load {key} — {response}")
            return False
        answer = rc.play()
        if not answer.startswith("Starting program"):
            # A good load followed by a refused play means either the arm state
            # changed since the check above, or the program itself has node
            # errors — the pendant log says which.
            self._resume = None
            self._set_status(
                f"{label}: {key} did not start — {answer} "
                f"({rc.robot_mode()}, {rc.safety_status()})"
            )
            return False
        self._set_status(f"{label}: running {key} …")
        if not self._wait_until_playing():
            print(f"{key}: never saw PLAYING - assuming it already finished")

        # Deliberately blind to the abort flag: this is what "let the current
        # subprogram finish" means.
        while not self._stop_flag.is_set():
            if self._demo_logout_requested.is_set():
                self._position = None
                self._set_status("Demo logged out — paused robot program stopped")
                return False
            if "STOPPED" in rc.program_state():
                break
            time.sleep(POLL_INTERVAL)
        if self._stop_flag.is_set():
            self._position = None
            self._set_status(f"Stopped during {key}")
            return False

        self._position = pg.end(key)
        self._last_step = key
        return True

    def _run_io_step(self, step: str, label: str) -> bool:
        """Switch an output. Does not move the arm, so _position is untouched."""
        try:
            action = pg.parse_io(step)
        except ValueError as e:
            self._set_status(f"{label}: {e}")
            return False
        try:
            if action.action == "pulse":
                self._set_status(
                    f"{label}: {action.channel} on for {action.seconds:g} s …"
                )
                iow.set_output(action.channel, True)
                self._sleep_watching(action.seconds)
                iow.set_output(action.channel, False)
            else:
                iow.set_output(action.channel, action.action == "on")
                self._set_status(f"{label}: {action.channel} → {action.action.upper()}")
        except Exception as e:
            self._set_status(f"{label}: {action.channel} failed — {e}")
            return False
        # Worker thread: the buttons may only be recoloured on the UI thread.
        self._post(self._refresh_io_buttons)
        self._last_step = step
        return True

    def _run_media_step(self, step: str, label: str):
        """Put something on the panel and carry straight on.

        Never fails a run: a missing clip is a caption in the panel. Does not
        move the arm, so _position is untouched.
        """
        cue = pg.parse_media(step)
        self._last_step = step
        if self.media is None:
            return
        # show_file touches Tk, and this is the worker thread.
        self._post(lambda: self.media.show_file(cue.name))
        self._set_status(
            f"{label}: {'video off' if cue.name is None else f'showing {cue.name}'}"
        )

    def _sleep_watching(self, seconds: float):
        """Sleep, but come back early if the window is closing.

        Not abort-aware on purpose: a pulse is part of the step that is running,
        and an abort lets the running step finish.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._stop_flag.is_set():
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
