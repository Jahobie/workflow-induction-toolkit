// Read-only compositor state. No input interception or event suppression.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Keyboard from 'resource:///org/gnome/shell/ui/status/keyboard.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const IFACE = `<node><interface name="org.crec.Input">
  <method name="GetPointer"><arg type="i" direction="out"/><arg type="i" direction="out"/></method>
  <method name="GetMonitors"><arg type="a(iiiiid)" direction="out"/></method>
  <method name="BeginPointerHistory"><arg type="s" direction="out"/></method>
  <method name="ReadPointerHistory"><arg type="s" direction="in"/><arg type="x" direction="in"/><arg type="s" direction="out"/></method>
  <method name="EndPointerHistory"><arg type="s" direction="in"/></method>
  <method name="GetPointerSettings"><arg type="s" direction="out"/></method>
  <method name="GetKeyboardState"><arg type="s" direction="out"/></method>
  <method name="GetKeyboardHistory"><arg type="x" direction="in"/><arg type="s" direction="out"/></method>
</interface></node>`;

export default class CrecInputExtension extends Extension {
    enable() {
        this._leases = new Map();
        this._history = [];
        this._settings = new Gio.Settings({schema_id: 'org.gnome.desktop.input-sources'});
        this._inputSources = Keyboard.getInputSourceManager();
        this._keyboardHistory = [];
        this._sourceSignal = this._inputSources.connect('current-source-changed',
            () => this._sampleKeyboard());
        this._settingsSignal = this._settings.connect('changed',
            () => this._sampleKeyboard());
        this._sampleKeyboard();
        this._pointerSettings = Object.fromEntries(['mouse', 'touchpad'].map(name =>
            [name, new Gio.Settings({schema_id: `org.gnome.desktop.peripherals.${name}`})]));
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, '/org/crec/Input');
    }

    disable() {
        this._stopHistory();
        this._leases.clear();
        this._dbus?.unexport();
        this._dbus = null;
        if (this._sourceSignal)
            this._inputSources.disconnect(this._sourceSignal);
        if (this._settingsSignal)
            this._settings.disconnect(this._settingsSignal);
        this._sourceSignal = 0;
        this._settingsSignal = 0;
        this._inputSources = null;
        this._keyboardHistory = [];
        this._settings = null;
        this._pointerSettings = null;
    }

    GetPointer() {
        return global.get_pointer().slice(0, 2);
    }

    GetMonitors() {
        return Main.layoutManager.monitors.map(m =>
            [m.index, m.x, m.y, m.width, m.height, m.geometry_scale]);
    }

    _samplePointer() {
        const [x, y] = this.GetPointer();
        this._history.push([GLib.get_monotonic_time(), x, y]);
        if (this._history.length > 8192)
            this._history = this._history.slice(-4096);
    }

    _stopHistory() {
        if (this._historyTimer) {
            GLib.Source.remove(this._historyTimer);
            this._historyTimer = 0;
        }
        if (this._positionSignal) {
            this._tracker.disconnect(this._positionSignal);
            this._positionSignal = 0;
        }
        this._tracker = null;
        this._history = [];
    }

    BeginPointerHistory() {
        // Establish a valid layout baseline before the recorder opens input
        // devices; every subsequent key can then select a preceding snapshot.
        this._sampleKeyboard();
        if (!this._historyTimer) {
            this._history = [];
            // GNOME 49 moved this constructor to Meta.Backend. Keep the older
            // accessor for GNOME releases still advertised in metadata.json.
            this._tracker = global.backend?.get_cursor_tracker?.() ??
                Meta.get_backend?.()?.get_cursor_tracker?.() ??
                Meta.CursorTracker.get_for_display?.(global.display);
            if (!this._tracker)
                throw new Error('GNOME cursor tracker API unavailable');
            this._positionSignal = this._tracker.connect('position-invalidated',
                () => this._samplePointer());
            this._samplePointer();
            // Sampling also records stationary intervals. Queries never assign
            // today's pointer to an earlier event; gaps/motion are ambiguous.
            this._historyTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 2, () => {
                const now = GLib.get_monotonic_time();
                for (const [token, lastRead] of this._leases) {
                    if (now - lastRead > 5000000)
                        this._leases.delete(token);
                }
                if (!this._leases.size) {
                    this._historyTimer = 0;
                    this._stopHistory();
                    return GLib.SOURCE_REMOVE;
                }
                this._samplePointer();
                return GLib.SOURCE_CONTINUE;
            });
        }
        const token = GLib.uuid_string_random();
        this._leases.set(token, GLib.get_monotonic_time());
        return token;
    }

    ReadPointerHistory(token, since) {
        if (!this._leases.has(token))
            throw new Error('Pointer history lease expired');
        this._leases.set(token, GLib.get_monotonic_time());
        if (since < 0)
            return '[]'; // Keepalive without transmitting an idle history.
        this._samplePointer();
        let first = this._history.findIndex(sample => sample[0] >= since);
        if (first < 0)
            first = this._history.length;
        return JSON.stringify(this._history.slice(Math.max(0, first - 1)));
    }

    EndPointerHistory(token) {
        this._leases.delete(token);
        if (!this._leases.size)
            this._stopHistory();
    }

    GetPointerSettings() {
        return JSON.stringify(Object.fromEntries(Object.entries(this._pointerSettings).map(
            ([name, settings]) => [name, Object.fromEntries(settings.settings_schema.list_keys().map(
                key => [key, settings.get_value(key).deepUnpack()]))])));
    }

    _keyboardState() {
        const source = this._inputSources.currentSource;
        return {
            type: source?.type,
            id: source?.xkbId,
            options: this._settings.get_strv('xkb-options'),
            model: this._settings.settings_schema.has_key('xkb-model')
                ? this._settings.get_string('xkb-model') : 'pc105',
            modifiers: global.get_pointer()[2],
        };
    }

    _sampleKeyboard() {
        const row = [GLib.get_monotonic_time(), this._keyboardState()];
        const previous = this._keyboardHistory.at(-1);
        if (!previous || JSON.stringify(previous[1]) !== JSON.stringify(row[1]))
            this._keyboardHistory.push(row);
        if (this._keyboardHistory.length > 4096)
            this._keyboardHistory = this._keyboardHistory.slice(-2048);
    }

    GetKeyboardHistory(since) {
        this._sampleKeyboard();
        let first = this._keyboardHistory.findIndex(row => row[0] >= since);
        if (first < 0)
            first = this._keyboardHistory.length;
        return JSON.stringify(this._keyboardHistory.slice(Math.max(0, first - 1)));
    }

    GetKeyboardState() {
        // IBus and independent XKB group toggles must be rejected by the client:
        // their effective layout cannot be reconstructed from this interface.
        return JSON.stringify(this._keyboardState());
    }
}
