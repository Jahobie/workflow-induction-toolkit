# Human Activity Recording Tool

Records mouse clicks, scrolling, and keyboard actions with associated JPEG
screenshots. Actions are stored in `actions.db` through the existing
SQLAlchemy/SQLite persistence layer; images are stored in `screenshots/`.
The default output directory is `~/Downloads/records`.

This fork adds native Fedora/GNOME Wayland recording alongside the original
macOS backend. It also adds output-directory selection, safer shutdown,
Linux scroll screenshot throttling, and protection against terminal output
blocking the recorder. Changes to recording are contained in this package;
the workflow-induction implementation is unchanged.

This is an action-and-screenshot recorder, not continuous video, an agent-log
exporter, or a terminal transcript. It does not capture off-screen tmux
scrollback or reconstruct pasted text as individual keystrokes.

## Installation

### macOS

From the repository root:

```bash
python3 -m venv computer-recorder/.venv
computer-recorder/.venv/bin/python -m pip install -e ./computer-recorder
```

Grant Accessibility and Screen Recording permissions to the application
launching the recorder in macOS System Settings. The macOS backend uses
Quartz/mss for capture and pynput for input.

### Fedora Linux with GNOME Wayland

Install the system bindings and give your user access to input devices:

```bash
sudo dnf install python3-gobject python3-evdev python3-xkbcommon \
    gstreamer1-plugin-pipewire libinput
sudo usermod -aG input "$USER"
```

Log out and back in after changing group membership. Membership in the
`input` group permits reading input-device events, including keyboard input.

From the repository root, create a virtual environment that can use Fedora's
system Python bindings:

```bash
python3 -m venv --system-site-packages computer-recorder/.venv
computer-recorder/.venv/bin/python -m pip install -e './computer-recorder[linux]'
```

Install the included GNOME Shell extension, also from the repository root:

```bash
mkdir -p ~/.local/share/gnome-shell/extensions
ln -sfn "$PWD/computer-recorder/crec/linux/gnome-shell-extension/crec-input@crec" \
    ~/.local/share/gnome-shell/extensions/crec-input@crec
```

Log out and back in so GNOME discovers the extension, then enable it:

```bash
gnome-extensions enable crec-input@crec
```

The extension supplies compositor pointer history, monitor geometry, keyboard
configuration history, and input-device settings. It is required for this
Linux backend. Its D-Bus methods are accessible to other clients on the same
user's session bus; callers are not authenticated as specifically the recorder.
History is kept in memory.

After changing extension files, log out and back in to load them. Python-only
changes require restarting the recorder, not GNOME. The extension declares
support for GNOME Shell 45–50; that declaration is not a claim that every
version has been tested.

## Record a task

From the repository root:

```bash
computer-recorder/.venv/bin/python -I -B -m crec.cli \
  --user-name your-name \
  --output-dir "$HOME/recordings/$(date +%Y%m%d-%H%M%S)" \
  --scroll-debounce 0.5
```

The installed `crec` command accepts the same arguments when the environment
is activated. `--output-dir` expands `~` and resolves relative paths against
the current working directory. Use a new directory for each task; reusing a
directory appends observations to its existing database.

On Linux, approve whole-screen sharing and select **all monitors** if the
portal asks. A saved restore token may allow later runs to reuse authorization;
the portal controls whether another prompt appears.

Keep the recorder running in a separate terminal or tmux session while you work.
Detaching tmux leaves the recorder running. Stop with **Ctrl+C in its terminal**
and wait for the process to exit before packaging the result.

The CLI logs `Screen observer started` after backend startup; it does not
print `LIVE_READY`. Before starting substantial work, type and click briefly
and check that new screenshots and observations appear in the selected folder.
Check periodically during long tasks. An error followed by a shell prompt
means the recorder has stopped, even if startup initially appeared successful.

Output:

```text
<output-dir>/
  actions.db
  actions.db-wal       # may be present
  actions.db-shm       # may be present
  screenshots/
    <timestamp>_<action>_<stage>.jpg
```

SQLite uses WAL mode. Preserve the database and any WAL/SHM sidecars together;
do not delete them to reduce upload size. Recordings and local review artifacts
are excluded from version control.

## What changed in the Linux adaptation

### Capture and input

