"""Kernel keyboard capture synchronized with GNOME, plus libinput gestures."""
from __future__ import annotations

import asyncio
import fcntl
import logging
import heapq
import platform
import struct
import time
from collections import deque

log = logging.getLogger("crec.linux.inputs")


class InputUnavailable(RuntimeError):
    """Faithful compositor input is unavailable or interrupted."""


_SPECIAL = {
    "Return": "enter",
    "KP_Enter": "enter",
    "BackSpace": "backspace",
    "space": "space",
    "Tab": "tab",
    "ISO_Left_Tab": "tab",
    "Escape": "esc",
    "Delete": "delete",
    "Left": "left",
    "Right": "right",
    "Up": "up",
    "Down": "down",
    "Home": "home",
    "End": "end",
    "Page_Up": "page_up",
    "Page_Down": "page_down",
    "Insert": "insert",
    "Caps_Lock": "caps_lock",
    "Num_Lock": "num_lock",
    "Scroll_Lock": "scroll_lock",
    "Pause": "pause",
    "Print": "print_screen",
    "Menu": "menu",
    "Shift_L": "shift",
    "Shift_R": "shift",
    "Control_L": "ctrl",
    "Control_R": "ctrl",
    "Alt_L": "alt",
    "Alt_R": "alt_gr",
    "ISO_Level3_Shift": "alt_gr",
    "Super_L": "cmd",
    "Super_R": "cmd",
    "Meta_L": "cmd",
    "Meta_R": "cmd",
}
for _n in range(1, 21):
    _SPECIAL[f"F{_n}"] = f"f{_n}"


def position_at(history, event_at, motion_times=(), event_order=None):
    """Use the immediate bracketing observations, not a 40 ms quiet window.

    A move before/after these samples does not invalidate the event position.
    A changed bracket or a motion still awaiting a compositor observation is
    ambiguous; do not interpolate an invented coordinate across that boundary.
    """
    before = next((row for row in reversed(history) if row[0] <= event_at), None)
    after = next((row for row in history if row[0] > event_at), None)
    if before is None or after is None:
        return None, None
    before_point, after_point = tuple(before[1:]), tuple(after[1:])
    def side(motion):
        at, order = motion if isinstance(motion, tuple) else (motion, None)
        if at < event_at or (at == event_at and order is not None and
                             event_order is not None and order < event_order):
            return "earlier" if at > before[0] else None
        if at > event_at or (at == event_at and order is not None and
                             event_order is not None and order > event_order):
            return "later" if at <= after[0] else None
        return "exact" if at == event_at else None
    sides = {side(motion) for motion in motion_times}
    if "exact" in sides:
        return None, None
    earlier_motion = "earlier" in sides
    later_motion = "later" in sides
    if earlier_motion and later_motion:
        return None, None
    if before_point == after_point:
        return before_point
    if earlier_motion:
        return after_point
    if later_motion:
        return before_point
    return None, None


