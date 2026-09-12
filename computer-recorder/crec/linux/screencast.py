"""Screen capture via xdg-desktop-portal ScreenCast + PipeWire.

This is the Wayland-native replacement for ``mss`` (which returns black frames
under Wayland because it goes through XWayland).  The flow is:

    1. Ask ``org.freedesktop.portal.ScreenCast`` to CreateSession, SelectSources
       (all monitors) and Start.  The user approves once; a restore token is
       persisted so later runs are silent.
    2. OpenPipeWireRemote to get a PipeWire fd, then run one GStreamer
       ``pipewiresrc`` pipeline per monitor into an ``appsink`` that always
       holds the most recent frame.
    3. ``grab(monitor)`` returns that frame as a ``Frame`` whose ``width``,
       ``height`` and ``rgb`` attributes match what ``mss`` produced, so the
       Pillow code in ``screen.py`` works unchanged.

The portal handshake needs a GLib main loop to receive its asynchronous
``Response`` signals; that loop runs on a dedicated thread for the lifetime of
the capture so the session (and its PipeWire fd) stays alive.
"""

from __future__ import annotations

import logging
import os
import queue
import random
import string
import threading
import tempfile
import time
import weakref
import zlib
from collections import deque
from dataclasses import dataclass, replace

log = logging.getLogger("crec.linux.screencast")

_PORTAL = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_SC_IFACE = "org.freedesktop.portal.ScreenCast"

# Portal cursor modes / source types / persist modes (see the portal spec).
_CURSOR_EMBEDDED = 2  # draw the cursor into the captured frames
_SOURCE_MONITOR = 1
_PERSIST_PERSISTENT = 2

_STATE_DIR = os.path.expanduser(
    os.path.join(os.environ.get("XDG_STATE_HOME", "~/.local/state"), "crec")
)
_TOKEN_FILE = os.path.join(_STATE_DIR, "screencast.token")


class ScreenCastError(RuntimeError):
    """Raised when the portal handshake or the GStreamer pipeline fails."""


def _token() -> str:
    return "crec" + "".join(random.choices(string.ascii_lowercase, k=12))