| Component | Linux implementation |
| --- | --- |
| Screen capture | xdg-desktop-portal, PipeWire, and GStreamer |
| Keyboard input | evdev, decoded using GNOME's active XKB configuration |
| Pointer buttons and scrolling | libinput, including wheel and touchpad events |
| Coordinates and monitor layout | Included GNOME Shell extension |
| Persistence | Existing SQLAlchemy Observation model and SQLite database |

Mac-only imports and dependencies are platform-gated. Linux dependencies are
available through the `linux` installation extra; the GNOME extension is
included in package data.

Keyboard timestamps use Linux's `EVIOCSCLOCKID` ioctl to select the monotonic
clock, including on evdev versions without a Python clock-selection wrapper.
Keyboard and pointer events are merged by acquisition time with a 25 ms
holdback. Keyboard configuration history allows queued keys to use the layout
active when the event occurred. Repeats are retained; keyboard releases update
state but are not separate key observations.

Pointer positions combine GNOME samples with ordered libinput motion timestamps.
Stationary sampling gaps can be resolved when motion evidence establishes that
the pointer did not move. The extension samples approximately every 2 ms;
this is scheduled sampling, not a guaranteed hardware frequency.

Monitor matching uses GNOME logical geometry and explicit stream identity.
Action coordinates are logical coordinates **local to the selected monitor**.
Pointer annotations are scaled to screenshot pixels. Equal-sized displays,
negative origins, and mixed scaling have regression coverage.

### Screenshot timing and retention

Before frames are selected and pinned at acquisition, so a delayed callback
cannot silently replace them with the current screen. Coordinates are retained
through long button holds and queued events.

Clicks retain press and release timing internally. Their after-frame target is
release plus 75 ms; scroll after-frame targets use event time plus 75 ms.
GStreamer presentation timestamps are mapped into the monotonic clock.
An unchanged screen may supply a valid after image; the recorder does not
require every action to change pixels.

These are immediate post-action images, not a guarantee that a network request
or other asynchronous application operation has finished. Missing after frames,
stream errors, and delivery stalls are reported explicitly.

Keyboard screenshot retention keeps the first and final images of typing
sequences and removes intermediate images. All acquired key observations still
go to the database. A screenshot count is therefore not a keystroke count.

### Linux scroll screenshot throttling

Every acquired Linux scroll action retains its raw coordinates and deltas in
the database. `--scroll-debounce` limits how often normal scroll screenshot
pairs are saved, using the event's acquisition timestamp:

- `--scroll-debounce 0.5` (default): at most one normal pair every 0.5 seconds.
- `--scroll-debounce 1.0`: at most one normal pair every second.
- `--scroll-debounce 0`: retain screenshot pairs for every scroll event.

Each pair contains a before and an after image. A stationary mouse pointer does
not prevent later scroll screenshots. Ambiguous-position fallback images and
incomplete-event reporting bypass this throttle.

This is time-based sampling, not gesture batching: deltas are not combined,
and the final position of every scroll gesture is not guaranteed a screenshot.
The legacy macOS filter remains separate. Its `--scroll-min-distance`,
`--scroll-max-frequency`, and `--scroll-session-timeout` options do not affect
the Linux path; its distance check measures pointer movement.

### Terminal and tmux protection

Ctrl+S in a terminal with software flow control enabled can pause its output.
Previously, synchronous console logging could then block the asyncio thread,
stop input collection, and eventually cause a kernel keyboard-buffer overflow.

The CLI now disables `IXON` while it runs and restores the original terminal
settings on exit. The default recorder console handler uses a separate daemon
thread and a bounded queue of 1,024 diagnostic messages. Native terminal writes
use a duplicated descriptor so a stalled writer does not hold a Python
buffered-stream lock or delay interpreter shutdown.

If that queue fills, console messages can be dropped; it is separate from the
recorded-action queue and does not discard database observations. Screen and
Linux-backend diagnostics route through the recorder logger. Applications
embedding the library that install their own logging handlers are responsible
for ensuring those handlers do not block.

### Ownership, memory, and persistence

Blocking D-Bus setup, image work, and other backend operations are offloaded
from the input event loop. Queued Linux images are losslessly spooled to private
`.crec-frames-*` directories beneath `screenshots/`. Queues hold file references
instead of full RGB buffers. A cleanup thread retires files when no event or
history entry needs them, and normal shutdown removes the spool.

