"""The on-screen number pad, used for the payload field and for PIN prompts.

Two forms, one call. Keypad(master, ...) on the main window gives the overlay:
a card laid over the window, built once and only shown and hidden after that —
no new window, so nothing is built, placed or animated by the window manager
while someone waits to type. Opened from a separate dialog window (the I/O step
dialog), it is that window's own pop-up, as before, because an overlay in the
main window would sit underneath the dialog.

prepare(app) builds the overlay ahead of time, so even the first PIN prompt
opens at once. gui_timing.py still carries its own copy of the pop-up.

It writes into its own buffer and hands the finished string to on_accept, so the
caller decides what to do with it — a payload in grams, or a PIN to check. A real
keyboard (or a system on-screen one, while this window has focus) types into it
as well.
"""

import customtkinter as ctk

import cobot_theme as theme


class KeypadWindow(ctk.CTkToplevel):
    """The pop-up form of the pad, for dialogs that are windows of their own.

    mask=True shows dots instead of digits, for a PIN; allow_dot=False leaves the
    decimal point off the pad, for anything that is a whole number; ip=True
    allows up to three dots, for an IPv4 address.
    """

    ROWS = (("7", "8", "9"), ("4", "5", "6"), ("1", "2", "3"), (".", "0", "←"))

    def __init__(
        self,
        master,
        *,
        title: str,
        initial: str = "",
        mask: bool = False,
        allow_dot: bool = True,
        ip: bool = False,
        on_accept=None,
    ):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self._mask = mask
        self._allow_dot = allow_dot or ip
        self._ip = ip
        self._on_accept = on_accept
        self._value = initial
        self.display_var = ctk.StringVar()

        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=22, weight="bold")).pack(
            padx=24, pady=(20, 8)
        )
        ctk.CTkLabel(
            self,
            textvariable=self.display_var,
            font=ctk.CTkFont(size=34, weight="bold"),
            height=56,
            width=320,
            fg_color=theme.PANEL_DISPLAY,
            corner_radius=8,
        ).pack(padx=24, pady=(0, 12))

        grid = ctk.CTkFrame(self, fg_color="transparent")
        grid.pack(padx=24)
        for r, row in enumerate(self.ROWS):
            for c, key in enumerate(row):
                if key == "." and not self._allow_dot:
                    continue
                ctk.CTkButton(
                    grid,
                    text=key,
                    width=100,
                    height=72,
                    font=ctk.CTkFont(size=26, weight="bold"),
                    command=lambda k=key: self._press(k),
                ).grid(row=r, column=c, padx=5, pady=5)

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(padx=24, pady=(14, 20))
        ctk.CTkButton(
            actions,
            text="Clear",
            width=100,
            height=64,
            font=ctk.CTkFont(size=18, weight="bold"),
            command=self._clear,
        ).grid(row=0, column=0, padx=5)
        ctk.CTkButton(
            actions,
            text="Cancel",
            width=100,
            height=64,
            font=ctk.CTkFont(size=18, weight="bold"),
            **theme.SECONDARY,
            command=self.destroy,
        ).grid(row=0, column=1, padx=5)
        ctk.CTkButton(
            actions,
            text="OK",
            width=100,
            height=64,
            font=ctk.CTkFont(size=18, weight="bold"),
            command=self._accept,
        ).grid(row=0, column=2, padx=5)

        self._refresh()
        self.bind("<Key>", self._on_key)
        self.transient(master)
        self.attributes("-topmost", True)
        # CustomTkinter builds a Toplevel over two event loop turns: right now
        # it does not know its own size yet, so it is centred once for the first
        # frame and again once the real geometry is in.
        self._centre_on(master)
        self.after(60, lambda: self._centre_on(master))
        # Grabbing before it is on screen raises, hence the retry.
        self.after(120, self._grab)

    # ----- input -------------------------------------------------------------

    def _press(self, key: str):
        if key == "←":
            self._value = self._value[:-1]
        elif key == "." and self._ip:
            if self._value and not self._value.endswith(".") and self._value.count(".") < 3:
                self._value += "."
        elif key == ".":
            if self._allow_dot and "." not in self._value:
                self._value += "." if self._value else "0."
        else:
            self._value += key
        self._refresh()

    def _on_key(self, event):
        if event.keysym in ("Return", "KP_Enter"):
            self._accept()
        elif event.keysym == "Escape":
            self.destroy()
        elif event.keysym == "BackSpace":
            self._press("←")
        elif event.char and event.char in "0123456789.":
            self._press(event.char)

    def _clear(self):
        self._value = ""
        self._refresh()

    def _refresh(self):
        shown = "•" * len(self._value) if self._mask else self._value
        self.display_var.set(shown or "–")

    def _accept(self):
        value = self._value
        self.destroy()
        if self._on_accept is not None:
            self._on_accept(value)

    # ----- window ------------------------------------------------------------

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



# ----- the overlay ------------------------------------------------------------