def _read_restore_token() -> str:
    try:
        with open(_TOKEN_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _write_restore_token(token: str) -> None:
    if not token:
        return
    temporary = None
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".screencast-", dir=_STATE_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(token)
        # Replaces an existing file/symlink without following it.
        os.replace(temporary, _TOKEN_FILE)
    except OSError as exc:
        log.debug("could not persist restore token: %s", exc)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class Frame:
    """An immutable RGB sample with a presentation time on CLOCK_MONOTONIC."""
    width: int
    height: int
    rgb: bytes
    captured_at: float = 0.0  # CLOCK_MONOTONIC; zero only for legacy/test frames
    repeated: bool = False  # PipeWire keepalive, identified by unchanged sequence


class StoredFrame:
    """Small shared handle; event queues never own uncompressed pixel buffers."""

    def __init__(self, frame, path, retire):
        self.width, self.height = frame.width, frame.height
        self.captured_at = frame.captured_at
        self.repeated = frame.repeated
        self.path = path
        self._cleanup = weakref.finalize(self, retire, path)

    @staticmethod
    def _unlink(path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    @property
    def rgb(self):
        with open(self.path, "rb") as source:
            data = zlib.decompress(source.read())
        if len(data) != self.width * self.height * 3:
            raise ScreenCastError("Corrupt temporary screenshot")
        return data


class EventFrames:
    """Pin the event-time frame and a settled frame after action completion."""

    def __init__(self, capture, index, at, before, after):
        self.capture, self.index, self.at = capture, index, at
        self.before, self.after = before, None
        self.target = None

    def __getitem__(self, index):
        return (self.before, self.after)[index]

    def resolve_after(self, timeout=3):
        # Callers outside the input backend have no separate completion edge.
        # Give them the same bounded settling policy starting at the event.
        if self.target is None:
            self.complete(self.at)
        deadline = time.monotonic() + timeout
        with self.capture._condition:
            while self.after is None:
                error = self.capture._stream_errors.get(self.index)
                if error:
                    raise error
                if self.capture._reader_stop.is_set():
                    raise ScreenCastError("ScreenCast stopped before the after screenshot")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    error = ScreenCastError(f"No fresh after frame within {timeout:g} seconds; capture stalled")
                    self.capture._stream_errors[self.index] = error
                    self.capture._condition.notify_all()
                    raise error
                self.capture._condition.wait(remaining)
            return self.after

    def complete(self, completed_at=None, settle=0.075):
        """Arm after-frame selection once the action has actually completed."""
        target = max(self.at, self.at if completed_at is None else completed_at) + settle
        with self.capture._condition:
            if self.target is not None:
                return
            self.target = target
            history = self.capture._history.get(self.index, ())
            self.after = next((frame for frame in history
                               if self.capture._is_after(frame, target)), None)
            if self.after is not None:
                self.capture._pending_frames.get(self.index, ()).discard(self)
            self.capture._condition.notify_all()


class PortalScreenCast:
    """Capture every monitor through the ScreenCast portal.

    Attributes
    ----------
    monitors : list[dict]
        One dict per captured monitor with ``left``/``top``/``width``/``height``
        in *logical* coordinates (matching the pointer coordinate space) plus
        ``scale`` (pixels per logical unit).  Ordered to match the streams.
    """

    def __init__(self, monitor_hints: list[dict] | None = None, spool_directory=None) -> None:
        # Optional logical geometry from the shell bridge, used to label streams.
        self._hints = monitor_hints or []
        self.monitors: list[dict] = []

        self._gi = None
        self._Gst = None
        self._bus = None
        self._loop = None
        self._loop_thread: threading.Thread | None = None
        self._pipelines: list = []
        self._sinks: list = []
        self._dup_fds: list[int] = []
        self._session_handle = ""
        self._cancel_start = threading.Event()
        self._reader_stop = threading.Event()
        self._readers = []
        self._condition = threading.Condition()
        self._latest = {}
        self._history = {}
        self._stream_errors = {}
        self._pending_frames = {}
        self._last_sample = {}
        self._last_sequence = {}
        # Serialize decode/spooling across monitors: history and pending events
        # retain disk handles, with at most one decoded sample in this stage.
        self._decode_lock = threading.Lock()
        self._spool_directory = spool_directory
        self._spool = None
        self._retired = queue.Queue()
        self._cleanup_thread = None
        self._cleanup_error = None
        self._retire_lock = threading.Lock()
        self._accept_retirements = True

    def _cleanup_files(self):
        while True:
            path = self._retired.get()
            try:
                if path is None:
                    return
                try:
                    StoredFrame._unlink(path)
                except OSError as exc:
                    self._cleanup_error = exc
                    log.error("Cannot retire temporary screenshot %s: %s", path, exc)
            finally:
                self._retired.task_done()

    def _retire_frame(self, path):
        # A finalizer may run after stop removed the complete spool directory.
        # In that case there is no remaining filesystem work to schedule.
        with self._retire_lock:
            if self._accept_retirements and self._cleanup_thread is not None:
                self._retired.put(path)

    # ------------------------------------------------------------------ start
    def cancel_start(self) -> None:
        self._cancel_start.set()

    def _check_cancelled(self) -> None:
        if self._cancel_start.is_set():
            raise ScreenCastError("screen capture startup cancelled")

    def start(self) -> None:
        try:
            self._start()
        except Exception as exc:
            self.stop()
            if isinstance(exc, ScreenCastError):
                raise
            raise ScreenCastError(f"ScreenCast setup failed: {exc}") from exc
        except BaseException:
            self.stop()
            raise

    def _start(self) -> None:
        self._check_cancelled()
        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        gi.require_version("Gst", "1.0")
        from gi.repository import Gio, GLib, Gst

        self._gi = (Gio, GLib)
        self._Gst = Gst
        Gst.init(None)

        if Gst.ElementFactory.find("pipewiresrc") is None:
            raise ScreenCastError(
                "GStreamer 'pipewiresrc' element not found. Install the "
                "pipewire GStreamer plugin (Fedora: gstreamer1-plugin-pipewire)."
            )

        self._context = GLib.MainContext.new()
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._proxy = Gio.DBusProxy.new_sync(
            self._bus,
            Gio.DBusProxyFlags.NONE,
            None,
            _PORTAL,
            _PORTAL_PATH,
            _SC_IFACE,
            None,
        )

        # Drive portal Response signals on a private GLib loop/thread.
        self._loop = GLib.MainLoop.new(self._context, False)
        loop = self._loop
        def run_loop():
            self._context.push_thread_default()
            try:
                loop.run()
            finally:
                self._context.pop_thread_default()
        self._loop_thread = threading.Thread(
            target=run_loop, name="crec-glib", daemon=True
        )
        self._loop_thread.start()

        streams, fd = self._handshake()
        try:
            self._check_cancelled()
            self._build_pipelines(streams, fd)
        finally:
            os.close(fd)


    def _on_glib(self, function, *args):
        """Subscribe on the loop's thread so Gio delivers on its private context."""
        _, GLib = self._gi
        done = threading.Event()
        result = []
        def invoke(*_):
            try:
                result.append((True, function(*args)))
            except BaseException as exc:
                result.append((False, exc))
            finally:
                done.set()
            return False
        source = GLib.idle_source_new()
        source.set_callback(invoke)
        source.attach(self._context)
        if not done.wait(5):
            source.destroy()
            raise ScreenCastError("GLib loop did not respond")
        success, value = result[0]
        if not success:
            raise value
        return value

    # -------------------------------------------------------------- handshake
    def _sender(self) -> str:
        return self._bus.get_unique_name()[1:].replace(".", "_")

    def _call_with_response(self, method: str, build_args) -> tuple[int, dict]:
        Gio, GLib = self._gi
        handle_token = _token()
        req_path = (
            f"/org/freedesktop/portal/desktop/request/"
            f"{self._sender()}/{handle_token}"
        )
        result: dict = {}
        done = threading.Event()

        def on_response(_c, _s, _o, _i, _sig, params):
            code, res = params.unpack()
            result["code"] = code
            result["results"] = res
            done.set()

        sub = self._on_glib(self._bus.signal_subscribe,
            _PORTAL,
            "org.freedesktop.portal.Request",
            "Response",
            req_path,
            None,
            Gio.DBusSignalFlags.NO_MATCH_RULE,
            on_response,
        )
        try:
            ret = self._proxy.call_sync(
                method, build_args(handle_token), Gio.DBusCallFlags.NONE, 5000, None
            )
            actual = ret.unpack()[0]
            if actual != req_path:
                # Some portal versions return a different request path.
                self._bus.signal_unsubscribe(sub)
                sub = self._on_glib(self._bus.signal_subscribe,
                    _PORTAL,
                    "org.freedesktop.portal.Request",
                    "Response",
                    actual,
                    None,
                    Gio.DBusSignalFlags.NO_MATCH_RULE,
                    on_response,
                )
            deadline = time.monotonic() + 180
            while not done.wait(timeout=0.1):
                if self._cancel_start.is_set() or time.monotonic() >= deadline:
                    self._bus.call_sync(
                        _PORTAL, actual, "org.freedesktop.portal.Request",
                        "Close", None, None, Gio.DBusCallFlags.NONE, 2000, None,
                    )
                    raise ScreenCastError(f"portal {method} cancelled or timed out")
        finally:
            self._bus.signal_unsubscribe(sub)
        return result["code"], result["results"]

    def _handshake(self):
        Gio, GLib = self._gi

        code, res = self._call_with_response(
            "CreateSession",
            lambda ht: GLib.Variant(
                "(a{sv})",
                (
                    {
                        "handle_token": GLib.Variant("s", ht),
                        "session_handle_token": GLib.Variant("s", _token()),
                    },
                ),
            ),
        )
        if code != 0:
            raise ScreenCastError(f"CreateSession failed (code {code})")
        self._session_handle = res["session_handle"]

        select_opts = {
            "handle_token": GLib.Variant("s", _token()),
            "types": GLib.Variant("u", _SOURCE_MONITOR),
            "multiple": GLib.Variant("b", True),
            "cursor_mode": GLib.Variant("u", _CURSOR_EMBEDDED),
            "persist_mode": GLib.Variant("u", _PERSIST_PERSISTENT),
        }
        restore = _read_restore_token()
        if restore:
            select_opts["restore_token"] = GLib.Variant("s", restore)

        code, res = self._call_with_response(
            "SelectSources",
            lambda ht: GLib.Variant(
                "(oa{sv})",
                (self._session_handle, {**select_opts, "handle_token": GLib.Variant("s", ht)}),
            ),
        )
        if code != 0:
            raise ScreenCastError(f"SelectSources failed (code {code})")

        code, res = self._call_with_response(
            "Start",
            lambda ht: GLib.Variant(
                "(osa{sv})",
                (self._session_handle, "", {"handle_token": GLib.Variant("s", ht)}),
            ),
        )
        if code != 0:
            raise ScreenCastError(
                f"Start failed (code {code}); screen-capture permission denied?"
            )
        _write_restore_token(res.get("restore_token", ""))

        streams = res.get("streams", [])
        if not streams:
            raise ScreenCastError("portal returned no ScreenCast streams")

        fd_ret, fd_list = self._proxy.call_with_unix_fd_list_sync(
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self._session_handle, {})),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
            None,
        )
        fd = fd_list.get(fd_ret.unpack()[0])
        return streams, fd

    # -------------------------------------------------------------- pipelines
    def _match_hint(self, position, size, candidates=None, *, size_only=True):
        """Prefer full logical geometry; only use unique fallback matches."""
        candidates = self._hints if candidates is None else candidates
        def close_pair(a, b):
            return a is not None and b is not None and all(
                abs(x - y) <= 4 for x, y in zip(a, b)
            )
        exact, scaled, sizes = [], [], []
        for hint in candidates:
            origin = (hint["left"], hint["top"])
            logical = (hint["width"], hint["height"])
            scale = hint.get("scale", 1.0) or 1.0
            physical = tuple(v * scale for v in logical)
            if close_pair(position, origin) and close_pair(size, logical):
                exact.append(hint)
            elif close_pair(position, origin) and close_pair(size, physical):
                scaled.append(hint)
            if close_pair(size, logical) or close_pair(size, physical):
                sizes.append(hint)
        for matches in (exact, scaled):
            if matches:
                return matches[0] if len(matches) == 1 else None
        return sizes[0] if size_only and len(sizes) == 1 else None

    def _match_streams(self, streams):
        # Reserve all full matches before a size fallback can consume a hint.
        remaining = list(self._hints)
        matches = [None] * len(streams)
        for size_only in (False, True):
            for idx, (_, props) in enumerate(streams):
                if matches[idx] is not None:
                    continue
                hint = self._match_hint(props.get("position"), props.get("size"),
                                        remaining, size_only=size_only)
                if hint is not None:
                    matches[idx] = hint
                    remaining = [h for h in remaining if h is not hint]
        if self._hints and (any(h is None for h in matches) or remaining):
            raise ScreenCastError("Select all monitors; portal geometry cannot be mapped unambiguously")
        return matches

    def _build_pipelines(self, streams, fd) -> None:
        Gst = self._Gst
        hints = self._match_streams(streams)
        for stream_index, ((node_id, props), hint) in enumerate(zip(streams, hints)):
            self._check_cancelled()
            position = props.get("position", (0, 0))
            size = props.get("size", (0, 0))

            # Each pipeline gets its own dup of the PipeWire fd. GStreamer's
            # ownership of it once handed to pipewiresrc isn't guaranteed, so
            # it's tracked here and explicitly closed in stop() rather than
            # relying on that -- otherwise every start/stop cycle can leak one
            # fd per monitor.
            dup_fd = os.dup(fd)
            self._dup_fds.append(dup_fd)
            desc = (
                f"pipewiresrc fd={dup_fd} path={node_id} do-timestamp=false max-buffers=4 "
                f"keepalive-time=1000 ! videoconvert ! video/x-raw,format=RGB ! "
                f"appsink name=sink emit-signals=false max-buffers=1 drop=true sync=false"
            )
            pipeline = Gst.parse_launch(desc)
            self._pipelines.append(pipeline)
            sink = pipeline.get_by_name("sink")
            ret = pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                raise ScreenCastError(f"failed to start pipeline for node {node_id}")

            if hint is not None:
                left, top = hint["left"], hint["top"]
                lw, lh = hint["width"], hint["height"]
                scale = hint.get("scale", 1.0) or 1.0
            else:
                # No logical hint: treat pixel geometry as logical (scale 1).
                left, top = position
                lw, lh = size
                scale = 1.0

            if lw <= 0 or lh <= 0 or sink is None:
                raise ScreenCastError("missing monitor geometry or pipeline sink")
            self._sinks.append(sink)
            self.monitors.append(
                {
                    "stream_index": stream_index,
                    "left": int(left),
                    "top": int(top),
                    "width": int(lw),
                    "height": int(lh),
                    "scale": float(scale),
                }
            )

        self._start_readers()

    def _start_readers(self):
        # Exactly one sample consumer per stream. grab() never drains appsink.
        for idx, sink in enumerate(self._sinks):
            self._history[idx] = deque()
            reader = threading.Thread(target=self._read_samples, args=(idx, sink),
                                      name=f"crec-pipewire-{idx}", daemon=True)
            self._readers.append(reader)
            reader.start()
        deadline = time.monotonic() + 5
        with self._condition:
            while len(self._latest) < len(self._sinks):
                self._check_cancelled()
                if self._stream_errors:
                    raise next(iter(self._stream_errors.values()))
                if time.monotonic() >= deadline:
                    raise ScreenCastError("no initial frame from ScreenCast stream")
                self._condition.wait(0.1)

    def _read_samples(self, idx, sink):
        Gst = self._Gst
        bus = self._pipelines[idx].get_bus()
        try:
            while not self._reader_stop.is_set():
                message = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if message is not None:
                    if message.type == Gst.MessageType.ERROR:
                        error, _ = message.parse_error()
                        raise ScreenCastError(f"ScreenCast stream {idx} failed: {error}")
                    raise ScreenCastError(f"ScreenCast stream {idx} ended")
                sample = sink.emit("try-pull-sample", Gst.SECOND // 10)
                if sample is None:
                    continue  # An unchanged desktop need not submit new frames.
                with self._decode_lock:
                    decoded = self._decode_sample(sample, self._sample_time(idx, sample))
                    sequence = sample.get_buffer().offset
                    if sequence == Gst.BUFFER_OFFSET_NONE:
                        raise ScreenCastError("ScreenCast frame lacks a sequence number for freshness checking")
                    repeated = sequence == self._last_sequence.get(idx)
                    self._last_sequence[idx] = sequence
                    frame = self._store_frame(replace(decoded, repeated=repeated))
                    del decoded
                self._publish_frame(idx, frame)
        except Exception as exc:
            with self._condition:
                self._stream_errors[idx] = (exc if isinstance(exc, ScreenCastError)
                                           else ScreenCastError(f"ScreenCast sample failed: {exc}"))
                self._condition.notify_all()

    def _store_frame(self, frame):
        if self._cleanup_thread is None:
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_files, name="crec-frame-cleanup", daemon=True)
            self._cleanup_thread.start()
        if self._spool is None:
            # Screen supplies its output directory, avoiding Fedora's tmpfs /tmp.
            directory = self._spool_directory or os.path.expanduser("~/.cache/crec")
            os.makedirs(directory, exist_ok=True)
            self._spool = tempfile.TemporaryDirectory(prefix=".crec-frames-", dir=directory)
        fd, path = tempfile.mkstemp(dir=self._spool.name)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(zlib.compress(frame.rgb, 1))
            # weakref finalization only queues a string. All filesystem work is
            # performed by the dedicated cleanup thread.
            return StoredFrame(frame, path, self._retire_frame)
        except BaseException:
            StoredFrame._unlink(path)
            raise

    def _sample_time(self, idx, sample):
        """Map segment PTS -> pipeline running time -> CLOCK_MONOTONIC.

        Decoding/disk/reader delays do not change the presentation timestamp.
        Reject missing timestamps rather than label old pixels as fresh.
        """
        Gst = self._Gst
        pts = sample.get_buffer().pts
        running = sample.get_segment().to_running_time(Gst.Format.TIME, pts)
        pipeline = self._pipelines[idx]
        clock = pipeline.get_clock()
        if pts == Gst.CLOCK_TIME_NONE or running == Gst.CLOCK_TIME_NONE or clock is None:
            raise ScreenCastError("ScreenCast frame has no usable presentation timestamp")
        start = time.monotonic()
        clock_now = clock.get_time()
        end = time.monotonic()
        return (start + end) / 2 + (pipeline.get_base_time() + running - clock_now) / Gst.SECOND

    def _publish_frame(self, idx, frame):
        with self._condition:
            self._latest[idx] = frame
            self._last_sample[idx] = time.monotonic()
            history = self._history.setdefault(idx, deque())
            history.append(frame)
            # Only disk handles are retained, including by pending contexts.
            while len(history) > 120 or (len(history) > 2 and
                    history[1].captured_at < frame.captured_at - 2):
                history.popleft()
            for ticket in list(self._pending_frames.get(idx, ())):
                if ticket.target is not None and self._is_after(frame, ticket.target):
                    ticket.after = frame
                    self._pending_frames[idx].discard(ticket)
            self._condition.notify_all()

    @staticmethod
    def _is_after(frame, at):
        # ``at`` already includes the completion/render settling interval.
        # An unchanged keepalive is valid after that target: not every action
        # changes pixels, and its delivery keeps the wait bounded.
        return frame.captured_at >= at

    def check_health(self):
        with self._condition:
            if self._cleanup_error is not None:
                raise ScreenCastError(
                    f"Temporary screenshot cleanup failed: {self._cleanup_error}")
            # pipewiresrc sends its last buffer every second when idle. This
            # watchdog checks delivery, while ERROR/EOS detects stream loss.
            # A keepalive is explicitly distinguished from a new producer frame.
            for idx, last in self._last_sample.items():
                if time.monotonic() - last > 5:
                    self._stream_errors.setdefault(idx, ScreenCastError(
                        f"ScreenCast stream {idx} stopped delivering frames"))
            if self._stream_errors:
                raise next(iter(self._stream_errors.values()))

    def _stream_index(self, monitor):
        idx = monitor["stream_index"]
        if not 0 <= idx < len(self.monitors) or monitor is not self.monitors[idx]:
            raise ScreenCastError("monitor does not belong to this capture")
        return idx

    def grab(self, monitor: dict) -> StoredFrame:
        """Return the retained latest frame, including when the screen is quiet."""
        idx = self._stream_index(monitor)
        with self._condition:
            if idx in self._stream_errors:
                raise self._stream_errors[idx]
            if idx not in self._latest:
                raise ScreenCastError("no frame available from ScreenCast stream")
            return self._latest[idx]

    def event_frames(self, monitor, event_at):
        """Keep monitor contexts independent, including during stream failure."""
        idx = self._stream_index(monitor)
        with self._condition:
            history = self._history.get(idx, ())
            before = next((f for f in reversed(history) if f.captured_at <= event_at), None)
            ticket = EventFrames(self, idx, event_at, before, None)
            self._pending_frames.setdefault(idx, weakref.WeakSet()).add(ticket)
            return ticket

    def _decode_sample(self, sample, captured_at):
        Gst = self._Gst
        buf = sample.get_buffer()
        caps = sample.get_caps().get_structure(0)
        width = caps.get_value("width")
        height = caps.get_value("height")
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            raise ScreenCastError("failed to map ScreenCast buffer")
        try:
            # RGB is 3 bytes/px but GStreamer rows are padded to a multiple of 4.
            stride = (width * 3 + 3) & ~3
            data = bytes(mapinfo.data)
            if stride == width * 3:
                rgb = data[: width * height * 3]
            else:
                rgb = b"".join(
                    data[r * stride : r * stride + width * 3] for r in range(height)
                )
        finally:
            buf.unmap(mapinfo)
        return Frame(width, height, rgb, captured_at)

    # ------------------------------------------------------------------- stop
    def stop(self) -> None:
        self._reader_stop.set()
        for reader in self._readers:
            reader.join()
        self._readers.clear()
        with self._condition:
            self._latest.clear()
            self._history.clear()
            self._pending_frames.clear()
            self._condition.notify_all()
        # Releasing history/latest queues retirements. Drain those removals
        # before removing the private spool directory and joining the worker.
        self._retired.join()
        if self._cleanup_thread is not None:
            with self._retire_lock:
                self._accept_retirements = False
                self._retired.put(None)
            self._retired.join()
            self._cleanup_thread.join()
            self._cleanup_thread = None
        if self._spool is not None:
            self._spool.cleanup()
            self._spool = None
        Gst = self._Gst
        for pipeline in self._pipelines:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
        self._pipelines.clear()
        self._sinks.clear()
        for dup_fd in self._dup_fds:
            try:
                os.close(dup_fd)
            except OSError:
                pass  # already closed by GStreamer, or invalid
        self._dup_fds.clear()
        if self._session_handle and self._bus is not None:
            try:
                Gio, _ = self._gi
                self._bus.call_sync(
                    _PORTAL,
                    self._session_handle,
                    "org.freedesktop.portal.Session",
                    "Close",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )
            except Exception:
                pass
            self._session_handle = ""
        if self._loop is not None:
            try:
                # Queue quit as well: quit() before run() starts is ineffective.
                _, GLib = self._gi
                loop = self._loop
                source = GLib.idle_source_new()
                source.set_callback(lambda *_: (loop.quit(), False)[1])
                source.attach(self._context)
                loop.quit()
            except Exception:
                pass
            self._loop = None
        if self._loop_thread is not None:
            self._loop_thread.join()
            self._loop_thread = None
        self.monitors.clear()

    # context-manager sugar so call sites can mirror ``with mss.mss() as sct``
    def __enter__(self) -> "PortalScreenCast":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