This bounds queued pixel memory at the cost of temporary disk space and I/O.
It does not bound every queue under arbitrary load. Use disk-backed storage for
long recordings and allow space for both screenshots and temporary frames.

Observer shutdown no longer discards queued updates. The recorder drains acquired
actions, preserves per-observer commit ordering, and waits for database writes
before finishing. Write failures are surfaced and failed updates remain in
memory for recovery; this is not a durable retry journal after process death.

### Output compatibility and explicit limitations

The existing ORM schema is retained. Action strings such as
`key_press('a')`, `click_left(120.0, 240.0)`, and
`scroll(120.0, 240.0, dx=0.00, dy=-1.00)` remain in `observations`.
Database timestamps are insertion times; screenshot filenames use event
timestamps. The schema has no new screenshot foreign key or tmux pane ID.

Ordinary filenames remain `{timestamp}_{action}_{stage}.jpg`. On Linux, an
action containing `/` uses a `~crec1~` prefix and percent-encoding for the
filename component, because a slash cannot be part of a filename:

```text
123.00000_~crec1~key_press('%2F')_first.jpg
```

Removing the prefix and percent-decoding recovers the original action/stage.
The database still stores the raw action. This filename extension is a
documented difference from the legacy format.

When event-time pointer position remains ambiguous after a historical retry,
the recorder retains the action, uses `nan` for unknown pointer-action
coordinates, saves available unmarked images for candidate monitors, and writes
`recorder_incomplete(...)`. Fallback filenames include
`_unlocated_mon{number}`. Recording continues after these local gaps.
Stopping with a button held also records that its release was not observed.

`recorder_error(...)` indicates a fatal failure. A clean exit alone does not
mean every action has complete coordinates or images. A keyboard-overflow
startup failure has also been observed separately from the fixed Ctrl+S
failure; do not proceed with a run that reports it.

IBus input methods, independent XKB `grp:` switching, missing/outdated extensions,
and unreadable input devices are reported as unsupported or failed acquisition.
GNOME's normal XKB input-source switcher supports layouts and variants.
Faithful input on KDE and wlroots desktops is not currently supported.

## Verification

From the repository root, using the environment above:

```bash
computer-recorder/.venv/bin/python -I -B -m unittest discover \
  -s computer-recorder/tests -v
```

The suite covers timing, monitor association, keyboard translation, shutdown,
scroll throttling, and persistence. Most capture/input tests use fake hardware
boundaries. Terminal tests use real POSIX pseudo-terminals and SQLite with
synthetic actions: they pause output with Ctrl+S and verify persistence and
process exit, including Ctrl+C shutdown, while output remains paused.
These tests do not establish universal live desktop compatibility.

Live Fedora/GNOME recordings have exercised terminal/tmux work, typing, clicks,
scrolling, repeated Ctrl+S, and shutdown. Coverage is limited to the exercised
host and interactions. Before relying on a new setup:

1. Verify new database observations and screenshots after a few actions.
2. Press Ctrl+S in the recorder terminal, then verify later actions still save.
3. Check pointer annotations and before/after images on every selected monitor.
4. Exercise your keyboard layout, modifiers, wheel, and touchpad.
5. Stop with Ctrl+C and inspect both fatal-error and incomplete observations.

To summarize a stopped recording, set `recording_dir` to its directory:

```bash
recording_dir="$HOME/recordings/your-session"
computer-recorder/.venv/bin/python -I -B - "$recording_dir" <<'PY'
from pathlib import Path
import sqlite3
import sys

root = Path(sys.argv[1]).expanduser().resolve()
with sqlite3.connect((root / "actions.db").as_uri() + "?mode=ro", uri=True) as db:
    print("Observations:", db.execute("SELECT count(*) FROM observations").fetchone()[0])
    for content, count in db.execute(
        "SELECT content, count(*) FROM observations "
        "WHERE content LIKE 'recorder_%' GROUP BY content"
    ):
        print(count, content)
print("Screenshots:", len(list((root / "screenshots").glob("*.jpg"))))
PY
```

Long recordings can exceed upload limits. Preserve the original recording and
make a separate upload copy if additional JPEG compression is needed. Keep
every image, its dimensions and filename, and the database files together.
The recorder does not automatically recompress existing recordings for upload.
