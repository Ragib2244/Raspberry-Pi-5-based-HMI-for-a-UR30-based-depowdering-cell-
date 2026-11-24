import socket
import threading
import time
from typing import Optional

ROBOT_IP = "192.0.2.1"  # Documentation-only placeholder; configure locally.

# The Dashboard Server accepts only one connection at a time, so all access is
# serialised through this lock to stay safe when called from several threads
# (e.g. a GUI worker polling state while a STOP button fires).
_dashboard_lock = threading.Lock()


def dashboard_command(command: str, timeout: float = 5.0) -> str:
    """Send a single command to the Dashboard Server (port 29999)."""
    with _dashboard_lock:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((ROBOT_IP, 29999))
            s.recv(1024)  # welcome banner
            s.sendall((command + "\n").encode())
            return s.recv(1024).decode().strip()


def load_program(path: str):
    """Load a .urp program by its path on the robot controller.

    The Dashboard Server only answers once the program *and* its associated
    installation have loaded, which can take several seconds - hence the longer
    timeout. Answers are "Loading program: <p>", "File not found: <p>" or
    "Error while loading program: <p>" (the last one means the installation
    belonging to the program wants a safety confirmation on the pendant).
    """
    response = dashboard_command(f"load {path}", timeout=20.0)
    print(f"response: {response}")
    return response


def load_installation(path: str):
    """Load an .installation file. Fails with "Failed to load installation"
    if the installation's safety settings differ from the active ones - that
    confirmation can only be given on the teach pendant."""
    response = dashboard_command(f"load installation {path}", timeout=20.0)
    print(f"response: {response}")
    return response


def play() -> str:
    """Start the loaded program.

    Answers "Starting program" or "Failed to execute: play". A load can succeed
    while play is refused, because the two have different preconditions: play
    needs the arm powered on with brakes released (robotmode RUNNING), the
    safety state NORMAL, and a program without node errors. A program saved
    against a different installation loads fine but its nodes cannot resolve
    TCPs, features or installation variables that the active installation does
    not have - PolyScope then refuses to run it and logs the reason.
    """
    response = dashboard_command("play")
    print(response)
    return response


def pause() -> str:
    """Pause the running program, keeping its execution point.

    Answers "Pausing program", or "Failed to execute: pause" when nothing is
    playing. A paused program is resumed with play(), which continues from the
    node it stopped at — unlike stop(), which discards the position so the next
    play() starts the program from the beginning.
    """
    response = dashboard_command("pause")
    print(response)
    return response


def stop() -> str:
    response = dashboard_command("stop")
    print(response)
    return response


def program_state() -> str:
    return dashboard_command("programstate")


def robot_mode() -> str:
    """e.g. "Robotmode: RUNNING". Anything but RUNNING means the arm is not
    powered on and brake released, and play will be refused."""
    return dashboard_command("robotmode")


def safety_status() -> str:
    return dashboard_command("safetystatus")


def not_ready_reason() -> Optional[str]:
    """Return why play would be refused, or None if the robot looks ready.

    Deliberately read-only: powering on and releasing the brakes makes the arm
    move, so that stays a conscious act via power_on_and_release_brakes().
    """
    mode = robot_mode()
    if "RUNNING" not in mode:
        return f"arm not ready ({mode}) - power on and release the brakes"
    status = safety_status()
    if "NORMAL" not in status:
        return f"safety not normal ({status})"
    return None


def power_on_and_release_brakes(timeout: float = 30.0) -> bool:
    """Power the arm on and release its brakes, then wait for robotmode RUNNING.

    The arm physically holds itself when the brakes release, so only call this
    when someone is aware the robot is about to become live.
    """
    dashboard_command("power on")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "IDLE" in robot_mode():
            break
        time.sleep(0.5)
    dashboard_command("brake release")
    while time.time() < deadline:
        if "RUNNING" in robot_mode():
            return True
        time.sleep(0.5)
    return False


def set_payload(mass_kg: float, cog: list[float] = [0.0, 0.0, 0.0]):
    """Set the payload mass (kg) and center of gravity [x, y, z] in meters."""
    script = f"set_payload({mass_kg}, [{cog[0]}, {cog[1]}, {cog[2]}])"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(5.0)
        s.connect((ROBOT_IP, 30001))
        s.sendall((script + "\n").encode())


def read_scale() -> float:
    """Return the current payload mass in kg from the scale.

    TODO: integrate the real scale hardware here. Until then this raises so the
    GUI can report that the scale is not connected yet instead of sending a
    bogus payload to the robot.
    """
    raise NotImplementedError("Scale not connected yet")


if __name__ == "__main__":
    # --- Example usage ---
    load_program("/programs/myprogram.urp")
    time.sleep(0.5)
    play()

    while True:
        state = program_state()
        print(state)
        if "STOPPED" in state:
            break
        time.sleep(1)
