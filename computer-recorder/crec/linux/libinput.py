"""Small ctypes binding to libinput's interpreted pointer events.

A separate, non-grabbing seat context recognizes touchpad scrolling and taps.
GNOME's device settings are mirrored before dispatch; wheel v120 is converted
to detents, finger/continuous deltas to Mutter's 10-unit scroll steps. Legacy
AXIS events are ignored because libinput also emits the modern scroll events.
"""
from __future__ import annotations

import ctypes as C
import ctypes.util
import os
import time

from .inputs import InputUnavailable

_OPEN = C.CFUNCTYPE(C.c_int, C.c_char_p, C.c_int, C.c_void_p)
_CLOSE = C.CFUNCTYPE(None, C.c_int, C.c_void_p)


class _Interface(C.Structure):
    _fields_ = [("open_restricted", _OPEN), ("close_restricted", _CLOSE)]


class LibinputPointer:
    def __init__(self, settings_source):
        self._source = settings_source
        self._context = None
        self._udev = None
        self._fds = set()
        self._devices = set()
        self._settings = None
        self._last_settings = 0
        self._denied = False

    def _bind(self, name, result, *args):
        fn = getattr(self._lib, "libinput_" + name)
        fn.restype, fn.argtypes = result, list(args)
        setattr(self, name, fn)

    def start(self):
        try:
            self._lib = C.CDLL(ctypes.util.find_library("input") or "libinput.so.10")
            self._udev_lib = C.CDLL(ctypes.util.find_library("udev") or "libudev.so.1")
            self._udev_lib.udev_new.restype = C.c_void_p
            self._udev_lib.udev_unref.argtypes = [C.c_void_p]
            self._udev_lib.udev_unref.restype = C.c_void_p
            ptr, integer = C.c_void_p, C.c_int
            for name, result, args in [
                ("udev_create_context", ptr, [C.POINTER(_Interface), ptr, ptr]),
                ("udev_assign_seat", integer, [ptr, C.c_char_p]),
                ("unref", ptr, [ptr]), ("dispatch", integer, [ptr]),
                ("get_event", ptr, [ptr]), ("event_destroy", None, [ptr]),
                ("event_get_type", integer, [ptr]), ("event_get_device", ptr, [ptr]),
                ("event_get_pointer_event", ptr, [ptr]),
                ("event_pointer_get_time_usec", C.c_uint64, [ptr]),
                ("event_pointer_get_button", C.c_uint, [ptr]),
                ("event_pointer_get_button_state", integer, [ptr]),
                ("event_pointer_has_axis", integer, [ptr, integer]),
                ("event_pointer_get_scroll_value", C.c_double, [ptr, integer]),
                ("event_pointer_get_scroll_value_v120", C.c_double, [ptr, integer]),
                ("device_config_tap_get_finger_count", integer, [ptr]),
                ("device_has_capability", integer, [ptr, integer]),
            ]:
                self._bind(name, result, *args)
            for name in ("scroll_set_natural_scroll_enabled", "scroll_set_method",
                         "tap_set_enabled", "tap_set_drag_enabled", "tap_set_drag_lock_enabled",
                         "tap_set_button_map", "dwt_set_enabled", "left_handed_set",
                         "middle_emulation_set_enabled", "click_set_method", "send_events_set_mode"):
                self._bind("device_config_" + name, integer, ptr, integer)
            def open_device(path, flags, _data):
                try:
                    fd = os.open(path, flags | os.O_CLOEXEC)
                    self._fds.add(fd)
                    return fd
                except OSError as exc:
                    self._denied = True
                    return -exc.errno
            def close_device(fd, _data):
                if fd in self._fds:
                    os.close(fd)
                    self._fds.discard(fd)
            self._interface = _Interface(_OPEN(open_device), _CLOSE(close_device))
            self._udev = self._udev_lib.udev_new()
            if not self._udev:
                raise InputUnavailable("Could not create udev context")
            self._context = self.udev_create_context(C.byref(self._interface), None, self._udev)
            if not self._context or self.udev_assign_seat(
                    self._context, os.environ.get("XDG_SEAT", "seat0").encode()) != 0:
                raise InputUnavailable("Could not open libinput seat")
            self.read()  # DEVICE_ADDED events configure devices before recording.
            if self._denied or not self._devices:
                raise InputUnavailable("Cannot read all input devices; check input group membership")
        except BaseException:
            self.stop()
            raise

    def _configure(self, device):
        touchpad = self.device_config_tap_get_finger_count(device) > 0
        settings = self._settings["touchpad" if touchpad else "mouse"]
        def set_value(name, value):
            status = getattr(self, "device_config_" + name)(device, int(value))
            # Unsupported defaults are normal for hardware lacking that feature;
            # enabled/non-default unsupported settings cannot be reproduced.
            if status and value:
                raise InputUnavailable(f"libinput cannot apply GNOME setting {name}={value}")
        set_value("scroll_set_natural_scroll_enabled", settings["natural-scroll"])
        left = settings["left-handed"]
        if left == "mouse":
            left = self._settings["mouse"]["left-handed"]
        elif isinstance(left, str):
            left = left == "left"
        set_value("left_handed_set", left)
        set_value("middle_emulation_set_enabled", settings["middle-click-emulation"])
        if touchpad:
            method = 1 if settings["two-finger-scrolling-enabled"] else (
                2 if settings["edge-scrolling-enabled"] else 0)
            set_value("scroll_set_method", method)
            set_value("tap_set_enabled", settings["tap-to-click"])
            set_value("tap_set_drag_enabled", settings["tap-and-drag"])
            set_value("tap_set_drag_lock_enabled", settings["tap-and-drag-lock"])
            if settings["tap-button-map"] != "default":
                set_value("tap_set_button_map", {"lrm": 0, "lmr": 1}[settings["tap-button-map"]])
            set_value("dwt_set_enabled", settings["disable-while-typing"])
            if settings["click-method"] != "default":
                set_value("click_set_method", {"none": 0, "areas": 1, "fingers": 2}[settings["click-method"]])
            set_value("send_events_set_mode", {"enabled": 0, "disabled": 1,
                      "disabled-on-external-mouse": 2}[settings["send-events"]])

    def read(self):
        now = time.monotonic()
        if self._settings is None or now - self._last_settings >= 0.1:
            settings = self._source()
            if settings != self._settings:
                self._settings = settings
                for device in self._devices:
                    self._configure(device)
            self._last_settings = now
        if self.dispatch(self._context) != 0:
            raise InputUnavailable("libinput dispatch failed")
        result = []
        while event := self.get_event(self._context):
            try:
                kind = self.event_get_type(event)
                device = self.event_get_device(event)
                if kind == 1:  # DEVICE_ADDED
                    if self.device_has_capability(device, 1):  # POINTER
                        self._configure(device)
                        self._devices.add(device)
                elif kind == 2:  # DEVICE_REMOVED
                    self._devices.discard(device)
                    if not self._devices:
                        raise InputUnavailable("All pointer devices disconnected")
                elif kind in (400, 401):  # Relative/absolute motion; retain timing for correlation.
                    pointer = self.event_get_pointer_event(event)
                    result.append(("motion", None, None,
                                   self.event_pointer_get_time_usec(pointer) / 1000000))
                elif kind == 402:  # POINTER_BUTTON
                    pointer = self.event_get_pointer_event(event)
                    button = {272: "click_left", 273: "click_right", 274: "click_middle"}.get(
                        self.event_pointer_get_button(pointer))
                    if button:
                        # Keep both edges. Applications commonly activate a
                        # control on release, so button-down alone cannot
                        # establish when a click's result is renderable.
                        pressed = self.event_pointer_get_button_state(pointer) == 1
                        result.append(("button", button, pressed,
                                       self.event_pointer_get_time_usec(pointer) / 1000000))
                elif kind in (404, 405, 406):  # WHEEL, FINGER, CONTINUOUS
                    pointer = self.event_get_pointer_event(event)
                    getter = (self.event_pointer_get_scroll_value_v120 if kind == 404
                              else self.event_pointer_get_scroll_value)
                    divisor = 120.0 if kind == 404 else 10.0
                    dy, dx = [getter(pointer, axis) / divisor
                              if self.event_pointer_has_axis(pointer, axis) else 0.0
                              for axis in (0, 1)]
                    if dx or dy:
                        result.append(("scroll", dx, -dy, self.event_pointer_get_time_usec(pointer) / 1000000))
            except Exception as exc:
                # The acquisition merger must retain earlier events from this
                # batch even if a later device notification fails.
                exc.acquired_events = result
                raise
            finally:
                self.event_destroy(event)
        if self._denied:
            error = InputUnavailable("libinput lost access to an input device")
            error.acquired_events = result
            raise error
        return result

    def stop(self):
        if self._context:
            self.unref(self._context)
            self._context = None
        if self._udev:
            self._udev_lib.udev_unref(self._udev)
            self._udev = None
        for fd in list(self._fds):
            os.close(fd)
        self._fds.clear()
        self._devices.clear()
