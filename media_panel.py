"""The media panel: a picture or a silent video beside the robot controls.

Shared by main.py, gui_simple.py and gui_locking.py — the panel itself,
where it gets its files, and the two optional libraries it leans on all live
here, so there is one copy to fix and one place to point a camera at later.

The panel has no buttons. What it shows is decided by the running sequence: a
"media:<filename>" step in a preset or the queue calls show_file(), and
"media:off" puts MEDIA_IDLE back up (see programs.py). Until a step says
otherwise the idle picture is what is on screen. Video autoplays, loops by
itself and has no sound.

Stills are drawn by Pillow and regular media clips are decoded frame by frame by
OpenCV on a worker thread; the UI thread only picks up the newest frame every
MEDIA_TICK_MS and draws it. The Demo screensaver can use VLC's native decoder
instead when running under X11.

Everything here is optional by design: a missing library, a missing folder or a
file OpenCV cannot open all end up as a line of text in the panel, and the robot
side of the GUI does not notice.

    pip3 install pillow opencv-python-headless
"""

import shutil
import subprocess
import threading
import time
from pathlib import Path

import customtkinter as ctk

import cobot_theme as theme

# Both are optional: without Pillow the panel says so and everything else runs,
# without OpenCV only video does.
try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:  # pragma: no cover - depends on what is on the Pi
    Image = None
    ImageOps = None
    ImageTk = None
try:
    import cv2
except ImportError:  # pragma: no cover - depends on what is on the Pi
    cv2 = None

# --- what the panel shows ----------------------------------------------------
# The panel has no buttons: what it shows is chosen by the running sequence, via
# a "media:<filename>" step in a preset or the queue (see programs.py), which
# calls show_file(). "media:off" brings it back to MEDIA_IDLE.
#
# MEDIA_FILE wins if it is set; otherwise every supported file in MEDIA_DIR can
# be named by a step. All paths are relative to this folder. MEDIA_ENABLED =
# False leaves the panel out of every GUI that asks for it, and their layouts
# close up as if it were never there.
MEDIA_ENABLED = True
MEDIA_FILE = ""  # e.g. "media/demo.mp4" — empty means: use the whole folder
MEDIA_DIR = "media"
# What is on the panel when nothing else has been selected: at startup, and
# after a "media:off" step. A picture is the sensible choice. If it is not
# there, the first file in MEDIA_DIR is used instead, and if the folder is empty
# the panel says so — a missing idle file is never an error.
MEDIA_IDLE = "idle.png"
MEDIA_W, MEDIA_H = 400, 300  # the picture area; the panel is a little wider
MEDIA_MAX_FPS = 12  # decoding and drawing each frame is expensive on a Pi
MEDIA_TICK_MS = 66  # how often the panel picks up the newest frame

# The panel's own buttons, when the GUI does not hand its own factory in.
def _plain_button(parent, text, command, *, width, height, size, **kw):
    return ctk.CTkButton(
        parent,
        text=text,
        command=command,
        width=width,
        height=height,
        font=ctk.CTkFont(size=size, weight="bold"),
        **kw,
    )


