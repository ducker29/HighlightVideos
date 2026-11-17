"""Quality table state, independent of Tk and download workers.

Use on the UI thread. click() returns the resulting state; the dialog starts a
worker on DOWNLOADING and reports its result with set_state(). A finished
download becomes DOWNLOADED, not SELECTED, until the next user click.
"""
from enum import Enum


class QualityState(str, Enum):
    NEUTRAL = "neutral"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    SELECTED = "selected"


STATE_LABELS = {
    QualityState.NEUTRAL: "не загружено",
    QualityState.DOWNLOADING: "загружается",
    QualityState.DOWNLOADED: "загружено",
    QualityState.SELECTED: "выбрано",
}


class QualityTableController:
    """Own row states and optional ttk-compatible button references.

    set_state() also initializes rows from disk. At most one download and one
    selection are allowed. A selection may remain active during a download.
    External blocking and download blocking are independent.
    """

    def __init__(self, heights=()):
        self.states = {}
        self.buttons = {}
        self._blocked = False
        for height in heights:
            self._validate_height(height)
            self.states[height] = QualityState.NEUTRAL

    @staticmethod
    def _validate_height(height):
        if type(height) is not int or height <= 0:
            raise ValueError("Высота качества должна быть положительным целым числом.")

    @property
    def blocked(self):
        return self._blocked or QualityState.DOWNLOADING in self.states.values()

    def bind_button(self, height, button):
        """Attach a button after creating its row; no Tk import is required."""
        if height not in self.states:
            raise KeyError(height)
        self.buttons[height] = button
        self._refresh_buttons()

    def _refresh_buttons(self):
        for height, button in self.buttons.items():
            button.configure(text=STATE_LABELS[self.states[height]],
                             state="disabled" if self.blocked else "normal")

    def block_all(self, blocked):
        if type(blocked) is not bool:
            raise ValueError("Флаг блокировки должен быть bool.")
        self._blocked = blocked
        self._refresh_buttons()

    def selected(self):
        return next((h for h, state in self.states.items()
                     if state == QualityState.SELECTED), None)

    def set_state(self, height, state):
        self._validate_height(height)
        state = QualityState(state)
        if state == QualityState.DOWNLOADING and any(
                h != height and s == QualityState.DOWNLOADING
                for h, s in self.states.items()):
            raise ValueError("Уже загружается другое качество.")
        if state == QualityState.SELECTED:
            previous = self.selected()
            if previous is not None and previous != height:
                self.states[previous] = QualityState.DOWNLOADED
        self.states[height] = state
        self._refresh_buttons()
        return state

    def click(self, height):
        state = self.states[height]
        if self.blocked:
            return state
        return self.set_state(height, {
            QualityState.NEUTRAL: QualityState.DOWNLOADING,
            QualityState.DOWNLOADED: QualityState.SELECTED,
            QualityState.SELECTED: QualityState.DOWNLOADED,
        }[state])
