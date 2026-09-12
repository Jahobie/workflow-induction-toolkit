from __future__ import annotations
###############################################################################
# Imports                                                                     #
###############################################################################

# — Standard library —
import base64
import gc
import logging
import os
import time
from urllib.parse import quote
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files as get_package_file
from typing import Any, Dict, Iterable, List, Optional

import asyncio
from functools import partial

import sys

# Platform detection.  The macOS recorder relies on Quartz (window/display
# queries), mss (screen grab) and pynput (input) — none of which work under
# Wayland.  On Linux we swap in the backends under ``crec/linux`` instead, so
# these macOS-only libraries are imported lazily and never touched on Linux.
_IS_MAC = sys.platform == "darwin"
_IS_LINUX = sys.platform.startswith("linux")

# — Third-party —
from PIL import Image, ImageDraw

if _IS_MAC:
    import mss
    import Quartz
    from pynput import mouse, keyboard           # still synchronous
    from shapely.geometry import box
    from shapely.ops import unary_union

# — Local —
from .observer import Observer
from ..schemas import Update

# Exception types the Linux capture/input backends raise on a missing
# permission or plugin, caught in ``_worker`` so the observer degrades
# gracefully instead of crashing.  These modules have no top-level imports of
# Linux-only system libraries, so importing the exception classes is always
# safe; only their actual use (inside functions) is platform-gated.
if _IS_LINUX:
    from ..linux import InputUnavailable, ScreenCastError
else:
    class InputUnavailable(Exception):
        """Unused on this platform; the Linux evdev backend never runs here."""

    class ScreenCastError(Exception):
        """Unused on this platform; the Linux ScreenCast backend never runs here."""

# — OpenAI async client —
# from openai import AsyncOpenAI
# client = AsyncOpenAI()

# — Google Drive (optional; the integration below is disabled by default) —
try:
    from pydrive.auth import GoogleAuth
    from pydrive.drive import GoogleDrive
except ImportError:  # PyDrive is optional and unused unless GDrive upload is on
    GoogleAuth = GoogleDrive = None

def initialize_google_drive(client_secrets_path: str = None) -> GoogleDrive:
    """
    Initialize Google Drive authentication with optional custom client_secrets.json path.
    
    Parameters
    ----------
    client_secrets_path : str, optional
        Path to the client_secrets.json file. If None, uses default location.
        
    Returns
    -------
    GoogleDrive
        Authenticated Google Drive client
    """
    gauth = GoogleAuth()
    
    if client_secrets_path:
        # Expand user path and get absolute path
        client_secrets_path = os.path.abspath(os.path.expanduser(client_secrets_path))
        
        # Verify the file exists
        if not os.path.exists(client_secrets_path):
            raise FileNotFoundError(f"Client secrets file not found: {client_secrets_path}")
        
        # Copy the client_secrets.json to current directory temporarily
        import shutil
        temp_client_secrets = "client_secrets.json"
        
        try:
            shutil.copy2(client_secrets_path, temp_client_secrets)
            print(f"✅ Copied client_secrets.json to current directory")
            
            # Use default behavior (PyDrive will find client_secrets.json in current directory)
            gauth.LocalWebserverAuth()  # Opens browser for first-time authentication
            
        finally:
            # Clean up temporary file
            try:
                os.remove(temp_client_secrets)
                print(f"✅ Cleaned up temporary client_secrets.json")
            except OSError:
                pass  # File might already be deleted
    else:
        # Use default behavior (looks for client_secrets.json in current directory)
        gauth.LocalWebserverAuth()  # Opens browser for first-time authentication
    
    return GoogleDrive(gauth)

# Initialize with default behavior (looks for client_secrets.json in current directory)
# drive = initialize_google_drive()

def list_folders(drive: GoogleDrive):
    """List all folders in Google Drive to help find folder IDs"""
    folders = drive.ListFile({'q': "mimeType='application/vnd.google-apps.folder' and trashed=false"}).GetList()
    print("Available folders:")
    for folder in folders:
        print(f"Name: {folder['title']}, ID: {folder['id']}")
    return folders

def find_folder_by_name(folder_name: str, drive: GoogleDrive):
    """Find a folder by name and return its ID"""
    folders = drive.ListFile({'q': f"mimeType='application/vnd.google-apps.folder' and title='{folder_name}' and trashed=false"}).GetList()
    if folders:
        return folders[0]['id']
    return None

def upload_file(path: str, drive_dir: str, drive_instance: GoogleDrive):
    """Upload a file to Google Drive and delete the local file.
    
    Parameters
    ----------
    path : str
        Path to the file to upload
    drive_dir : str
        Google Drive folder ID to upload to
    drive_instance : GoogleDrive
        Google Drive client instance.
    """
    upload_file = drive_instance.CreateFile({
        'title': path.split('/')[-1],
        'parents': [{'id': drive_dir}]
    })
    upload_file.SetContentFile(path)
    upload_file.Upload()
    os.remove(path)

