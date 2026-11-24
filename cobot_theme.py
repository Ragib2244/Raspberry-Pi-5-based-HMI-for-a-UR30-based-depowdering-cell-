"""Neutral visual theme and shared header elements for the Cobot Control HMI."""

from __future__ import annotations

import customtkinter as ctk

WHITE = "#FFFFFF"
BLACK = "#111827"
GREEN = "#187C66"
BLUE = "#155E75"
GREY_BLUE = "#A9BBC4"
TEAL = "#087F8C"
CYAN = "#35A9B7"
LIME = "#A3C96A"
ORANGE = "#E58A24"
YELLOW = "#F2B84B"
MAGENTA = "#B42332"
BERRY = "#7F1D2D"
BLUE_DARK = "#173B4D"
BLUE_80 = "#3A788A"
BLUE_60 = "#719BA8"
BLUE_40 = "#A5C0C9"
BLUE_20 = "#D2E0E5"
BLUE_10 = "#EAF0F2"
TEXT_GREY = "#4B5563"

ABORT, ABORT_HOVER = MAGENTA, BERRY
PAUSE, PAUSE_HOVER, PAUSE_TEXT = ORANGE, YELLOW, BLACK
RESUME, RESUME_HOVER = GREEN, LIME
IO_ON, IO_ON_HOVER, IO_ON_TEXT = GREEN, TEAL, WHITE
IO_OFF, IO_OFF_HOVER, IO_OFF_TEXT = BLUE_20, BLUE_40, BLUE_DARK

SECONDARY = {
    "fg_color": (GREY_BLUE, "gray30"),
    "hover_color": (BLUE_40, "gray40"),
    "text_color": (BLUE_DARK, "gray90"),
}
PANEL = (BLUE_10, "gray20")
PANEL_DISPLAY = (BLUE_10, "gray25")

BUTTON_ROLES = {
    "primary": {"fg_color": BLUE, "hover_color": BLUE_DARK},
    "nav": {"fg_color": TEAL, "hover_color": BLUE},
    "secondary": dict(SECONDARY),
}

FONT_CANDIDATES = ("Arial", "Liberation Sans", "Open Sans", "Noto Sans")
LOGO_HEIGHT = 64


def button_style(role: str = "primary") -> dict:
    return dict(BUTTON_ROLES[role])


def _installed_font() -> str | None:
    try:
        import tkinter
        from tkinter import font as tkfont

        probe = tkinter.Tk()
        probe.withdraw()
        try:
            families = {name.lower() for name in tkfont.families(probe)}
        finally:
            probe.destroy()
    except Exception:
        return None
    return next((name for name in FONT_CANDIDATES if name.lower() in families), None)


def install(appearance: str = "light") -> str | None:
    ctk.set_appearance_mode(appearance)
    ctk.set_default_color_theme("blue")
    theme = ctk.ThemeManager.theme

    def patch(widget: str, **values):
        theme.setdefault(widget, {}).update(values)

    patch("CTk", fg_color=WHITE)
    patch("CTkToplevel", fg_color=WHITE)
    patch("CTkFrame", fg_color=WHITE, top_fg_color=BLUE_10, border_color=GREY_BLUE)
    patch("CTkLabel", text_color=BLACK)
    patch(
        "CTkButton",
        corner_radius=4,
        border_width=0,
        fg_color=BLUE,
        hover_color=BLUE_DARK,
        border_color=GREEN,
        text_color=WHITE,
        text_color_disabled=BLUE_40,
    )
    patch(
        "CTkSegmentedButton",
        corner_radius=4,
        border_width=2,
        fg_color=BLUE_20,
        selected_color=CYAN,
        selected_hover_color=TEAL,
        unselected_color=BLUE_20,
        unselected_hover_color=BLUE_40,
        text_color=BLUE_DARK,
        text_color_disabled=BLUE_60,
    )
    patch(
        "CTkOptionMenu",
        corner_radius=4,
        fg_color=BLUE,
        button_color=BLUE_DARK,
        button_hover_color=TEAL,
        text_color=WHITE,
        text_color_disabled=BLUE_40,
    )
    patch("DropdownMenu", corner_radius=4, fg_color=WHITE, hover_color=BLUE_20,
          text_color=BLUE_DARK)
    patch(
        "CTkCheckBox",
        corner_radius=4,
        border_width=3,
        fg_color=GREEN,
        hover_color=TEAL,
        border_color=BLUE,
        checkmark_color=WHITE,
        text_color=BLACK,
    )
    patch("CTkProgressBar", corner_radius=2, border_width=0, fg_color=BLUE_20,
          progress_color=GREEN, border_color=GREY_BLUE)
    patch(
        "CTkEntry",
        corner_radius=4,
        border_width=2,
        fg_color=WHITE,
        border_color=GREY_BLUE,
        text_color=BLACK,
        placeholder_text_color=BLUE_60,
    )
    patch("CTkTextbox", corner_radius=4, border_width=2, fg_color=BLUE_10,
          border_color=GREY_BLUE, text_color=BLACK, scrollbar_button_color=GREY_BLUE,
          scrollbar_button_hover_color=BLUE_60)

    family = _installed_font()
    if family is not None:
        theme["CTkFont"]["family"] = family
    return family


def header(
    parent,
    title: str,
    subtitle: str | None = None,
    logo_height: int = LOGO_HEIGHT,
    logo_command=None,
):
    bar = ctk.CTkFrame(parent, fg_color="transparent")
    text = ctk.CTkFrame(bar, fg_color="transparent")
    text.pack(side="left", anchor="w")
    ctk.CTkLabel(
        text,
        text=title,
        text_color=BLUE,
        font=ctk.CTkFont(size=30, weight="bold"),
        anchor="w",
    ).pack(anchor="w")
    if subtitle:
        ctk.CTkLabel(
            text,
            text=subtitle,
            text_color=GREEN,
            font=ctk.CTkFont(size=18),
            anchor="w",
        ).pack(anchor="w")
    ctk.CTkFrame(text, width=44, height=4, corner_radius=0, fg_color=GREEN).pack(
        anchor="w", pady=(3, 0)
    )

    mark = ctk.CTkFrame(bar, fg_color="transparent")
    badge = ctk.CTkLabel(
        mark,
        text="C",
        width=logo_height,
        height=logo_height,
        corner_radius=logo_height // 2,
        fg_color=BLUE,
        text_color=WHITE,
        font=ctk.CTkFont(size=max(20, logo_height // 2), weight="bold"),
    )
    badge.pack(side="left", padx=(0, 8))
    ctk.CTkLabel(
        mark,
        text="COBOT\nCONTROL",
        text_color=BLUE_DARK,
        justify="left",
        font=ctk.CTkFont(size=14, weight="bold"),
    ).pack(side="left")
    if logo_command is not None:
        def bind_click(widget):
            widget.bind("<Button-1>", lambda _event: logo_command(), add="+")
            for child in widget.winfo_children():
                bind_click(child)

        bind_click(mark)
    mark.pack(side="right", anchor="e")
    return bar


def separator(parent):
    return ctk.CTkFrame(parent, height=2, corner_radius=0, fg_color=BLUE_20)


def section_label(parent, text: str, **kw):
    return ctk.CTkLabel(
        parent,
        text=text,
        text_color=GREEN,
        font=ctk.CTkFont(size=18, weight="bold"),
        anchor="w",
        **kw,
    )
