"""
Output backends for Switch2 Bridge
==================================

`DSUServer` (dsu_server.py) is the primary backend: it presents the
controller to emulators as a real analog gamepad, needs no permissions and
no driver.

`KeyboardOutput` here is the legacy bridge — it types key presses, which
costs an Accessibility grant and throws away analog precision. It is off by
default and kept only for emulators that read nothing but the keyboard.
"""

import logging
import threading

log = logging.getLogger(__name__)

# Key names accepted in mappings.json, declared without importing pynput so
# that mappings can still be parsed and validated when pynput is missing.
SPECIAL_KEY_NAMES = frozenset(
    (
        "<up>", "<down>", "<left>", "<right>",
        "<space>", "<enter>", "<esc>", "<tab>",
        "<backspace>", "<delete>", "<home>", "<end>",
        "<pageup>", "<pagedown>",
        "<shift>", "<ctrl>", "<alt>", "<cmd>",
    )
    + tuple(f"<f{i}>" for i in range(1, 21))
)

# token -> attribute name on pynput.keyboard.Key
_PYNPUT_ATTRS = {
    "<up>": "up", "<down>": "down", "<left>": "left", "<right>": "right",
    "<space>": "space", "<enter>": "enter", "<esc>": "esc", "<tab>": "tab",
    "<backspace>": "backspace", "<delete>": "delete",
    "<home>": "home", "<end>": "end",
    "<pageup>": "page_up", "<pagedown>": "page_down",
    "<shift>": "shift", "<ctrl>": "ctrl", "<alt>": "alt", "<cmd>": "cmd",
}
_PYNPUT_ATTRS.update({f"<f{i}>": f"f{i}" for i in range(1, 21)})


def pynput_available():
    try:
        import pynput.keyboard  # noqa: F401
        return True
    except Exception:
        return False


class KeyboardOutput:
    """Types key presses for a ControllerState. Legacy, opt-in.

    Two inputs may share one key: presses are reference counted so releasing
    one source does not release a key another source still holds.
    """

    # Stick directions, as (source name, mapping side, direction, sign, axis)
    _STICK_SOURCES = (
        ('ls_up', 'left', 'up', 1, 'ly'),
        ('ls_down', 'left', 'down', -1, 'ly'),
        ('ls_left', 'left', 'left', -1, 'lx'),
        ('ls_right', 'left', 'right', 1, 'lx'),
        ('rs_up', 'right', 'up', 1, 'ry'),
        ('rs_down', 'right', 'down', -1, 'ry'),
        ('rs_left', 'right', 'left', -1, 'rx'),
        ('rs_right', 'right', 'right', 1, 'rx'),
    )

    def __init__(self, mappings):
        self.mappings = mappings
        self.last_error = None  # consumed by the UI tick
        self._controller = None
        self._lock = threading.Lock()
        self._source_keys = {}  # source name -> key token currently held
        self._key_refs = {}     # resolved key -> number of sources holding it

    # --- lifecycle ---

    @property
    def enabled(self):
        return self._controller is not None

    def start(self):
        """Create the pynput controller. Returns True when typing is possible."""
        if self._controller is not None:
            return True
        try:
            from pynput.keyboard import Controller
            self._controller = Controller()
        except Exception as e:
            log.warning("keyboard output unavailable: %s", e)
            self.last_error = (
                f"Keyboard bridge unavailable: {e}\n"
                "Install pynput, or leave the keyboard bridge off and use DSU."
            )
            return False
        log.info("keyboard output started")
        return True

    def stop(self):
        self.release_all()
        self._controller = None
        log.info("keyboard output stopped")

    # --- key resolution ---

    def _resolve(self, token):
        """mappings token -> object pynput can press, or None."""
        if token is None or self._controller is None:
            return None
        attr = _PYNPUT_ATTRS.get(token)
        if attr is None:
            return token  # a plain single character
        try:
            from pynput.keyboard import Key
            return getattr(Key, attr)
        except Exception:
            return None

    # --- key dispatch (reference counted) ---

    def _press_ref(self, key):
        n = self._key_refs.get(key, 0)
        self._key_refs[key] = n + 1
        if n == 0:
            try:
                self._controller.press(key)
            except Exception as e:
                log.warning("keyboard press failed: %s", e)

    def _release_ref(self, key):
        n = self._key_refs.get(key, 0)
        if n <= 1:
            self._key_refs.pop(key, None)
            try:
                self._controller.release(key)
            except Exception as e:
                log.warning("keyboard release failed: %s", e)
        else:
            self._key_refs[key] = n - 1

    def _set_key(self, source, token, active):
        """Press/release `token` on behalf of `source` (a button or direction)."""
        with self._lock:
            if self._controller is None:
                return
            prev = self._source_keys.get(source)
            key = self._resolve(token) if active else None
            if active and key is not None:
                if prev == key:
                    return
                if prev is not None:
                    self._release_ref(prev)
                self._source_keys[source] = key
                self._press_ref(key)
            else:
                if prev is None:
                    return
                del self._source_keys[source]
                self._release_ref(prev)

    def release_all(self):
        with self._lock:
            for key in list(self._key_refs):
                try:
                    if self._controller is not None:
                        self._controller.release(key)
                except Exception as e:
                    log.warning("keyboard release on cleanup failed: %s", e)
            self._key_refs.clear()
            self._source_keys.clear()

    def _set_stick_key(self, source, token, value):
        """Threshold with hysteresis: press above t, release below 0.8*t.

        Avoids key chatter when the stick hovers right at the threshold.
        """
        t = self.mappings.stick_threshold
        held = source in self._source_keys
        self._set_key(source, token, value > (t * 0.8 if held else t))

    # --- the backend interface ---

    def push(self, state):
        """Render a ControllerState as key presses."""
        if self._controller is None:
            return
        buttons = self.mappings.buttons
        for name, token in buttons.items():
            self._set_key(name, token, state.pressed(name))

        sides = {'left': self.mappings.left_stick, 'right': self.mappings.right_stick}
        for source, side, direction, sign, axis in self._STICK_SOURCES:
            self._set_stick_key(
                source, sides[side].get(direction), sign * getattr(state, axis)
            )
