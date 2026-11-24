"""Touch panel application for a cobot depowdering cell.

This is the GUI. gui.py, gui_simple.py, gui_locking.py and gui_timing.py are
kept around as they were, but this file is the one that is meant to be started
on the panel, and it is the only one that understands slots, I/O steps, media
steps and ABORT.

All of the logic is in gui_core.py — this file is layout and nothing else.

WHAT IS HERE THAT THE OLDER GUIS DO NOT HAVE
--------------------------------------------
* Slots. The main view has a Slot 1 / Slot 2 / Shaker picker. A preset run is
  wrapped in that slot's pickup and place programs: the part is fetched, the
  preset runs (all of its loops), and the part goes back. Once around the whole
  run, not once per loop. The expert-mode queue is never wrapped — it runs
  exactly what is in it.

* CANCEL instead of STOP. In Demo, CANCEL waits for the current snippet to
  finish and then ends the run. In other modes, CANCEL pauses the active
  program and clears the queue immediately; CONTINUE lets that program finish.
  The physical e-stop is the emergency stop.

* Users. The panel starts on a login screen: User, Demo, Super User or Developer
  Mode from a dropdown, then that role's PIN. What each role may do — Expert
  Mode, the I/O view, Windows Mode, Settings — is the ROLES table in
  programs.py; the buttons a role may not use are hidden. The square "Log out"
  tile, bottom left on every view, goes back to the login screen, but not
  while a run is going or held.

* Settings (Developer Mode, the tile next to Log out on Expert Mode): the
  PINs, the robot IP and whether ESC may leave fullscreen. Saved to
  settings.json at once — see settings.py.

* Fullscreen with one way out. The window opens fullscreen. "Windows Mode", in
  expert mode and only for Developer Mode, leaves it — and with it the way to
  the rest of the machine. The same button then reads "Fullscreen Mode" and
  puts the panel back; logging out does that too. ESC does nothing unless it
  is switched on in Settings; then it toggles the same way, for every user.

* I/O and video steps in the queue. "+ I/O …" and "+ Video …" in expert mode
  put an output switch or a panel clip into the queue, alongside the programs.
  The same steps can be written into the presets in programs.py — that file
  explains how.

WHAT IT INHERITS FROM THE OTHERS
--------------------------------
* The progress bar and countdown of gui_timing.py, counted against the
  duration_s estimates in programs.py.
* The position locking of gui_locking.py: a program can only be queued when its
  start position is where the previous one leaves the arm, add-buttons that do
  not fit are greyed out, and hovering or tapping one says why. I/O and media
  steps are transparent to that chain — they do not move the arm.
* Loops (×1 … ×10, ∞) and the refusal to repeat a sequence that does not park
  the arm back at Home.
* The media panel of gui_simple.py, now driven by the sequence instead of by
  its own buttons.
* The built-in number pad (keypad.py) for PIN prompts.

Role PIN defaults are defined in programs.py. Change them before deployment.
"""

import customtkinter as ctk

import cobot_theme as theme
import gui_core as core
import io_wrapper as iow
import media_panel as mp
import programs as pg
import settings as st

# --- Appearance -------------------------------------------------------------
# Theme colors, fonts, and shared headers are defined in cobot_theme.py.
APPEARANCE_MODE = "light"
WIDGET_SCALING = 1.0  # raise to ~1.2 if everything is still too small to hit

# --- Touch layout (a 12" panel is typically 1280x800) -----------------------
# Not a taste decision: the main view (controls plus the media panel) and the
# expert view are both about this wide, and below it their bottom rows start
# going off the screen. On a smaller panel, shrink the button sizes below, or
# switch the media panel off in media_panel.py.
WINDOW_SIZE = "1280x800"
MIN_SIZE = (1270, 760)
# The window opens fullscreen; the Windows Mode button in expert mode
# (Developer Mode only, or ESC if allowed in Settings) makes it a window, and WINDOW_SIZE is the size it
# gets then. The same button puts it back into fullscreen.
FULLSCREEN = True
# The Windows Mode button reads this in fullscreen, "Fullscreen Mode" in a window.
WINDOW_MODE_TEXT = "Windows Mode"
# Status text in the bottom bar wraps at this width (px), next to the tiles.
STATUS_WRAP = 520