###############################################################################
# Window‑geometry helpers                                                     #
###############################################################################


def _get_global_bounds() -> tuple[float, float, float, float]:
    """Return a bounding box enclosing **all** physical displays.

    Returns
    -------
    (min_x, min_y, max_x, max_y) tuple in Quartz global coordinates.
    """
    err, ids, cnt = Quartz.CGGetActiveDisplayList(16, None, None)
    if err != Quartz.kCGErrorSuccess:  # pragma: no cover (defensive)
        raise OSError(f"CGGetActiveDisplayList failed: {err}")

    min_x = min_y = float("inf")
    max_x = max_y = -float("inf")
    for did in ids[:cnt]:
        r = Quartz.CGDisplayBounds(did)
        x0, y0 = r.origin.x, r.origin.y
        x1, y1 = x0 + r.size.width, y0 + r.size.height
        min_x, min_y = min(min_x, x0), min(min_y, y0)
        max_x, max_y = max(max_x, x1), max(max_y, y1)
    return min_x, min_y, max_x, max_y


def _get_visible_windows() -> List[tuple[dict, float]]:
    """List *onscreen* windows with their visible‑area ratio.

    Each tuple is ``(window_info_dict, visible_ratio)`` where *visible_ratio*
    is in ``[0.0, 1.0]``.  Internal system windows (Dock, WindowServer, …) are
    ignored.
    """
    _, _, _, gmax_y = _get_global_bounds()

    opts = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListOptionIncludingWindow
    )
    wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)

    occupied = None  # running union of opaque regions above the current window
    result: list[tuple[dict, float]] = []

    for info in wins:
        owner = info.get("kCGWindowOwnerName", "")
        if owner in ("Dock", "WindowServer", "Window Server"):
            continue

        bounds = info.get("kCGWindowBounds", {})
        x, y, w, h = (
            bounds.get("X", 0),
            bounds.get("Y", 0),
            bounds.get("Width", 0),
            bounds.get("Height", 0),
        )
        if w <= 0 or h <= 0:
            continue  # hidden or minimised

        inv_y = gmax_y - y - h  # Quartz→Shapely Y‑flip
        poly = box(x, inv_y, x + w, inv_y + h)
        if poly.is_empty:
            continue

        visible = poly if occupied is None else poly.difference(occupied)
        if not visible.is_empty:
            ratio = visible.area / poly.area
            result.append((info, ratio))
            occupied = poly if occupied is None else unary_union([occupied, poly])

    return result


def _is_app_visible(names: Iterable[str]) -> bool:
    """Return *True* if **any** window from *names* is at least partially visible."""
    targets = set(names)
    return any(
        info.get("kCGWindowOwnerName", "") in targets and ratio > 0
        for info, ratio in _get_visible_windows()
    )

###############################################################################
# Input backend (macOS)                                                       #
###############################################################################

class _PynputInput:
    """macOS input backend wrapping pynput behind the same interface as the
    Wayland ``EvdevInput`` backend, so ``Screen._worker`` is platform-neutral.

    pynput fires its callbacks from its own threads, so they are marshalled
    onto the asyncio loop with ``run_coroutine_threadsafe``.
    """

    def __init__(self) -> None:
        self._loop = None
        self._mouse_listener = None
        self._key_listener = None

    def _sched(self, coro):
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def start(self, on_click, on_scroll, on_key) -> None:
        # ``async`` for interface parity with EvdevInput; starting pynput's
        # listeners is cheap (it manages its own threads) so no to_thread here.
        self._loop = asyncio.get_running_loop()
        self._mouse_listener = mouse.Listener(
            on_click=lambda x, y, btn, prs: (
                self._sched(on_click(x, y, f"click_{btn.name}")) if prs else None
            ),
            on_scroll=lambda x, y, dx, dy: self._sched(on_scroll(x, y, dx, dy)),
        )
        self._key_listener = keyboard.Listener(
            on_press=lambda key: self._sched(on_key(str(key))),
        )
        self._mouse_listener.start()
        self._key_listener.start()

    async def pointer_position(self) -> tuple[float, float]:
        # ``async`` for interface parity with EvdevInput (whose equivalent does
        # a blocking D-Bus call and must run off-loop); pynput's own call is a
        # cheap local read, so no threading is needed here.
        return mouse.Controller().position

    async def stop(self) -> None:
        if self._mouse_listener is not None:
            self._mouse_listener.stop()
        if self._key_listener is not None:
            self._key_listener.stop()


###############################################################################
# Screen observer                                                             #
###############################################################################