class MediaPanel(ctk.CTkFrame):
    """The panel itself — see this module's docstring.

    on_status is the GUI's status line, used for the few things worth saying
    outside the panel. button is the GUI's own button factory; the panel has no
    buttons of its own any more, but the argument is kept so the GUIs that pass
    one keep working unchanged.
    """

    IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
    VIDEO_EXTS = (".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm")

    def __init__(
        self, master, *, button=None, on_status=None, width=MEDIA_W, height=MEDIA_H,
        chrome=True,
    ):
        super().__init__(master)
        self._width = width
        self._height = height
        self._chrome = chrome
        self._button = button or _plain_button
        self._on_status = on_status or (lambda _text: None)

        self._files = self._discover()
        # What is on screen now. Chosen by show_file(), which the run worker
        # calls for a "media:…" step; None means there is nothing to show.
        self._path = self._idle_path()
        self._is_video = False
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._vlc_process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._latest = None  # newest decoded frame, as a PIL image
        self._shown = None  # what is on the label now — keeps it from being freed
        self._video_photo = None  # reused Tk image avoids per-frame image churn

        self.caption_var = ctk.StringVar(value="")

        if chrome:
            theme.section_label(self, "Ansicht").pack(pady=(10, 4))
        self.view = ctk.CTkLabel(
            self,
            text="",
            width=width,
            height=height,
            fg_color=theme.PANEL if chrome else theme.BLACK,
            corner_radius=8 if chrome else 0,
            font=ctk.CTkFont(size=14),
            wraplength=width - 30,
        )
        if chrome:
            self.view.pack(padx=12)
            ctk.CTkLabel(
                self, textvariable=self.caption_var, font=ctk.CTkFont(size=14), height=22
            ).pack(pady=(6, 0))

            # No Play/Next buttons: the running sequence decides what is shown, and
            # a video simply plays and loops until a step replaces it.
            ctk.CTkFrame(self, fg_color="transparent", height=8).pack()
        else:
            self.configure(fg_color=theme.BLACK)
            self.view.pack(fill="both", expand=True)

        self._show_current()
        self._refresh()

    # ----- files -------------------------------------------------------------

    def _discover(self) -> list[Path]:
        """MEDIA_FILE if it is set, otherwise everything playable in MEDIA_DIR."""
        here = Path(__file__).resolve().parent
        if MEDIA_FILE:
            chosen = Path(MEDIA_FILE)
            if not chosen.is_absolute():
                chosen = here / chosen
            return [chosen] if chosen.is_file() else []
        folder = Path(MEDIA_DIR)
        if not folder.is_absolute():
            folder = here / folder
        if not folder.is_dir():
            return []
        known = self.IMAGE_EXTS + self.VIDEO_EXTS
        return sorted(
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in known
        )

    def _folder(self) -> Path:
        """MEDIA_DIR as an absolute path, whether or not it exists."""
        here = Path(__file__).resolve().parent
        folder = Path(MEDIA_DIR)
        return folder if folder.is_absolute() else here / folder

    def _idle_path(self) -> Path | None:
        """What to show when nothing has been selected.

        MEDIA_IDLE if it is there, otherwise the first file in the folder,
        otherwise nothing — a missing idle file is never an error.
        """
        if MEDIA_IDLE:
            idle = self._folder() / MEDIA_IDLE
            if idle.is_file():
                return idle
        return self._files[0] if self._files else None

    def _resolve(self, name: str) -> Path | None:
        """The file a "media:<name>" step means, or None if it is not there.

        Matched against the discovered files first (case-insensitively, so a
        step does not have to get the capitalisation of a filename right), then
        tried directly under MEDIA_DIR for a file added since startup.
        """
        wanted = name.strip().replace("\\", "/").lstrip("/")
        for path in self._files:
            if path.name.lower() == wanted.lower():
                return path
        candidate = self._folder() / wanted
        return candidate if candidate.is_file() else None

    def _current(self) -> Path | None:
        return self._path

    # ----- what is on screen -------------------------------------------------

    def _message(self, text: str):
        """Put a line of text in the picture area instead of a picture."""
        self._clear_image()
        self.view.configure(
            text=text,
            text_color=theme.WHITE if not self._chrome else theme.BLACK,
        )
        self.caption_var.set("")

    def _clear_image(self):
        if self.view.winfo_exists():
            self.view._label.configure(image="")
            self.view.configure(image=None)
        self._shown = None
        self._video_photo = None

    def _fit(self, image):
        """The image scaled to fit the panel, aspect ratio kept."""
        if not self._chrome:
            return ImageOps.fit(image, (self._width, self._height))
        return ImageOps.contain(image, (self._width, self._height))

    def _fit_video_frame(self, frame):
        """Crop and resize in OpenCV before converting a video frame to PIL."""
        height, width = frame.shape[:2]
        target_width = self._width
        target_height = self._height
        scale = (
            min(target_width / width, target_height / height)
            if self._chrome
            else max(target_width / width, target_height / height)
        )
        resized_width = max(1, round(width * scale))
        resized_height = max(1, round(height * scale))
        interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        frame = cv2.resize(
            frame, (resized_width, resized_height), interpolation=interpolation
        )
        if not self._chrome:
            left = (resized_width - target_width) // 2
            top = (resized_height - target_height) // 2
            frame = frame[top : top + target_height, left : left + target_width]
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

    def _draw_image(self, image):
        """Put a PIL image on the label. UI thread only — Tk is not thread-safe."""
        photo = ctk.CTkImage(light_image=image, size=image.size)
        self._shown = photo  # a CTkImage that nothing references is collected
        self._video_photo = None
        self.view.configure(image=photo, text="")

    def _draw_video_image(self, image):
        """Update a single Tk image instead of allocating one for every frame."""
        if self._video_photo is None:
            self._video_photo = ImageTk.PhotoImage(image, master=self.view._label)
            self._shown = self._video_photo
            self.view._label.configure(image=self._video_photo, text="")
        else:
            self._video_photo.paste(image)

    def _show_current(self):
        """Load whatever is selected now — a still straight away, a video as a
        decoding thread."""
        self._stop_video()
        path = self._current()

        if path is None:
            where = MEDIA_FILE or f"{MEDIA_DIR}/"
            self._message(
                f"No media found.\nPut {MEDIA_IDLE} (or any image or mp4) in {where}"
            )
            return
        if Image is None:
            self._message("Pillow is not installed —\npip3 install pillow")
            return

        self.caption_var.set("")
        self._is_video = path.suffix.lower() in self.VIDEO_EXTS
        if not self._is_video:
            try:
                with Image.open(path) as handle:
                    self._draw_image(self._fit(handle.convert("RGB")))
            except Exception as e:
                self._message(f"Could not open {path.name}:\n{e}")
            return

        if cv2 is None:
            self._message(
                f"{path.name} is a video, but OpenCV is not installed —\n"
                "pip3 install opencv-python-headless"
            )
            return
        self._message(f"Loading video:\n{path.name}")
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._play_video, args=(path,), daemon=True)
        self._worker.start()

    def _stop_video(self):
        if self._worker is not None:
            self._stop.set()
            self._worker.join(timeout=1.0)
            self._worker = None
        process, self._vlc_process = self._vlc_process, None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait(timeout=1.0)
        with self._lock:
            self._latest = None

    # ----- video -------------------------------------------------------------

    def _play_video(self, path: Path):
        """Decode frames into _latest until told to stop. Worker thread."""
        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            capture.release()
            self.after(
                0,
                lambda: self._video_error(
                    path.name, "OpenCV could not open the file"
                ),
            )
            return
        fps = capture.get(cv2.CAP_PROP_FPS)
        source_fps = fps if fps and fps > 0 else 25.0
        output_frame_step = source_fps / MEDIA_MAX_FPS
        try:
            timeline_start = time.monotonic()
            frame_index = 0
            next_output_frame = 0.0
            decoded_frames = 0
            while not self._stop.is_set():
                ok = capture.grab()
                if not ok:
                    # End of the file: rewind and keep going.
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok = capture.grab()
                    if not ok:
                        if decoded_frames == 0:
                            self.after(
                                0,
                                lambda: self._video_error(
                                    path.name,
                                    "No video frames could be decoded. "
                                    "Re-encode as H.264 MP4 for Raspberry Pi.",
                                ),
                            )
                        break
                    frame_index = 0
                    next_output_frame = 0.0
                    timeline_start = time.monotonic()

                if frame_index >= next_output_frame:
                    ok, frame = capture.retrieve()
                    if not ok:
                        frame_index += 1
                        continue
                    image = self._fit_video_frame(frame)
                    with self._lock:
                        self._latest = image
                    decoded_frames += 1
                    next_output_frame += output_frame_step

                frame_index += 1
                elapsed = time.monotonic() - timeline_start
                expected_frame = int(elapsed * source_fps)
                if expected_frame - frame_index > max(5, int(source_fps * 0.5)):
                    if capture.set(cv2.CAP_PROP_POS_FRAMES, expected_frame):
                        frame_index = expected_frame
                        next_output_frame = float(frame_index)

                due = timeline_start + frame_index / source_fps
                nap = due - time.monotonic()
                if nap > 0:
                    self._stop.wait(nap)
        except Exception as exc:
            error = str(exc)
            self.after(
                0,
                lambda error=error: self._video_error(path.name, error),
            )
        finally:
            capture.release()

    def _video_error(self, name: str, reason: str):
        """Show decode failures on the panel and in the GUI status line."""
        self._message(f"Could not play {name}:\n{reason}")
        self._on_status(f"Media: could not play {name} ({reason})")

    def _refresh(self):
        """Draw the newest decoded frame, if there is one. UI thread."""
        if not self.winfo_exists():
            return  # the window is going away; stop rescheduling
        with self._lock:
            image = self._latest
            self._latest = None
        if image is not None:
            try:
                self._draw_video_image(image)
            except Exception as exc:
                self._stop.set()
                self._video_error(self._path.name if self._path else "video", str(exc))
        self.after(MEDIA_TICK_MS, self._refresh)

    # ----- what the sequence asks for ----------------------------------------

    def available_files(self) -> list[str]:
        """The filenames a "media:<name>" step can choose from, for the picker."""
        return [path.name for path in self._files]

    def show_vlc_file(self, name: str) -> bool:
        """Play a video in this widget with VLC's native decoder on X11."""
        path = self._resolve(name)
        if path is None:
            self._on_status(f"Media: {name} is not in {MEDIA_DIR}/")
            return False
        if self.tk.call("tk", "windowingsystem") != "x11":
            self._on_status("VLC screensaver embedding requires Tk on X11")
            return False
        executable = shutil.which("cvlc") or shutil.which("vlc")
        if executable is None:
            self._on_status("VLC not found; falling back to OpenCV playback")
            return False

        self._stop_video()
        self._path = path
        self._is_video = True
        self.update_idletasks()
        drawable = self.view._label.winfo_id()
        command = [
            executable,
            "--intf",
            "dummy",
            "--no-one-instance",
            "--no-video-title-show",
            "--no-osd",
            "--no-audio",
            "--loop",
            "--quiet",
            "--drawable-xid",
            str(drawable),
            str(path),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._on_status(f"Could not start VLC ({exc}); falling back to OpenCV")
            return False
        self._vlc_process = process
        self.after(750, lambda: self._check_vlc_process(process, name))
        return True

    def _check_vlc_process(self, process: subprocess.Popen, name: str):
        if process is not self._vlc_process or process.poll() is None:
            return
        self._vlc_process = None
        self._on_status("VLC exited during startup; falling back to OpenCV playback")
        self.show_file(name)

    def show_file(self, name: str | None):
        """Show one file, or go back to the idle picture when name is None.

        This is what a "media:<filename>" step in a preset or the queue comes
        down to. It touches Tk, so a worker thread must call it through
        self.after(0, …) — see gui_core.RobotAppBase._run_media_step.

        A name that is not there is a line in the panel and a word on the
        status line, never an exception: a missing clip must not stop a run
        that the robot is halfway through.
        """
        if name is None:
            self._path = self._idle_path()
            self._show_current()
            return
        path = self._resolve(name)
        if path is None:
            self._on_status(f"Media: {name} is not in {MEDIA_DIR}/ — panel unchanged")
            self._message(f"{name}\nis not in {MEDIA_DIR}/")
            return
        self._path = path
        self._show_current()

    def reload(self):
        """Pick up files added to MEDIA_DIR since the GUI started."""
        self._files = self._discover()
        self._path = self._idle_path()
        self._show_current()

    def close(self):
        self._stop_video()