# The login view (dropdown and button).
LOGIN_W = 420
# The bar at the bottom of every view after the login: the square Log out tile
# (and, on Expert Mode for Developer Mode, the Settings tile) bottom left, the
# status text next to them, the way to other views on the right. TILE is the
# side of the square tiles and the height of the whole bar.
TILE = 96
NAV_W = 210  # the buttons on the right of that bar
DEMO_SCREENSAVER_VIDEO = "screensaver.mp4"


class RobotGUI(core.RobotAppBase):
    TITLE = "Cobot Control"
    WINDOW_SIZE = WINDOW_SIZE
    MIN_SIZE = MIN_SIZE
    START_FULLSCREEN = FULLSCREEN

    PRESETS = pg.DEPOWDER_PRESETS
    PROGRAM_LIST = pg.DEPOWDER_PROGRAMS

    # These are a vertical budget, not a taste decision: the main view and the
    # expert view both have to fit inside 800 px, and they are the two that are
    # full. If a preset or a program is added and the bottom row starts
    # disappearing, this is what to shrink — or drop PRESET_COLS to 2 and the
    # add grid to fewer entries.
    HALT_W, HALT_H = 370, 100  # CANCEL and PAUSE, the two biggest buttons
    HALT_FONT = 32
    BTN_W, BTN_H = 300, 76  # every other button: finger-sized, not full width
    BTN_FONT = 20
    SMALL_W, SMALL_H = 210, 68  # the I/O grid, where twelve have to fit
    PROGRESS_W = 720

    # The preset grid on the main view. Three to a row because five presets two
    # to a row is three rows, and the third one pushes the nav buttons off the
    # bottom of the panel.
    PRESET_COLS = 3
    PRESET_W, PRESET_H = 262, 70
    # The add-a-step grid in expert mode: ten programs plus I/O and Video.
    ADD_W, ADD_H = 290, 56

    # Colours of the buttons the logic recolours itself (see gui_core.py).
    COLOR_ABORT, COLOR_ABORT_HOVER = theme.ABORT, theme.ABORT_HOVER
    COLOR_ABORT_TEXT = theme.WHITE
    COLOR_PAUSE, COLOR_PAUSE_HOVER = theme.PAUSE, theme.PAUSE_HOVER
    COLOR_PAUSE_TEXT = theme.PAUSE_TEXT
    COLOR_RESUME, COLOR_RESUME_HOVER = theme.RESUME, theme.RESUME_HOVER
    COLOR_IO_ON, COLOR_IO_ON_HOVER = theme.IO_ON, theme.IO_ON_HOVER
    COLOR_IO_ON_TEXT = theme.IO_ON_TEXT
    COLOR_IO_OFF, COLOR_IO_OFF_HOVER = theme.IO_OFF, theme.IO_OFF_HOVER
    COLOR_IO_OFF_TEXT = theme.IO_OFF_TEXT

    # ----- small builders ----------------------------------------------------

    def _apply_role(self):
        super()._apply_role()
        demo_button = self.preset_buttons.get("DEMO")
        if demo_button is not None:
            is_demo = self._role == "Demo"
            demo_button.configure(
                width=420 if is_demo else self.PRESET_W,
                height=86 if is_demo else self.PRESET_H,
            )

    def _button(
        self, parent, text, command, *, width=None, height=None, size=None, role="primary", **kw
    ):
        """A touch-sized button. Fixed width on purpose: packed without fill=x
        they sit in a centred column instead of stretching across the screen.

        `role` picks the colours — "primary", "nav" or "secondary" (see
        cobot_theme.BUTTON_ROLES). Anything passed explicitly, like the
        CANCEL colours, wins over the role."""
        style = theme.button_style(role)
        style.update(kw)
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=self.BTN_W if width is None else width,
            height=self.BTN_H if height is None else height,
            font=ctk.CTkFont(size=self.BTN_FONT if size is None else size, weight="bold"),
            **style,
        )

    def _status_label(self, parent):
        """The line at the bottom of a view, under a thin rule — the template's
        footer. The text is the status the logic writes to self.status_var."""
        theme.separator(parent).pack(fill="x", padx=24, pady=(2, 0))
        return ctk.CTkLabel(
            parent,
            textvariable=self.status_var,
            text_color=theme.TEXT_GREY,
            font=ctk.CTkFont(size=16),
            wraplength=1100,
        )

    def _pack_controls(self, parent):
        """CANCEL and PAUSE side by side, the largest pair on the screen. Packed
        as one block at the top of every view so neither button ever moves."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.grid_columnconfigure(0, weight=1, uniform="halt")
        row.grid_columnconfigure(1, weight=1, uniform="halt")
        self._make_abort_button(row).grid(row=0, column=0, padx=(0, 8), sticky="ew")
        self._make_pause_button(row).grid(row=0, column=1, padx=(8, 0), sticky="ew")
        row.pack(pady=(4, 14))
        return row

    # ----- the bar at the bottom of every view -------------------------------

    def _footer(self, view, nav=(), *, settings_tile=False):
        """Log out tile bottom left, Settings tile next to it (Developer Mode,
        only where asked for), the status text, then `nav` on the right.

        Packed to the bottom of `view` first thing, before the content, so the
        content can never push it off the screen — if anything is too tall, it
        is the content that gets cut, and that shows up at once on the panel.
        `nav` is a list of (text, command, right) — right is the ROLES flag the
        button needs, or None for everyone.
        """
        bar = ctk.CTkFrame(view, fg_color="transparent")
        bar.pack(side="bottom", fill="x", padx=16, pady=(4, 12))
        theme.separator(view).pack(side="bottom", fill="x", padx=16, pady=(4, 4))

        column = 0
        ctk.CTkButton(
            bar,
            textvariable=self.logout_var,
            command=self.on_logout,
            width=TILE,
            height=TILE,
            font=ctk.CTkFont(size=15, weight="bold"),
            **theme.button_style("secondary"),
        ).grid(row=0, column=column, padx=(0, 8))
        column += 1
        if settings_tile:
            tile = self._button(
                bar, "Settings", self.show_settings, width=TILE, height=TILE, size=15,
                role="nav",
            )
            tile.grid(row=0, column=column, padx=(0, 8))
            self.role_widgets.append((tile, "settings"))
            column += 1

        ctk.CTkLabel(
            bar,
            textvariable=self.safety_status_var,
            text_color=theme.BLUE_DARK,
            font=ctk.CTkFont(size=15, weight="bold"),
            width=260,
            anchor="w",
        ).grid(row=0, column=column, sticky="w", padx=(8, 4))
        column += 1

        bar.grid_columnconfigure(column, weight=1)
        ctk.CTkLabel(
            bar,
            textvariable=self.status_var,
            text_color=theme.TEXT_GREY,
            font=ctk.CTkFont(size=16),
            anchor="w",
            justify="left",
            wraplength=STATUS_WRAP,
        ).grid(row=0, column=column, sticky="ew", padx=(8, 8))
        column += 1
        ctk.CTkLabel(
            bar,
            textvariable=self.idle_countdown_var,
            text_color=theme.MAGENTA,
            font=ctk.CTkFont(size=15, weight="bold"),
            width=148,
            anchor="e",
        ).grid(row=0, column=column, sticky="e", padx=(0, 8))
        column += 1

        for text, command, right in nav:
            btn = self._button(bar, text, command, width=NAV_W, height=TILE, role="nav")
            btn.grid(row=0, column=column, padx=(8, 0))
            if right:
                self.role_widgets.append((btn, right))
            if text == WINDOW_MODE_TEXT:
                self.window_mode_button = btn
            column += 1
        return bar

    # ----- view construction -------------------------------------------------

    def _build_views(self):
        self._build_login_frame()
        self._build_main_frame()
        self._build_expert_frame()
        self._build_io_frame()
        self._build_settings_frame()
        self._build_screensaver()

    def _build_screensaver(self):
        """A full-window media surface shown after Demo-mode inactivity."""
        self.screensaver_frame = None
        self.screensaver_media = None
        self._screensaver_return_view = None
        if not mp.MEDIA_ENABLED:
            return

        self.screensaver_frame = self._register_view(
            ctk.CTkFrame(self, fg_color=theme.BLACK)
        )
        self.screensaver_media = mp.MediaPanel(
            self.screensaver_frame,
            width=self.winfo_screenwidth(),
            height=self.winfo_screenheight(),
            chrome=False,
        )
        self.screensaver_media.pack(fill="both", expand=True)

    def _show_demo_screensaver(self) -> bool:
        if self.screensaver_frame is None or self.screensaver_media is None:
            return False
        files = self.screensaver_media.available_files()
        video = next(
            (name for name in files if name.lower() == DEMO_SCREENSAVER_VIDEO.lower()),
            None,
        )
        if video is None:
            self._set_status(
                f"Demo screensaver video not found: put {DEMO_SCREENSAVER_VIDEO} in "
                f"{mp.MEDIA_DIR}/"
            )
            return False
        self._screensaver_return_view = self.current_view() or self.main_frame
        if not self.screensaver_media.show_vlc_file(video):
            if mp.Image is None or mp.cv2 is None:
                self._set_status(
                    "Demo video unavailable: VLC could not start and OpenCV is missing"
                )
                return False
            self.screensaver_media.show_file(video)
        self.screensaver_frame.tkraise()
        self._demo_screensaver_visible = True
        return True

    def _hide_demo_screensaver(self):
        if not self._demo_screensaver_visible:
            return
        self._demo_screensaver_visible = False
        if self.screensaver_media is not None:
            self.screensaver_media.close()
        view = self._screensaver_return_view or self.main_frame
        self._screensaver_return_view = None
        if view is not None:
            view.tkraise()

    def _build_login_frame(self):
        """The first view: pick a role, then its PIN on the number pad.

        The roles, their PINs and what each may do are programs.ROLES (PINs
        changed in Settings come from settings.json). Nothing can run from here
        — there is no preset and no CANCEL on this view, which is why gui_core
        refuses a log-out while a run is going or held.
        """
        self.login_frame = self._register_view(ctk.CTkFrame(self))

        theme.header(self.login_frame, "Cobot Control").pack(fill="x", padx=24, pady=(8, 0))

        card = ctk.CTkFrame(self.login_frame, fg_color="transparent")
        card.pack(expand=True)
        theme.section_label(card, "Login").pack(anchor="w", pady=(0, 10))
        ctk.CTkLabel(
            card, text="User", text_color=theme.TEXT_GREY, font=ctk.CTkFont(size=18), anchor="w"
        ).pack(anchor="w", pady=(0, 4))

        roles = list(pg.ROLES)
        self.login_choice = ctk.StringVar(value=roles[0] if roles else "")
        ctk.CTkOptionMenu(
            card,
            values=roles,
            variable=self.login_choice,
            width=LOGIN_W,
            height=70,
            font=ctk.CTkFont(size=24, weight="bold"),
            dropdown_font=ctk.CTkFont(size=26),
            anchor="w",
        ).pack(pady=(0, 18))
        self._button(
            card,
            "Log in",
            lambda: self.on_login(self.login_choice.get()),
            width=LOGIN_W,
            height=80,
            size=24,
        ).pack()

        ctk.CTkLabel(
            self.login_frame,
            textvariable=self.safety_status_var,
            text_color=theme.BLUE_DARK,
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=24, pady=(4, 0))
        self._status_label(self.login_frame).pack(fill="x", padx=24, pady=(4, 8))

    def _build_main_frame(self):
        self.main_frame = self._register_view(ctk.CTkFrame(self))

        theme.header(
            self.main_frame,
            "Cobot Control",
            logo_height=round(theme.LOGO_HEIGHT * 1.4),
            logo_command=self.on_demo_logo_tap,
        ).pack(fill="x", padx=24, pady=(6, 0))
        self._footer(
            self.main_frame,
            nav=(("Expert Mode", self.show_expert, "expert"), ("I/O …", self.show_io, "io")),
        )

        # Controls on the left, the picture on the right. The controls keep the
        # same widths as the other views, so nothing on them moves when the
        # media panel is switched off — the picture simply is not there.
        body = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        body.pack(fill="both", expand=True)
        left = ctk.CTkFrame(body, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True)

        if mp.MEDIA_ENABLED:
            self.media = mp.MediaPanel(
                body, button=self._button, on_status=self._set_status
            )
            self.media.pack(side="right", fill="y", padx=(10, 14), pady=6)
            self._bind_demo_idle_tap(self.media.view)

        # CANCEL + pause/resume — kept first in every view so they never move.
        main_controls = self._pack_controls(left)
        self.role_pack_widgets.append(
            (main_controls, "non_demo", {"pady": (4, 14)})
        )
        self._make_progress_block(left, compact=True).pack(pady=(0, 10))

        # One button per preset, PRESET_COLS to a row; registered in run_buttons
        # so they are greyed out while a run is going.
        preset_grid = ctk.CTkFrame(left, fg_color="transparent")
        preset_grid.pack()
        for index, (label, steps) in enumerate(self.PRESETS.items()):
            btn = self._button(
                preset_grid,
                self._preset_text(label, steps),
                lambda key=label: self.on_run_preset(key),
                width=self.PRESET_W,
                height=self.PRESET_H,
                size=18,
            )
            btn.grid(
                row=index // self.PRESET_COLS,
                column=index % self.PRESET_COLS,
                padx=6,
                pady=5,
            )
            self.run_buttons.append(btn)
            self.preset_buttons[label] = btn
            self.role_widgets.append((btn, f"preset:{label}"))

        demo_controls_row = ctk.CTkFrame(left, fg_color="transparent")
        demo_cancel_button = self._make_abort_button(demo_controls_row)
        self.demo_cancel_button = demo_cancel_button
        demo_cancel_button.configure(
            width=420,
            height=78,
            font=ctk.CTkFont(size=24, weight="bold"),
        )
        demo_cancel_button.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        demo_pause_button = self._make_pause_button(demo_controls_row)
        self.demo_pause_button = demo_pause_button
        demo_pause_button.configure(
            width=420,
            height=78,
            font=ctk.CTkFont(size=24, weight="bold"),
        )
        demo_pause_button.grid(row=0, column=1, padx=(8, 0), sticky="ew")
        demo_controls_row.grid_columnconfigure((0, 1), weight=1, uniform="demo-controls")
        demo_controls_row.pack(pady=(10, 4), fill="x")
        self.role_pack_widgets.append(
            (demo_controls_row, "demo", {"pady": (10, 4), "fill": "x"})
        )

        self.demo_runs_var = ctk.StringVar(
            value=f"DEMO runs completed: {st.demo_run_count()}"
        )
        demo_runs_label = ctk.CTkLabel(
            left,
            textvariable=self.demo_runs_var,
            text_color=theme.TEXT_GREY,
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        demo_runs_label.pack(pady=(0, 4))
        self.role_pack_widgets.append((demo_runs_label, "demo", {"pady": (0, 4)}))

        # Which station the part comes from, and how often the preset repeats.
        # Both re-render the preset captions, because both change the total.
        slot_row = self._make_slot_row(left)
        slot_row.pack(pady=(10, 4))
        self.role_pack_widgets.append((slot_row, "adjust_run", {"pady": (10, 4)}))
        loop_row = self._make_loop_row(left)
        loop_row.pack(pady=(4, 4))
        self.role_pack_widgets.append((loop_row, "adjust_run", {"pady": (4, 4)}))

        # Payload is hidden for Demo and remains visible-but-disabled for other
        # roles because no payload data is sent to the robot.
        payload_row = self._make_payload_row(left)
        payload_row.pack(pady=(6, 4))
        self.role_pack_widgets.append(
            (payload_row, "non_demo", {"pady": (6, 4)})
        )

    def _bind_demo_idle_tap(self, widget):
        widget.bind("<Button-1>", lambda _event: self.on_demo_idle_tap(), add="+")
        for child in widget.winfo_children():
            self._bind_demo_idle_tap(child)

    def _build_expert_frame(self):
        """The queue, for Super User and Developer Mode: anything can be strung
        together here as long as the positions fit — plus I/O and video steps,
        which fit anywhere because they do not move the arm."""
        self.expert_frame = self._register_view(ctk.CTkFrame(self))

        theme.header(self.expert_frame, "Expert Mode").pack(fill="x", padx=24, pady=(6, 0))
        # Windows Mode: the way out of fullscreen, and therefore to the rest of
        # the machine, and the way back in — Developer Mode only. The label says
        # which of the two it does next, so it is kept and re-labelled.
        self._footer(
            self.expert_frame,
            nav=(
                (WINDOW_MODE_TEXT, self.on_window_mode, "windowed"),
                ("← Back", self.show_main, None),
            ),
            settings_tile=True,
        )

        # Same control block, same spot as the main view.
        self._pack_controls(self.expert_frame)
        self._make_progress_block(self.expert_frame, compact=True).pack(pady=(0, 6))

        # Two columns: what can be added on the left, the queue and what can be
        # done with it on the right. Side by side because this is the busiest
        # view and a single column of touch-sized buttons runs off the bottom
        # of an 800 px panel.
        body = ctk.CTkFrame(self.expert_frame, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24)
        body.grid_columnconfigure(0, weight=0)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(1, weight=1)

        theme.section_label(body, "Add a step:").grid(row=0, column=0, sticky="w", pady=(0, 4))
        # A plain grid, not a scrollable frame: dragging a scrollable frame on a
        # touchscreen is fiddly. It holds about ten programs at this size —
        # past that, this is the spot that needs rethinking.
        add_grid = ctk.CTkFrame(body, fg_color="transparent")
        add_grid.grid(row=1, column=0, sticky="nw")
        for index, label in enumerate(self.PROGRAM_LIST):
            btn = self._button(
                add_grid,
                f"+ {label}\n{pg.describe(label)} · {pg.describe_duration(label)}",
                lambda key=label: self.on_queue_add(key),
                width=self.ADD_W,
                height=self.ADD_H,
                size=14,
            )
            btn.grid(row=index // 2, column=index % 2, padx=6, pady=3)
            self.add_buttons[label] = btn
            self._bind_explain(btn, label)

        # The two step kinds that are not programs. They are never greyed out:
        # neither moves the arm, so both fit after anything.
        rows = (len(self.PROGRAM_LIST) + 1) // 2
        extra = ctk.CTkFrame(add_grid, fg_color="transparent")
        extra.grid(row=rows, column=0, columnspan=2, pady=(6, 0))
        for column, (text, command) in enumerate(
            (("+ I/O …", self.on_queue_add_io), ("+ Video …", self.on_queue_add_media))
        ):
            self._button(
                extra, text, command, width=self.ADD_W, height=self.ADD_H, size=17,
                role="nav",
            ).grid(row=0, column=column, padx=6)

        theme.section_label(body, "Queue:").grid(
            row=0, column=1, sticky="w", padx=(20, 0), pady=(0, 4)
        )
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=1, column=1, sticky="nsew", padx=(20, 0))
        self.queue_box = ctk.CTkTextbox(right, height=90, font=ctk.CTkFont(size=17))
        self.queue_box.pack(fill="both", expand=True)
        # Where the queue leaves the robot — what the next program must start
        # from — and what the whole thing is expected to cost in time.
        ctk.CTkLabel(
            right,
            textvariable=self.chain_var,
            anchor="w",
            height=22,
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(
            right, textvariable=self.total_var, anchor="w", height=22,
            font=ctk.CTkFont(size=16),
        ).pack(fill="x")

        # What to do with the queue, under the queue itself — the same column of
        # the screen as the thing it acts on.
        ctrl_row = ctk.CTkFrame(right, fg_color="transparent")
        ctrl_row.pack(pady=(6, 0))
        self.start_queue_button = self._button(
            ctrl_row, "Start Queue", self.on_queue_start, width=240
        )
        self.start_queue_button.grid(row=0, column=0, padx=4)
        self.run_buttons.append(self.start_queue_button)
        # With the queue locked, Clear is too blunt for fixing a mis-click.
        self._button(
            ctrl_row, "Remove last", self.on_queue_pop, width=180, role="secondary"
        ).grid(row=0, column=1, padx=4)
        self._button(
            ctrl_row, "Clear", self.on_queue_clear, width=130, role="secondary"
        ).grid(row=0, column=2, padx=4)
        self._make_loop_row(right).pack(pady=(8, 0))
        # The payload, narrow enough for this column (shared with the main view).
        self._make_payload_row(right, compact=True).pack(pady=(8, 0))

    def _build_io_frame(self):
        """Manual switching of the digital outputs wired to the Pi's GPIO.

        One toggle button per channel (IO1 … IO12, see io_wrapper.IO_PINS); the
        button is green while its output is on. Three columns — at this button
        size twelve full-width rows would not come close to fitting.

        This is for switching something by hand. To switch an output as part of
        a sequence, use "+ I/O …" in expert mode, or an "io:…" step in a preset.
        """
        self.io_frame = self._register_view(ctk.CTkFrame(self))

        theme.header(self.io_frame, "I/O Outputs").pack(fill="x", padx=24, pady=(6, 0))
        self._footer(
            self.io_frame,
            nav=(("All OFF", self.on_io_all_off, None), ("← Back", self.show_main, None)),
        )

        # Same control block, same spot as the other views.
        self._pack_controls(self.io_frame)

        self._make_io_grid(self.io_frame, columns=3).pack(pady=8)

        # Warns when the outputs are only simulated (no GPIO on this machine).
        ctk.CTkLabel(
            self.io_frame,
            textvariable=self.io_backend_var,
            anchor="w",
            wraplength=1100,
            justify="left",
            text_color=theme.TEXT_GREY,
            font=ctk.CTkFont(size=13),
        ).pack(fill="x", padx=24, pady=(6, 0))

    def _build_settings_frame(self):
        """Developer Mode only: PINs, robot IP and the ESC key.

        Every change is saved to settings.json at once (settings.py) — there is
        no Save button to forget. Reached from the Settings tile on Expert Mode.
        """
        self.settings_frame = self._register_view(ctk.CTkFrame(self))

        theme.header(self.settings_frame, "Settings").pack(fill="x", padx=24, pady=(6, 0))
        self._footer(self.settings_frame, nav=(("← Back", self.settings_back, None),))

        # CANCEL and PAUSE stay reachable here too, in the same spot.
        self._pack_controls(self.settings_frame)

        body = ctk.CTkFrame(self.settings_frame, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=40)
        body.grid_columnconfigure(0, weight=1, uniform="col")
        body.grid_columnconfigure(1, weight=1, uniform="col")

        # Left: one row per user — name, the PIN as dots, Change.
        pins = ctk.CTkFrame(body, fg_color="transparent")
        pins.grid(row=0, column=0, sticky="nw")
        theme.section_label(pins, "PINs").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        for index, label in enumerate(pg.ROLES, start=1):
            var = ctk.StringVar(value="")
            self.pin_vars[label] = var
            ctk.CTkLabel(pins, text=label, font=ctk.CTkFont(size=20), anchor="w", width=180).grid(
                row=index, column=0, sticky="w", pady=6
            )
            ctk.CTkLabel(
                pins, textvariable=var, font=ctk.CTkFont(size=22, weight="bold"),
                text_color=theme.BLUE, anchor="w", width=110,
            ).grid(row=index, column=1, sticky="w")
            self._button(
                pins, "Change", lambda key=label: self.on_change_pin(key),
                width=150, height=64, size=18,
            ).grid(row=index, column=2, padx=(8, 0), pady=6)

        # Right: network and panel.
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nw", padx=(30, 0))
        theme.section_label(right, "Network").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ctk.CTkLabel(right, text="Robot IP (UR30)", font=ctk.CTkFont(size=20), anchor="w").grid(
            row=1, column=0, sticky="w"
        )
        ip_row = ctk.CTkFrame(right, fg_color="transparent")
        ip_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 4))
        ctk.CTkLabel(
            ip_row, textvariable=self.robot_ip_var, font=ctk.CTkFont(size=22, weight="bold"),
            text_color=theme.BLUE, anchor="w", width=220,
        ).grid(row=0, column=0, sticky="w")
        self._button(
            ip_row, "Change", self.on_change_robot_ip, width=150, height=64, size=18
        ).grid(row=0, column=1, padx=(8, 0))
        self.pi_ip_var = ctk.StringVar(value="")
        ctk.CTkLabel(
            right, textvariable=self.pi_ip_var, font=ctk.CTkFont(size=15),
            text_color=theme.TEXT_GREY, anchor="w", justify="left", wraplength=480,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 14))

        theme.section_label(right, "Panel").grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ctk.CTkCheckBox(
            right,
            text="ESC key switches to Windows Mode",
            variable=self.esc_var,
            command=self.on_esc_toggle,
            font=ctk.CTkFont(size=19),
            checkbox_width=40,
            checkbox_height=40,
        ).grid(row=5, column=0, columnspan=2, sticky="w")
        ctk.CTkLabel(
            right,
            text="Off: the panel stays locked in fullscreen, as before.",
            font=ctk.CTkFont(size=15), text_color=theme.TEXT_GREY, anchor="w",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 14))
        self._button(
            right, "Reset to defaults", self.on_settings_reset, width=240, height=60,
            size=17, role="secondary",
        ).grid(row=7, column=0, columnspan=2, sticky="w")

    def _refresh_settings(self):
        super()._refresh_settings()
        self.pi_ip_var.set(
            f"This Pi: {self.pi_addresses()} — its own address is set with "
            "sudo ./setup_ur_link.sh"
        )


if __name__ == "__main__":
    st.load()  # PINs, robot IP and the ESC setting from settings.json, if there
    theme.install(APPEARANCE_MODE)
    ctk.set_widget_scaling(WIDGET_SCALING)
    # Claim the GPIO pins now rather than on the first button press, so a pin
    # held by something else shows up in the I/O view's backend line instead of
    # surprising someone mid-run.
    iow.setup()
    app = RobotGUI()
    app.mainloop()