class KeypadOverlay(ctk.CTkFrame):
    """The pad as a card inside the main window. Built once, then reused.

    While it is up it holds a local grab, so a tap anywhere else on the panel
    does nothing — as modal as the pop-up was, without being a window.
    """

    ROWS = KeypadWindow.ROWS

    def __init__(self, master):
        super().__init__(
            master, fg_color=theme.WHITE, border_width=3, border_color=theme.BLUE, corner_radius=8
        )
        self._on_accept = None
        self._mask = False
        self._allow_dot = True
        self._ip = False
        self._value = ""
        self.title_var = ctk.StringVar(value="")
        self.display_var = ctk.StringVar(value="–")

        ctk.CTkLabel(
            self, textvariable=self.title_var, text_color=theme.BLUE,
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(padx=24, pady=(18, 8))
        ctk.CTkLabel(
            self,
            textvariable=self.display_var,
            font=ctk.CTkFont(size=34, weight="bold"),
            height=56,
            width=320,
            fg_color=theme.PANEL_DISPLAY,
            corner_radius=8,
        ).pack(padx=24, pady=(0, 12))

        grid = ctk.CTkFrame(self, fg_color="transparent")
        grid.pack(padx=24)
        self._dot_button = None
        for r, row in enumerate(self.ROWS):
            for c, key in enumerate(row):
                btn = ctk.CTkButton(
                    grid, text=key, width=100, height=72,
                    font=ctk.CTkFont(size=26, weight="bold"),
                    command=lambda k=key: self._press(k),
                )
                btn.grid(row=r, column=c, padx=5, pady=5)
                if key == ".":
                    self._dot_button = btn

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(padx=24, pady=(14, 18))
        for column, (text, command, style) in enumerate(
            (
                ("Clear", self._clear, {}),
                ("Cancel", self.close, theme.SECONDARY),
                ("OK", self._accept, {}),
            )
        ):
            ctk.CTkButton(
                actions, text=text, width=100, height=64,
                font=ctk.CTkFont(size=18, weight="bold"), command=command, **style,
            ).grid(row=0, column=column, padx=5)

        self.bind("<Key>", self._on_key)

    def open(self, *, title, initial="", mask=False, allow_dot=True, ip=False, on_accept=None):
        self._on_accept = on_accept
        self._mask = mask
        self._ip = ip
        self._allow_dot = allow_dot or ip
        self._value = initial
        self.title_var.set(title)
        # The "." key is hidden, not destroyed, for a PIN — its spot stays empty
        # as before, and the next prompt that wants it just shows it again.
        if self._dot_button is not None:
            if self._allow_dot:
                self._dot_button.grid()
            else:
                self._dot_button.grid_remove()
        self._refresh()
        self.place(relx=0.5, rely=0.5, anchor="center")
        self.lift()
        try:
            self.grab_set()
        except Exception:
            pass  # another grab is active; the pad still works, it is just not modal
        self.focus_set()
        return self

    def close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.place_forget()

    def is_open(self) -> bool:
        return bool(self.winfo_ismapped())

    # Same input handling as the pop-up.
    _press = KeypadWindow._press
    _clear = KeypadWindow._clear
    _refresh = KeypadWindow._refresh

    def _on_key(self, event):
        if event.keysym in ("Return", "KP_Enter"):
            self._accept()
        elif event.keysym == "Escape":
            self.close()
        elif event.keysym == "BackSpace":
            self._press("←")
        elif event.char and event.char in "0123456789.":
            self._press(event.char)

    def _accept(self):
        value = self._value
        callback = self._on_accept
        self.close()  # first, so whatever the callback shows next is on top
        if callback is not None:
            callback(value)


def prepare(app) -> KeypadOverlay:
    """Build the overlay for `app` now (hidden), instead of on the first prompt."""
    overlay = getattr(app, "_keypad_overlay", None)
    if overlay is None:
        overlay = KeypadOverlay(app)
        app._keypad_overlay = overlay
    return overlay


def Keypad(master, **options):
    """Open the number pad. See the module docstring for which form you get.

    Options: title, initial, mask, allow_dot, ip, on_accept — as before.
    """
    if isinstance(master, ctk.CTk):
        return prepare(master).open(**options)
    return KeypadWindow(master, **options)


# Append this to the very bottom of your script file to run it on desktop

if __name__ == "__main__":
    # 1. Initialize the main desktop application window
    root = ctk.CTk()
    root.title("Desktop HMI Simulator")
    root.geometry("800x480")  # Simulating a typical 7-inch RPi HMI screen

    # 2. Callback function to see what the user typed
    def handle_input(value):
        print(f"User entered value: {value}")

    # 3. Create a test button to trigger the keypad overlay
    def launch_keypad():
        Keypad(root, title="Enter Pin (g)", initial="", on_accept=handle_input)

    btn = ctk.CTkButton(root, text="Open Number Pad", command=launch_keypad, width=200, height=50)
    btn.place(relx=0.5, rely=0.5, anchor="center")

    # 4. Fire up the desktop event loop
    root.mainloop()