class Screen(Observer):
    """
    Capture before/after screenshots around user interactions.
    Blocking work (Quartz, mss, Pillow, OpenAI Vision) is executed in
    background threads via `asyncio.to_thread`.
    
    Keyboard events are optimized to save disk space:
    - Only the first and last screenshots are kept for consecutive key presses
    - Intermediate screenshots are automatically deleted
    - A keyboard session ends after `keyboard_timeout` seconds of inactivity
    """

    _CAPTURE_FPS: int = 5  # Reduced from 10 to 5 to reduce memory pressure
    _PERIODIC_SEC: int = 30
    _DEBOUNCE_SEC: int = 1
    _MON_START: int = 1     # first real display in mss
    _MEMORY_CLEANUP_INTERVAL: int = 30  # Force GC every 30 frames instead of 50
    _MAX_WORKERS: int = 4  # Limit thread pool size to prevent exhaustion
    
    # Scroll filtering constants
    _SCROLL_DEBOUNCE_SEC: float = 0.8  # Minimum time between scroll events
    _SCROLL_MIN_DISTANCE: float = 8.0  # Minimum scroll distance to log
    _SCROLL_MAX_FREQUENCY: int = 8  # Max scroll events per second
    _SCROLL_SESSION_TIMEOUT: float = 3.0  # Timeout for scroll sessions

    # ─────────────────────────────── construction
    def __init__(
        self,
        screenshots_dir: str = "~/Downloads/records/screenshots",
        skip_when_visible: Optional[str | list[str]] = None,
        history_k: int = 10,
        debug: bool = False,
        keyboard_timeout: float = 2.0,
        gdrive_dir: str = "screenshots",
        client_secrets_path: str = "~/Desktop/client_secrets.json",
        scroll_debounce_sec: float = 0.5,
        scroll_min_distance: float = 5.0,
        scroll_max_frequency: int = 10,
        scroll_session_timeout: float = 2.0,
    ) -> None:

        self.screens_dir = os.path.abspath(os.path.expanduser(screenshots_dir))
        os.makedirs(self.screens_dir, exist_ok=True)

        # Platform backends.  On macOS ``sct.monitors[0]`` is the union of all
        # displays, so the real monitors start at index 1; the Wayland capture
        # backend lists only real monitors, so it starts at 0.
        self._mon_slice = 1 if _IS_MAC else 0
        self._bridge = None
        self._input_backend = None
        self.failure = None

        self._guard = {skip_when_visible} if isinstance(skip_when_visible, str) else set(skip_when_visible or [])

        self.debug = debug

        # Custom thread pool to prevent exhaustion
        self._thread_pool = ThreadPoolExecutor(max_workers=self._MAX_WORKERS)

        # Scroll filtering configuration
        self._scroll_debounce_sec = scroll_debounce_sec
        self._scroll_min_distance = scroll_min_distance
        self._scroll_max_frequency = scroll_max_frequency
        self._scroll_session_timeout = scroll_session_timeout

        # state shared with worker
        self._frames: Dict[int, Any] = {}
        self._frame_lock = asyncio.Lock()

        self._history: deque[str] = deque(maxlen=max(0, history_k))
        self._pending_event: Optional[dict] = None
        self._debounce_handle: Optional[asyncio.TimerHandle] = None

        # keyboard activity tracking
        self._key_activity_start: Optional[float] = None
        self._key_activity_timeout: float = keyboard_timeout  # seconds of inactivity to consider session ended
        self._key_screenshots: List[str] = []  # track intermediate screenshots for cleanup
        self._key_activity_lock = asyncio.Lock()

        # scroll activity tracking
        self._scroll_last_time: Optional[float] = None
        self._scroll_last_position: Optional[tuple[float, float]] = None
        self._scroll_session_start: Optional[float] = None
        self._scroll_event_count: int = 0
        self._scroll_lock = asyncio.Lock()

        # Initialize Google Drive with custom client_secrets path if provided
        # self.drive = initialize_google_drive(client_secrets_path)
        # self.gdrive_dir = find_folder_by_name(gdrive_dir, self.drive)

        # call parent
        super().__init__()

        # Adjust settings for high-DPI displays
        if _IS_MAC and self._detect_high_dpi():
            self._CAPTURE_FPS = 3  # Even lower FPS for high-DPI displays
            self._MEMORY_CLEANUP_INTERVAL = 20  # More frequent cleanup
            if self.debug:
                logging.getLogger("crec.screen").info("High-DPI display detected, using conservative settings")

    @staticmethod
    def _mon_for(x: float, y: float, mons: list[dict]) -> Optional[int]:
        if x is None or y is None:
            return None
        for idx, m in enumerate(mons, 1):
            if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
                return idx
        return None

    async def _run_in_thread(self, func, *args, **kwargs):
        """Run a function in the custom thread pool."""
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._thread_pool, lambda: func(*args, **kwargs))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Executor cancellation does not stop its thread. Join in-flight
            # grabs/saves before tearing down resources used by that thread.
            try:
                await future
            except Exception:
                pass
            raise

    def _detect_high_dpi(self) -> bool:
        """Detect if running on a high-DPI display and adjust settings."""
        try:
            if _IS_LINUX:
                mons = self._bridge.get_monitors() if self._bridge else None
                if mons:
                    return any(
                        m.get("scale", 1.0) > 1.0
                        or m["width"] > 2560
                        or m["height"] > 1600
                        for m in mons
                    )
                return False
            # Check if any monitor has high resolution (likely Retina)
            with mss.mss() as sct:
                for monitor in sct.monitors[1:]:  # Skip monitor 0 (all monitors)
                    if monitor['width'] > 2560 or monitor['height'] > 1600:
                        return True
        except Exception:
            pass
        return False

    def _make_capture(self):
        """Return a screen-capture backend exposing ``.monitors`` and ``.grab``."""
        if _IS_LINUX:
            from ..linux import PortalScreenCast
            hints = self._bridge.get_monitors() if self._bridge else None
            if not hints:
                raise ScreenCastError("GNOME monitor geometry is unavailable")
            return PortalScreenCast(monitor_hints=hints, spool_directory=self.screens_dir)
        return mss.mss()

    def _make_input(self):
        """Return an input backend exposing ``start``/``stop``/``pointer_position``."""
        if _IS_LINUX:
            from ..linux import EvdevInput
            return EvdevInput(bridge=self._bridge)
        return _PynputInput()

    def _should_log_scroll(self, x: float, y: float, dx: float, dy: float) -> bool:
        """
        Determine if a scroll event should be logged based on filtering criteria.
        
        Returns True if the scroll event should be logged, False otherwise.
        """
        current_time = time.time()
        
        # Check if this is a new scroll session
        if (self._scroll_session_start is None or 
            current_time - self._scroll_session_start > self._scroll_session_timeout):
            # Start new session
            self._scroll_session_start = current_time
            self._scroll_event_count = 0
            self._scroll_last_position = (x, y)
            self._scroll_last_time = current_time
            return True
        
        # Check debounce time
        if (self._scroll_last_time is not None and 
            current_time - self._scroll_last_time < self._scroll_debounce_sec):
            return False
        
        # Check minimum distance
        if self._scroll_last_position is not None:
            distance = ((x - self._scroll_last_position[0]) ** 2 + 
                       (y - self._scroll_last_position[1]) ** 2) ** 0.5
            if distance < self._scroll_min_distance:
                return False
        
        # Check frequency limit
        self._scroll_event_count += 1
        session_duration = current_time - self._scroll_session_start
        if session_duration > 0:
            frequency = self._scroll_event_count / session_duration
            if frequency > self._scroll_max_frequency:
                return False
        
        # Update tracking state
        self._scroll_last_position = (x, y)
        self._scroll_last_time = current_time
        
        return True

    async def _cleanup_key_screenshots(self) -> None:
        """Clean up intermediate keyboard screenshots, keeping only first and last."""
        if len(self._key_screenshots) <= 2:
            return
        
        # Keep first and last, delete the rest
        to_delete = self._key_screenshots[1:-1]
        self._key_screenshots = [self._key_screenshots[0], self._key_screenshots[-1]]
        
        for path in to_delete:
            try:
                await self._run_in_thread(os.remove, path)
                if self.debug:
                    logging.getLogger("crec.screen").info(f"Deleted intermediate screenshot: {path}")
            except OSError:
                pass  # File might already be deleted

    # ─────────────────────────────── I/O helpers
    async def _save_frame(self, frame, x, y, tag: str, box_color: str = "red", box_width: int = 10, scale: float = 1.0, timestamp: float | None = None, mark_pointer: bool = True) -> str:
        ts = f"{time.time() if timestamp is None else timestamp:.5f}"
        # A version marker distinguishes escaped names from legacy literal %xx.
        # The action in actions.db is always the original, unescaped token.
        if _IS_LINUX and "/" in tag:
            tag = "~crec1~" + quote(tag, safe=" ()',_=.-")
        path = os.path.join(self.screens_dir, f"{ts}_{tag}.jpg")
        def save():
            # Disk-backed Linux frames must be read and decoded off the event
            # loop so input acquisition can continue during slow storage.
            image = Image.frombytes("RGB", (frame.width, frame.height), frame.rgb)
            try:
                if mark_pointer:
                    draw = ImageDraw.Draw(image)
                    px, py = x * scale, y * scale
                    x1, x2 = max(0, px - 30), min(frame.width, px + 30)
                    y1, y2 = max(0, py - 20), min(frame.height, py + 20)
                    draw.rectangle([x1, y1, x2, y2], outline=box_color, width=box_width)
                image.save(path, "JPEG", quality=70, optimize=True)
            finally:
                image.close()
        await self._run_in_thread(save)

        # upload to google drive and delete local file
        # if self.gdrive_dir is not None:
        #     await asyncio.to_thread(upload_file, path, self.gdrive_dir, self.drive)
        return path

    async def _process_and_emit(
        self, before_path: str, after_path: str | None, 
        action: str | None, ev: dict | None,
    ) -> None:
        if "scroll" in action:
            # Include scroll delta information
            scroll_info = ev.get("scroll", (0, 0))
            step = f"scroll({ev['position'][0]:.1f}, {ev['position'][1]:.1f}, dx={scroll_info[0]:.2f}, dy={scroll_info[1]:.2f})"
            await self.update_queue.put(Update(content=step, content_type="input_text"))
        elif "click" in action:
            step = f"{action}({ev['position'][0]:.1f}, {ev['position'][1]:.1f})"
            await self.update_queue.put(Update(content=step, content_type="input_text"))
        else:
            step = f"{action}({ev['text']})"
            await self.update_queue.put(Update(content=step, content_type="input_text"))

    async def stop(self) -> None:
        """Stop the observer and clean up resources."""
        await super().stop()

        # Stop the input backend explicitly: its reader tasks (evdev) live
        # outside the worker task, so cancelling the worker does not reach them.
        if self._input_backend is not None:
            try:
                await self._input_backend.stop()
            except Exception:
                pass

        # Clean up frame objects
        async with self._frame_lock:
            for frame in self._frames.values():
                if frame is not None:
                    del frame
            self._frames.clear()
        
        # Force garbage collection
        await self._run_in_thread(gc.collect)
        
        # Shutdown thread pool
        if hasattr(self, '_thread_pool'):
            self._thread_pool.shutdown(wait=True)

    # ─────────────────────────────── skip guard
    def _skip(self) -> bool:
        if not self._guard:
            return False
        if _IS_MAC:
            return _is_app_visible(self._guard)
        # Wayland has no cross-desktop "which window is visible" query, so the
        # skip-when-visible guard is a no-op here (it is unused by the CLI).
        return False

    # ─────────────────────────────── main async worker
    async def _worker(self) -> None:
        log = logging.getLogger("crec.screen")
        sct = None
        try:
            if _IS_LINUX:
                from ..linux import ShellBridge
                self._bridge = await self._run_in_thread(ShellBridge)
                if not self._bridge.available:
                    raise InputUnavailable("Enable the crec-input GNOME Shell extension before recording")
                await self._run_in_thread(self._bridge.verify_protocol)
                if await self._run_in_thread(self._detect_high_dpi):
                    self._CAPTURE_FPS = 3
                    self._MEMORY_CLEANUP_INTERVAL = 20
                sct = await self._run_in_thread(self._make_capture)
                startup = asyncio.create_task(self._run_in_thread(sct.start))
                try:
                    await asyncio.shield(startup)
                except asyncio.CancelledError:
                    sct.cancel_start()
                    await asyncio.gather(startup, return_exceptions=True)
                    raise
            else:
                sct = self._make_capture()
            await self._capture_worker(sct)
        except (InputUnavailable, ScreenCastError, ImportError) as exc:
            self.failure = exc
            log.error("Screen recording unavailable: %s", exc)
            # Keep the failure visible in the ordinary observations stream.
            await self.update_queue.put(Update(
                content=f"recorder_error({exc})", content_type="input_text"))
        finally:
            try:
                if self._input_backend is not None:
                    await self._input_backend.stop()
            finally:
                if sct is not None:
                    if _IS_LINUX:
                        await self._run_in_thread(sct.stop)
                    else:
                        sct.close()

    async def _capture_worker(self, sct) -> None:
        log = logging.getLogger("crec.screen")
        CAP_FPS = self._CAPTURE_FPS
        try:
            mons = sct.monitors[self._mon_slice:]

            # ---- nested helper inside the async context ----
            async def flush():
                if self._pending_event is None:
                    return
                if self._skip():
                    self._pending_event = None
                    return

                ev = self._pending_event
                try:
                    if ev.get("event") is not None:
                        pair = ev["event"]["frames"].get(ev["mon"])
                        aft = (await self._run_in_thread(pair.resolve_after)
                               if hasattr(pair, "resolve_after") else pair[1] if pair else None)
                        if aft is None:
                            raise ScreenCastError("No event-associated after frame available")
                    else:
                        aft = await self._run_in_thread(sct.grab, mons[ev["mon"] - 1])
                except Exception as e:
                    logging.getLogger("crec.screen").error(f"Failed to capture after frame: {e}")
                    self._pending_event = None
                    if _IS_LINUX:
                        # Preserve the available before image even when the
                        # stream disappears before the after image arrives.
                        mon = mons[ev["mon"] - 1]
                        x, y = ev["position"]
                        step = (f"scroll({x:.1f}, {y:.1f}, dx={ev['scroll'][0]:.2f}, dy={ev['scroll'][1]:.2f})"
                                if ev["type"] == "scroll" else f"{ev['type']}({x:.1f}, {y:.1f})")
                        await self._save_frame(ev["before"], x, y, f"{step}_before",
                                              scale=ev["before"].width / mon["width"],
                                              timestamp=ev["event"]["wall_time"])
                        raise
                    return

                if "scroll" in ev["type"]:
                    scroll_info = ev.get("scroll", (0, 0))
                    step = f"scroll({ev['position'][0]:.1f}, {ev['position'][1]:.1f}, dx={scroll_info[0]:.2f}, dy={scroll_info[1]:.2f})"
                else:
                    step = f"{ev['type']}({ev['position'][0]:.1f}, {ev['position'][1]:.1f})"
                
                mon = mons[ev["mon"] - 1]
                bef_scale = ev["before"].width / mon["width"]
                aft_scale = aft.width / mon["width"]
                stamp = ev["event"]["wall_time"] if ev.get("event") else None
                bef_path = await self._save_frame(ev["before"], ev["position"][0], ev["position"][1], f"{step}_before", scale=bef_scale, timestamp=stamp)
                aft_path = await self._save_frame(aft, ev["position"][0], ev["position"][1], f"{step}_after", scale=aft_scale, timestamp=stamp)
                if not _IS_LINUX:
                    await self._process_and_emit(bef_path, aft_path, ev["type"], ev)

                log.info(f"{ev['type']} captured on monitor {ev['mon']}")
                self._pending_event = None

            # def debounce_flush():
            #     # callback from loop.call_later → must create task
            #     asyncio.create_task(flush())

            # ---- keyboard event reception ----
            # ``key`` is already the pynput ``str(key)`` token (e.g. "'a'" or
            # "Key.enter"); both backends deliver it in that form.
            async def key_event(key, typ: str = "press", *, event=None):
                step = f"key_{typ}({key})"
                # Retain the key even when no pointer position/frame is known.
                await self.update_queue.put(Update(content=step, content_type="input_text"))
                x, y = await self._input_backend.pointer_position()
                idx = self._mon_for(x, y, mons)
                if idx is None:
                    if _IS_LINUX and event is not None:
                        await preserve_unlocated_frames(step, event, include_after=False)
                        await report_incomplete(
                            "Key position is ambiguous; unmarked monitor screenshots were retained")
                        return
                    log.error("Key retained without screenshot: pointer/monitor unavailable")
                    return
                mon = mons[idx - 1]
                x, y = x - mon["left"], y - mon["top"]
                if event is not None:
                    frame = event["frames"].get(idx, (None, None))[0]
                else:
                    async with self._frame_lock:
                        frame = self._frames.get(idx)
                if frame is None:
                    if _IS_LINUX:
                        await report_incomplete(
                            "Key has no preceding screenshot; recording is incomplete")
                        return
                    raise InputUnavailable("Key has no preceding screenshot")
                stamp = event["wall_time"] if event else None
                scale = frame.width / mon["width"]

                async with self._key_activity_lock:
                    current_time = time.time()

                    # Check if this is the start of a new keyboard session
                    if (self._key_activity_start is None or
                        current_time - self._key_activity_start > self._key_activity_timeout):
                        # Start new session - save first screenshot
                        self._key_activity_start = current_time
                        self._key_screenshots = []
                        screenshot_path = await self._save_frame(frame, x, y, f"{step}_first", scale=scale, timestamp=stamp)
                        self._key_screenshots.append(screenshot_path)
                        log.info(f"Started new keyboard session, saved first screenshot: {screenshot_path}")
                    else:
                        # Continue existing session - save intermediate screenshot
                        screenshot_path = await self._save_frame(frame, x, y, f"{step}_intermediate", scale=scale, timestamp=stamp)
                        self._key_screenshots.append(screenshot_path)
                        log.info(f"Continued keyboard session, saved intermediate screenshot: {screenshot_path}")
                    
                    # Schedule cleanup of previous intermediate screenshots
                    if len(self._key_screenshots) > 2:
                        if _IS_LINUX:
                            await self._cleanup_key_screenshots()
                        else:
                            asyncio.create_task(self._cleanup_key_screenshots())

            async def retain_pointer_event(typ, x, y, dx=None, dy=None):
                if not _IS_LINUX:
                    return
                idx = self._mon_for(x, y, mons)
                if idx is None:
                    px = py = float("nan")
                    log.error("%s retained with unknown coordinates; pointer/monitor unavailable", typ)
                else:
                    mon = mons[idx - 1]
                    px, py = x - mon["left"], y - mon["top"]
                if typ == "scroll":
                    step = f"scroll({px:.1f}, {py:.1f}, dx={dx:.2f}, dy={dy:.2f})"
                else:
                    step = f"{typ}({px:.1f}, {py:.1f})"
                await self.update_queue.put(Update(content=step, content_type="input_text"))

            async def preserve_unlocated_frames(step, event, include_after):
                """Retain every possible monitor image before reporting ambiguity."""
                saved = 0
                for mon_idx, mon in enumerate(mons, 1):
                    pair = event["frames"].get(mon_idx)
                    before = pair[0] if pair else None
                    if before is not None:
                        await self._save_frame(
                            before, None, None, f"{step}_unlocated_mon{mon_idx}_before",
                            scale=before.width / mon["width"], timestamp=event["wall_time"],
                            mark_pointer=False)
                        saved += 1
                    if include_after and pair is not None:
                        try:
                            after = (await self._run_in_thread(pair.resolve_after)
                                     if hasattr(pair, "resolve_after") else pair[1])
                            if after is not None:
                                await self._save_frame(
                                    after, None, None, f"{step}_unlocated_mon{mon_idx}_after",
                                    scale=after.width / mon["width"],
                                    timestamp=event["wall_time"], mark_pointer=False)
                                saved += 1
                        except Exception as exc:
                            log.error("Cannot retain unlocated after image for monitor %s: %s",
                                      mon_idx, exc)
                if not saved:
                    log.error("No monitor frames were available for ambiguous action %s", step)

            async def report_incomplete(message):
                """Persist a local data gap while allowing later work to record."""
                log.error(message)
                await self.update_queue.put(Update(
                    content=f"recorder_incomplete({message})", content_type="input_text"))

            # ---- scroll event reception ----
            async def scroll_event(x: float, y: float, dx: float, dy: float, *, event=None):
                await retain_pointer_event("scroll", x, y, dx, dy)
                if not _IS_LINUX:
                    # Apply scroll filtering
                    async with self._scroll_lock:
                        if not self._should_log_scroll(x, y, dx, dy):
                            if self.debug:
                                log.info(f"Scroll filtered out: dx={dx:.2f}, dy={dy:.2f}")
                            return
                
                idx = self._mon_for(x, y, mons)
                if idx is None:
                    if _IS_LINUX and event is not None:
                        step = f"scroll(nan, nan, dx={dx:.2f}, dy={dy:.2f})"
                        await preserve_unlocated_frames(step, event, include_after=True)
                        await report_incomplete(
                            "Scroll position is ambiguous; unmarked monitor screenshots were retained")
                    return

                mon = mons[idx - 1]
                x = x - mon["left"]
                y = y - mon["top"]

                # Only log significant scroll movements
                scroll_magnitude = (dx**2 + dy**2)**0.5
                if not _IS_LINUX and scroll_magnitude < 1.0:  # Very small scrolls
                    if self.debug:
                        log.info(f"Scroll too small: magnitude={scroll_magnitude:.2f}")
                    return
                
                log.info(f"Scroll @({x:7.1f},{y:7.1f}) dx={dx:.2f} dy={dy:.2f} → mon={idx}")
                
                if self._skip():
                    return

                async with self._frame_lock:
                    bf = (event["frames"].get(idx, (None, None))[0]
                          if event is not None else self._frames.get(idx))
                    if bf is None:
                        if _IS_LINUX and event is not None:
                            await report_incomplete(
                                "Scroll has no preceding screenshot; recording is incomplete")
                            return
                        log.error("Action retained without screenshot: no frame preceding the event")
                        return
                    if _IS_LINUX:
                        # Throttle screenshots only; every raw action is already queued.
                        # Acquisition time keeps queued bursts independent of save speed.
                        at = event["at"] if event is not None else time.monotonic()
                        if (self._scroll_last_time is not None and
                                at - self._scroll_last_time < self._scroll_debounce_sec):
                            return
                        self._scroll_last_time = at

                    self._pending_event = {"type": "scroll", "position": (x, y), "mon": idx, "before": bf, "scroll": (dx, dy), "event": event}
                
                # Process event immediately
                await flush()

            # ---- mouse event reception ----
            async def mouse_event(x: float, y: float, typ: str, *, event=None):
                await retain_pointer_event(typ, x, y)
                if event is not None and event.get("incomplete"):
                    await report_incomplete(
                        f"{typ}: {event['incomplete']}")
                idx = self._mon_for(x, y, mons)
                if idx is None:
                    if _IS_LINUX and event is not None:
                        step = f"{typ}(nan, nan)"
                        await preserve_unlocated_frames(step, event, include_after=True)
                        await report_incomplete(
                            "Pointer position is ambiguous; unmarked monitor screenshots were retained")
                    return
                if self._skip():
                    return

                mon = mons[idx - 1]
                x = x - mon["left"]
                y = y - mon["top"]
                log.info(f"{typ:<6} @({x:7.1f},{y:7.1f}) → mon={idx}")

                async with self._frame_lock:
                    bf = (event["frames"].get(idx, (None, None))[0]
                          if event is not None else self._frames.get(idx))
                    if bf is None:
                        if _IS_LINUX and event is not None:
                            await report_incomplete(
                                "Pointer action has no preceding screenshot; recording is incomplete")
                            return
                        log.error("Action retained without screenshot: no frame preceding the event")
                        return
                    self._pending_event = {"type": typ, "position": (x, y), "mon": idx, "before": bf, "event": event}
                
                # Process event immediately instead of using debounce
                await flush()

            # Prime all frames before any input callbacks can run.
            for idx, mon in enumerate(mons, 1):
                self._frames[idx] = await self._run_in_thread(sct.grab, mon)

            # ---- start input backend (delivers the callbacks above) ----
            # Constructing EvdevInput loads an xkb keymap from disk (blocking
            # file I/O), so build it off-loop too, not just .start().
            self._input_backend = await self._run_in_thread(self._make_input)
            kwargs = {}
            if _IS_LINUX:
                kwargs["snapshot_source"] = lambda at: {
                    idx: sct.event_frames(mon, at) for idx, mon in enumerate(mons, 1)}
            await self._input_backend.start(
                on_click=mouse_event, on_scroll=scroll_event, on_key=key_event, **kwargs)

            # ---- main capture loop ----
            log.info(f"Screen observer started — guarding {self._guard or '∅'}")
            last_periodic = time.time()
            frame_count = 0

            while self._running:                         # flag from base class
                if _IS_LINUX:
                    self._input_backend.check_health()
                    sct.check_health()
                t0 = time.time()

                # refresh 'before' buffers
                for idx, m in enumerate(mons, 1):
                    old_frame = None
                    async with self._frame_lock:
                        old_frame = self._frames.get(idx)
                    
                    # Capture new frame using custom thread pool
                    try:
                        frame = await self._run_in_thread(sct.grab, m)
                    except Exception as e:
                        if _IS_LINUX:
                            raise
                        if self.debug:
                            logging.getLogger("crec.screen").error(f"Failed to capture frame: {e}")
                        continue
                    
                    async with self._frame_lock:
                        self._frames[idx] = frame
                    
                    # Explicitly delete old frame to free memory
                    if old_frame is not None:
                        del old_frame
                    
                    frame_count += 1
                    
                    # Force garbage collection every 30 frames to prevent memory buildup
                    if frame_count % self._MEMORY_CLEANUP_INTERVAL == 0:
                        await self._run_in_thread(gc.collect)

                # Check for keyboard session timeout
                current_time = time.time()
                if (self._key_activity_start is not None and 
                    current_time - self._key_activity_start > self._key_activity_timeout and
                    len(self._key_screenshots) > 1):
                    # Session ended - rename last screenshot to indicate it's the final one
                    async with self._key_activity_lock:
                        if len(self._key_screenshots) > 1:
                            last_path = self._key_screenshots[-1]
                            final_path = last_path.replace("_intermediate", "_final")
                            try:
                                await self._run_in_thread(os.rename, last_path, final_path)
                                self._key_screenshots[-1] = final_path
                                log.info(f"Keyboard session ended, renamed final screenshot: {final_path}")
                            except OSError:
                                pass
                        self._key_activity_start = None
                        self._key_screenshots = []

                # fps throttle
                dt = time.time() - t0
                await asyncio.sleep(max(0, (1 / CAP_FPS) - dt))

        finally:
            # Drain input while capture is alive, then finalize keyboard images.
            if self._input_backend is not None:
                await self._input_backend.stop()
            if _IS_LINUX and self._input_backend is not None:
                self._input_backend.check_health()
            # Final cleanup of any remaining keyboard session
            if self._key_activity_start is not None and len(self._key_screenshots) > 1:
                async with self._key_activity_lock:
                    last_path = self._key_screenshots[-1]
                    final_path = last_path.replace("_intermediate", "_final")
                    try:
                        await self._run_in_thread(os.rename, last_path, final_path)
                        log.info(f"Final keyboard session cleanup, renamed: {final_path}")
                    except OSError:
                        pass
                    await self._cleanup_key_screenshots()