class EvdevInput:
    """Use evdev for keys and libinput for interpreted pointer events."""

    def __init__(self, bridge):
        if bridge is None or not bridge.available:
            raise InputUnavailable("Enable the crec-input GNOME Shell extension before recording; "
                                   "faithful input on other compositors is not supported yet")
        self._bridge = bridge
        self._devices = []
        self._tasks = []
        self._events = asyncio.Queue(maxsize=10000)
        self._dispatch_task = None
        self._event_position = None
        self._snapshot_source = lambda _: {}
        self._wall_offset = time.time() - time.monotonic()
        self._motions = deque(maxlen=8192)
        self._last_heartbeat = 0
        self._raw_heap = []
        self._button_presses = {}
        self._serial = 0
        self._pointer_serial = 0
        self._last_delivered = float("-inf")
        self._pointer = None
        self._failure = None
        self._pressed = set()
        self._configuration = None
        self._keyboard_lock = asyncio.Lock()
        self._warned_pointer = False
        from xkbcommon import xkb
        self._xkb = xkb
        from .libinput import LibinputPointer
        self._pointer = LibinputPointer(bridge.get_pointer_settings)

    async def _owned_call(self, method):
        task = asyncio.create_task(asyncio.to_thread(method))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise

    def _open_devices(self):
        import evdev
        from evdev import ecodes
        self._ec = ecodes
        try:
            for path in evdev.list_devices():
                try:
                    dev = evdev.InputDevice(path)
                except OSError:
                    continue
                # Own the fd immediately, even if capabilities/active_keys fail.
                self._devices.append(dev)
                if ecodes.KEY_A not in dev.capabilities().get(ecodes.EV_KEY, []):
                    dev.close()
                    self._devices.remove(dev)
                    continue
                self._set_monotonic_clock(dev)
                self._pressed.update((dev.path, code) for code in dev.active_keys())
            if not self._devices:
                raise InputUnavailable("No readable keyboard devices; join the input group and log in again")
            self._sync_keyboard()
            self._pointer.start()
        except BaseException as exc:
            for dev in self._devices:
                dev.close()
            self._devices.clear()
            self._pointer.stop()
            if isinstance(exc, Exception) and not isinstance(exc, InputUnavailable):
                raise InputUnavailable(f"Linux input initialization failed: {exc}") from exc
            raise

    @staticmethod
    def _set_monotonic_clock(dev):
        """Select CLOCK_MONOTONIC through Linux's input-event UAPI.

        python-evdev 1.9 does not expose InputDevice.set_clockid(). Fedora's
        supported architectures use one of the two Linux ioctl encodings below.
        """
        machine = platform.machine().lower()
        if machine in {"ppc", "ppc64", "ppc64le", "mips", "mips64"}:
            direction, direction_shift = 4, 29
        elif machine in {"x86_64", "amd64", "i386", "i686", "aarch64", "arm64",
                         "armv7l", "riscv64", "s390x"}:
            direction, direction_shift = 1, 30
        else:
            raise InputUnavailable(
                f"EVIOCSCLOCKID ioctl encoding is not defined for architecture {machine}")
        request = ((direction << direction_shift) | (struct.calcsize("i") << 16) |
                   (ord("E") << 8) | 0xA0)
        try:
            fcntl.ioctl(dev.fd, request, struct.pack("i", time.CLOCK_MONOTONIC))
        except OSError as exc:
            raise InputUnavailable(
                f"Cannot select monotonic timestamps for {dev.path}: {exc}") from exc

    def _sync_keyboard(self, config=None):
        config = config or self._bridge.get_keyboard_state()
        options = config.get("options", [])
        if (config.get("type") != "xkb" or not config.get("id")
                or any(option.startswith("grp:") for option in options)):
            raise InputUnavailable("Unsupported keyboard configuration: use a GNOME XKB input source "
                                   "and GNOME layout switching (not IBus or independent grp: options)")
        identity = (config["id"], tuple(options), config.get("model", "pc105"))
        if identity == self._configuration:
            return
        layout, _, variant = config["id"].partition("+")
        keymap = self._xkb.Context().keymap_new_from_names(
            rules="evdev", layout=layout, variant=variant,
            options=",".join(options), model=identity[2])
        state = keymap.state_new()
        for code in {code for _, code in self._pressed}:
            state.update_key(code + 8, self._xkb.KeyDirection.XKB_KEY_DOWN)
        # Seed lock state from the compositor, and depressed modifiers from
        # keys already held on all devices. XKB modifier indices are named,
        # never assumed to equal Clutter's numeric mask.
        locks = 0
        for bit, name in ((2, "Lock"), (16, "Mod2")):
            if config["modifiers"] & bit:
                index = keymap.mod_get_index(name)
                locks |= 1 << index
        depressed = state.serialize_mods(self._xkb.StateComponent.XKB_STATE_MODS_DEPRESSED)
        state.update_mask(depressed, 0, locks, 0, 0, 0)
        self._state = state
        self._configuration = identity

    async def start(self, on_click, on_scroll, on_key, snapshot_source=None):
        self._on_click, self._on_scroll, self._on_key = on_click, on_scroll, on_key
        self._snapshot_source = snapshot_source or (lambda _: {})
        try:
            await self._owned_call(self._bridge.begin_pointer_history)
            await self._owned_call(self._open_devices)
            self._dispatch_task = asyncio.create_task(self._dispatch_events())
            self._tasks = [asyncio.create_task(self._acquire_events())]
        except BaseException:
            await self.stop()
            raise

    async def pointer_position(self):
        if asyncio.current_task() is self._dispatch_task and self._event_position is not None:
            return self._event_position
        pos = await self._owned_call(self._bridge.get_pointer)
        if pos is None:
            if not self._warned_pointer:
                log.error("Pointer position unavailable; retaining actions with unknown coordinates")
                self._warned_pointer = True
            return None, None
        self._warned_pointer = False
        return pos

    def check_health(self):
        if self._failure is not None:
            raise InputUnavailable(str(self._failure)) from self._failure

    def _render_key(self, code):
        sym = self._state.key_get_one_sym(code + 8)
        if not sym:
            raise InputUnavailable(f"Unmapped keyboard code {code}")
        name = self._xkb.keysym_get_name(sym)
        if name in _SPECIAL:
            return f"Key.{_SPECIAL[name]}"
        text = self._state.key_get_string(code + 8)
        if text and text.isprintable():
            return f"'{text}'"
        return f"Key.{name.lower()}"

    async def _callback(self, callback, args, context):
        try:
            await callback(*args, event=context)
        except Exception as exc:
            self._failure = self._failure or exc
            log.exception("Input callback failed; draining acquired events before stopping")

    def _context(self, event_at):
        try:
            frames = self._snapshot_source(event_at)
        except Exception as exc:
            self._failure = self._failure or exc
            log.exception("Cannot associate screenshots; retaining the input action and stopping")
            frames = {}
        return {"at": event_at, "wall_time": event_at + self._wall_offset, "frames": frames}

    @staticmethod
    def _complete_context(context, completed_at):
        context["completed_at"] = completed_at
        for pair in context.get("frames", {}).values():
            complete = getattr(pair, "complete", None)
            if complete is not None:
                complete(completed_at)

    async def _dispatch_events(self):
        while True:
            item = await self._events.get()
            try:
                if item is None:
                    return
                kind, first, second, self._event_position, context = item
                if kind == "key":
                    await self._handle_key_event(first, second, context)
                elif kind == "scroll":
                    x, y = self._event_position
                    await self._callback(self._on_scroll, (x, y, first, second), context)
                else:
                    x, y = self._event_position
                    await self._callback(self._on_click, (x, y, first), context)
            except Exception as exc:
                self._failure = self._failure or exc
                log.exception("Cannot process acquired input; stopping recorder")
            finally:
                self._event_position = None
                self._events.task_done()

    async def _finish_on_cancel(self, coroutine):
        # Once acquired, an event belongs to the recorder. Finish associating
        # and queuing it even when stop cancels the reader during a D-Bus call.
        task = asyncio.create_task(coroutine)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _handle_key_event(self, dev, ev, context):
        async with self._keyboard_lock:
            config = context.get("keyboard")
            if config is None:
                raise InputUnavailable(
                    "Acquired key has no event-time keyboard configuration")
            await self._owned_call(lambda: self._sync_keyboard(config))
            token = self._render_key(ev.code) if ev.value in (1, 2) else None
            key = (dev.path, ev.code)
            if ev.value == 1 and key not in self._pressed:
                already_down = any(code == ev.code for _, code in self._pressed)
                self._pressed.add(key)
                if not already_down:
                    self._state.update_key(ev.code + 8, self._xkb.KeyDirection.XKB_KEY_DOWN)
            elif ev.value == 0:
                self._pressed.discard(key)
                if not any(code == ev.code for _, code in self._pressed):
                    self._state.update_key(ev.code + 8, self._xkb.KeyDirection.XKB_KEY_UP)
        if token is not None:
            await self._callback(self._on_key, (token,), context)

    def _poll_sources(self):
        """Drain every nonblocking source before advancing one shared watermark.

        The 25 ms holdback allows libinput's interpreted events and the GNOME
        pointer sampler to catch up. Later events stay in a timestamp heap.
        No layout lookups, screenshot saves or callbacks run in this stage.
        """
        cutoff = time.monotonic() - 0.025
        events = []
        for dev in self._devices:
            while True:
                try:
                    batch = list(dev.read())
                except BlockingIOError:
                    break
                except Exception as exc:
                    self._failure = self._failure or exc
                    break
                if not batch:
                    break
                for ev in batch:
                    if ev.type == self._ec.EV_SYN and ev.code == self._ec.SYN_DROPPED:
                        self._failure = InputUnavailable("Kernel keyboard event overflow; recording is incomplete")
                    elif ev.type == self._ec.EV_KEY and ev.code < self._ec.BTN_MISC:
                        events.append((ev.timestamp(), "key", dev, ev, None))
        try:
            pointer_events = self._pointer.read()
        except Exception as exc:
            self._failure = self._failure or exc
            pointer_events = getattr(exc, "acquired_events", ())
        for kind, first, second, at in pointer_events:
            self._pointer_serial += 1
            order = self._pointer_serial
            if kind == "motion":
                self._motions.append((at, order))
            else:
                events.append((at, kind, first, second, ("pointer", order)))
        key_times = [at for at, kind, *_ in events if kind == "key"]
        if key_times:
            try:
                keyboard = self._bridge.keyboard_history(min(key_times) - 0.01)
            except Exception as exc:
                self._failure = self._failure or exc
                keyboard = []
            resolved = []
            for row in events:
                at, kind, first, second, metadata = row
                if kind == "key":
                    metadata = next((config for changed, config in reversed(keyboard)
                                     if changed <= at), None)
                    if metadata is None:
                        self._failure = self._failure or InputUnavailable(
                            "No keyboard configuration recorded before input event")
                resolved.append((at, kind, first, second, metadata))
            events = resolved
        return cutoff, events

    async def _acquire_batch(self, final=False):
        try:
            cutoff, events = await self._owned_call(self._poll_sources)
        except Exception as exc:
            self._failure = self._failure or exc
            cutoff, events = time.monotonic() - 0.025, []
        for at, kind, first, second, metadata in events:
            self._serial += 1
            if kind == "button" and not second:
                pressed = self._button_presses.pop(first, None)
                if pressed is not None:
                    self._complete_context(pressed, at)
                continue
            context = self._context(at)
            if isinstance(metadata, tuple) and metadata[0] == "pointer":
                context["pointer_order"] = metadata[1]
            elif metadata is not None:
                context["keyboard"] = metadata
            if kind == "button":
                # Insert at button-down to retain click chronology, but do not
                # dispatch it until the matching release establishes when an
                # after-state can exist.
                self._button_presses[first] = context
                heapq.heappush(
                    self._raw_heap, (at, self._serial, "click", first, None, context))
                continue
            if kind in ("click", "scroll"):
                # Compatibility for synthetic/legacy sources without a
                # separate release edge, and scroll gestures which are atomic.
                self._complete_context(context, at)
            heapq.heappush(self._raw_heap, (at, self._serial, kind, first, second, context))
        # Resolve coordinates as soon as an event clears the timestamp
        # watermark. A held button may block chronological dispatch until its
        # release, but must not delay association until compositor history has
        # evicted the press samples. Events queued behind it are pinned too.
        mature = [row for row in self._raw_heap
                  if (final or row[0] <= cutoff) and "position" not in row[5]]
        if mature:
            latest = max(row[0] for row in mature)
            await asyncio.sleep(max(0, latest + 0.025 - time.monotonic()))
            first_at = min(row[0] for row in mature)
            try:
                history = await self._owned_call(
                    lambda: self._bridge.pointer_history(first_at - 0.01))
            except Exception as exc:
                history = []
                self._failure = self._failure or exc
            positions = [position_at(
                history, row[0], self._motions, row[5].get("pointer_order"))
                for row in mature]
            if any(position == (None, None) for position in positions):
                # A compositor sample may still be in flight. Retry only the
                # historical query; never substitute the cursor's later live
                # position for an acquired event.
                await asyncio.sleep(0.02)
                try:
                    history = await self._owned_call(
                        lambda: self._bridge.pointer_history(first_at - 0.01))
                    positions = [position_at(
                        history, row[0], self._motions,
                        row[5].get("pointer_order")) for row in mature]
                except Exception as exc:
                    self._failure = self._failure or exc
            for row, position in zip(mature, positions):
                at, _, kind, _, _, context = row
                context["position"] = position
                if position == (None, None):
                    before = next((sample for sample in reversed(history)
                                   if sample[0] <= at), None)
                    after = next((sample for sample in history if sample[0] > at), None)
                    log.warning("Event-time pointer association unresolved: kind=%s at=%.6f "
                                "before=%r after=%r pointer_order=%r",
                                kind, at, before, after, context.get("pointer_order"))
            self._last_heartbeat = time.monotonic()

        ready = []
        while self._raw_heap and (final or self._raw_heap[0][0] <= cutoff):
            if (self._raw_heap[0][2] == "click" and
                    "completed_at" not in self._raw_heap[0][5]):
                if not final:
                    break
                context = self._raw_heap[0][5]
                context["incomplete"] = "button release was not observed"
                self._complete_context(context, time.monotonic())
            ready.append(heapq.heappop(self._raw_heap))
        if ready:
            for at, _, kind, first, second, context in ready:
                if at < self._last_delivered:
                    # libinput may synthesize a late event. Preserve it, but
                    # fail explicitly instead of claiming chronological data.
                    self._failure = InputUnavailable("Input arrived behind the committed timestamp watermark")
                self._last_delivered = max(at, self._last_delivered)
                await self._events.put(
                    (kind, first, second, context.get("position", (None, None)), context))
        elif not mature and time.monotonic() - self._last_heartbeat > 1:
            await self._owned_call(self._bridge.pointer_history)
            self._last_heartbeat = time.monotonic()
        if ready:
            self._last_heartbeat = time.monotonic()

    async def _acquire_events(self):
        try:
            while self._failure is None:
                await self._finish_on_cancel(self._acquire_batch())
                await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure = self._failure or exc
            log.error("Input acquisition interrupted: %s", exc)
        finally:
            # Drain kernel/libinput and held-back events once, while the
            # dispatcher and capture are still alive.
            try:
                await self._finish_on_cancel(self._acquire_batch(final=True))
            except Exception as exc:
                self._failure = self._failure or exc

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._dispatch_task is not None:
            # Drain already captured events while screen capture is still alive.
            await self._events.put(None)
            await self._dispatch_task
            self._dispatch_task = None
        loop = asyncio.get_running_loop()
        for dev in self._devices:
            if isinstance(getattr(dev, "fd", None), int):
                loop.remove_reader(dev.fd)
        def close():
            for dev in self._devices:
                dev.close()
            self._devices.clear()
            if self._pointer is not None:
                self._pointer.stop()
        try:
            await self._owned_call(close)
        finally:
            await self._owned_call(self._bridge.end_pointer_history)
