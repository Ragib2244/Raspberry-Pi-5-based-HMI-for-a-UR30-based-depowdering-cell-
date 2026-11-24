"""One-shot check for why a program from the USB stick loads but will not run.

Point rc.ROBOT_IP at the *real* controller before running this - URSim in a VM
has no USB stick, so every usbdisk path there answers "File not found".

    python diagnose_usb.py                 # state + load only, robot stays still
    python diagnose_usb.py --play          # also start the program: IT MOVES

Loading and playing have different preconditions, which is why a load can
succeed and the play right after it be refused:

    "File not found: <p>"           wrong path/filename, or stick not mounted
    "Error while loading program"   the installation belonging to the program
                                    wants a safety confirmation on the pendant
    "Failed to execute: play"       arm not powered on and brake released, or
                                    safety state not NORMAL, or the program has
                                    node errors - unresolved TCPs, features or
                                    installation variables it was saved with.
                                    The log on the pendant names the node.
"""

import sys

import robotControl as rc

# The program on the stick, as PolyScope shows it below the USB icon. The
# controller mounts the stick inside the programs folder, so it is reachable at
# /programs/usbdisk even though the pendant draws it as a separate device.
PROGRAM_PATH = "/programs/usbdisk/ES_test/p3.urp"

# Tried only if PROGRAM_PATH answers "File not found" - older control boxes
# mount the stick elsewhere.
FALLBACK_PATHS = [
    "usbdisk/ES_test/p3.urp",  # same place, relative to the programs folder
    "/media/sdb1/ES_test/p3.urp",  # CB2
    "/mnt/usbdisk1/ES_test/p3.urp",  # CB1
]

STATE_QUERIES = [
    ("PolyScope version", "PolyscopeVersion"),
    ("Robot model", "get robot model"),
    ("Robot mode", "robotmode"),  # must be RUNNING before play is allowed
    ("Safety status", "safetystatus"),  # must be NORMAL
    ("Remote control", "is in remote control"),  # e-Series: load/play need true
    ("Operational mode", "get operational mode"),  # AUTOMATIC forbids loading
    ("Program state", "programstate"),
    ("Loaded program", "get loaded program"),
]


def show(label: str, command: str):
    try:
        print(f"  {label:<18} {rc.dashboard_command(command)}")
    except Exception as e:
        print(f"  {label:<18} <failed: {e}>")


def try_load(path: str) -> str:
    try:
        answer = rc.dashboard_command(f"load {path}", timeout=20.0)
    except Exception as e:
        answer = f"<failed: {e}>"
    print(f"  load {path}\n      -> {answer}")
    return answer


def main():
    play_it = "--play" in sys.argv
    print(f"Robot: {rc.ROBOT_IP}\n")

    print("Controller state")
    for label, command in STATE_QUERIES:
        show(label, command)

    state = rc.program_state()
    if "STOPPED" not in state:
        sys.exit(f"\nprogramstate is {state} - stop the program first, "
                 "this script will not load over a running one.")

    print("\nLoading")
    answer = try_load(PROGRAM_PATH)
    if answer.startswith("File not found"):
        for path in FALLBACK_PATHS:
            answer = try_load(path)
            if not answer.startswith("File not found"):
                break
    if not answer.startswith("Loading program"):
        sys.exit("\nThe program never loaded - nothing to play.")

    reason = rc.not_ready_reason()
    print(f"\nReady to play? {'yes' if reason is None else 'no - ' + reason}")

    if not play_it:
        print("Re-run with --play to start it (the robot will move).")
        return

    print("\n*** Starting the program - stay clear of the robot. ***")
    answer = rc.play()
    if answer.startswith("Starting program"):
        print(f"  started, programstate is now {rc.program_state()}")
    else:
        print(f"  refused: {answer}")
        print(f"  {rc.robot_mode()} / {rc.safety_status()}")
        print("  If both look fine, the program itself is the problem: open it "
              "on the pendant and check the log for node errors.")


if __name__ == "__main__":
    main()
