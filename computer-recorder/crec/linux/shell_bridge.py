"""D-Bus client for the ``crec-input`` GNOME Shell extension.

Wayland deliberately denies ordinary clients a global view of the pointer and
of the monitor layout in logical (scaled) coordinates -- the information Quartz
provides for free on macOS.  The compositor does have it, so the companion
GNOME Shell extension (see ``linux/gnome-shell-extension``) exports two
read-only methods on the session bus:

    org.gnome.Shell  /org/crec/Input  org.crec.Input
        GetPointer()  -> (x, y)                     logical global coordinates
        GetMonitors() -> [(index, x, y, w, h, scale), ...]

All methods are pure queries; the extension stores nothing. Keyboard and
pointer settings queries expose GNOME's active XKB source and device settings.
Without the extension callers must reject faithful recording rather than guess.
"""

from __future__ import annotations

import logging
import json

log = logging.getLogger("crec.linux.shell_bridge")

_BUS_NAME = "org.gnome.Shell"
_OBJ_PATH = "/org/crec/Input"
_IFACE = "org.crec.Input"


class ShellBridge:
    """Thin synchronous wrapper around the extension's D-Bus object."""

    def __init__(self) -> None:
        self._proxy = None
        self.available = False
        self._history_token = None
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib  # noqa: F401

            self._Gio = Gio
            self._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                None,
                _BUS_NAME,
                _OBJ_PATH,
                _IFACE,
                None,
            )
            # new_for_bus_sync succeeds even if the object is absent; probe it.
            self._proxy.call_sync(
                "GetPointer", None, Gio.DBusCallFlags.NONE, 2000, None
            )
            self.available = True
            log.info("crec-input GNOME Shell extension detected")
        except Exception as exc:  # extension missing / not GNOME / no session bus
            log.info("crec-input extension unavailable (%s)", exc)
            self._proxy = None
            self.available = False

    def get_pointer(self) -> tuple[float, float] | None:
        """Return the pointer position in logical global coordinates, or None."""
        if not self._proxy:
            return None
        try:
            res = self._proxy.call_sync(
                "GetPointer", None, self._Gio.DBusCallFlags.NONE, 2000, None
            )
            x, y = res.unpack()
            return float(x), float(y)
        except Exception as exc:
            log.debug("GetPointer failed: %s", exc)
            return None

    def get_monitors(self) -> list[dict] | None:
        """Return monitor rectangles in logical coordinates, or None.

        Each dict has ``left``, ``top``, ``width``, ``height``, ``scale`` and
        matches the shape mss uses for ``sct.monitors`` (plus ``scale``).
        """
        if not self._proxy:
            return None
        try:
            res = self._proxy.call_sync(
                "GetMonitors", None, self._Gio.DBusCallFlags.NONE, 2000, None
            )
            (rows,) = res.unpack()
            mons = [
                {
                    "index": int(idx),
                    "left": int(x),
                    "top": int(y),
                    "width": int(w),
                    "height": int(h),
                    "scale": float(scale) or 1.0,
                }
                for (idx, x, y, w, h, scale) in rows
            ]
            mons.sort(key=lambda m: m["index"])
            return mons or None
        except Exception as exc:
            log.debug("GetMonitors failed: %s", exc)
            return None

    def _input_call(self, method, parameters=None):
        from .inputs import InputUnavailable
        if not self.available:
            raise InputUnavailable("crec-input extension unavailable")
        try:
            return self._proxy.call_sync(
                method, parameters, self._Gio.DBusCallFlags.NONE, 2000, None
            ).unpack()
        except Exception as exc:
            raise InputUnavailable(f"crec-input {method} failed: {exc}") from exc

    def get_keyboard_state(self):
        (payload,) = self._input_call("GetKeyboardState")
        return json.loads(payload)

    def keyboard_history(self, since):
        from gi.repository import GLib
        (payload,) = self._input_call("GetKeyboardHistory", GLib.Variant(
            "(x)", (int(since * 1000000),)))
        return [(at / 1000000, config) for at, config in json.loads(payload)]

    def get_pointer_settings(self):
        (payload,) = self._input_call("GetPointerSettings")
        return json.loads(payload)

    def begin_pointer_history(self):
        (self._history_token,) = self._input_call("BeginPointerHistory")

    def pointer_history(self, since=-1):
        from gi.repository import GLib
        (payload,) = self._input_call("ReadPointerHistory", GLib.Variant(
            "(sx)", (self._history_token, int(since * 1000000) if since >= 0 else -1)))
        return [(at / 1000000, x, y) for at, x, y in json.loads(payload)]

    def end_pointer_history(self):
        if self._history_token is not None:
            from gi.repository import GLib
            token, self._history_token = self._history_token, None
            self._input_call("EndPointerHistory", GLib.Variant("(s)", (token,)))

    def verify_protocol(self):
        """Reject an old loaded extension before displaying a capture dialog."""
        import xml.etree.ElementTree as ET
        from .inputs import InputUnavailable
        try:
            (xml,) = self._proxy.get_connection().call_sync(
                _BUS_NAME, _OBJ_PATH, "org.freedesktop.DBus.Introspectable", "Introspect",
                None, None, self._Gio.DBusCallFlags.NONE, 2000, None).unpack()
            methods = {node.attrib["name"] for node in ET.fromstring(xml).findall(
                "./interface[@name='org.crec.Input']/method")}
            required = {"GetPointer", "GetMonitors", "GetKeyboardState", "GetPointerSettings",
                        "GetKeyboardHistory", "BeginPointerHistory", "ReadPointerHistory",
                        "EndPointerHistory"}
            if not required <= methods:
                raise InputUnavailable("The loaded crec-input extension is outdated. "
                                       "Log out of GNOME and back in to load the updated files.")
        except InputUnavailable:
            raise
        except Exception as exc:
            raise InputUnavailable(f"Cannot verify crec-input extension: {exc}") from exc
