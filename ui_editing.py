"""Layout-independent editing shortcuts for Tkinter Entry/Combobox on Windows."""
import tkinter as tk

_WINDOWS_KEYS = {65: "all", 67: "copy", 86: "paste", 88: "cut"}
_SYMBOL_KEYS = {"a": "all", "c": "copy", "v": "paste", "x": "cut"}
_VIRTUAL = {"copy": "<<Copy>>", "paste": "<<Paste>>", "cut": "<<Cut>>"}


def edit_action(widget, action):
    if widget.instate(["disabled"]):
        return
    if action in ("paste", "cut") and widget.instate(["readonly"]):
        return
    if action == "all":
        widget.selection_range(0, tk.END)
        widget.icursor(tk.END)
    else:
        widget.event_generate(_VIRTUAL[action])


def handle_edit_key(widget, event):
    # Do not intercept AltGr or Ctrl+Alt application shortcuts.
    if not event.state & 0x4 or event.state & (0x8 | 0x20000):
        return None
    if widget.tk.call("tk", "windowingsystem") == "win32":
        action = _WINDOWS_KEYS.get(event.keycode)
    else:
        action = _SYMBOL_KEYS.get(event.keysym.lower())
    if action:
        edit_action(widget, action)
        # Suppress the class binding, otherwise English-layout Ctrl+V pastes twice.
        return "break"
    return None


def install_editing(widget):
    widget.bind("<Control-KeyPress>", lambda event: handle_edit_key(widget, event), add="+")
    menu = tk.Menu(widget, tearoff=False)
    for title, action in (("Вырезать", "cut"), ("Копировать", "copy"), ("Вставить", "paste"), ("Выделить всё", "all")):
        menu.add_command(label=title, command=lambda action=action: edit_action(widget, action))

    def popup(event):
        if widget.instate(["disabled"]):
            return "break"
        widget.focus_set()
        readonly = widget.instate(["readonly"])
        selected = widget.selection_present()
        for index, enabled in enumerate((selected and not readonly, selected, not readonly, True)):
            menu.entryconfigure(index, state="normal" if enabled else "disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    widget.bind("<Button-3>", popup, add="+")
    widget._edit_menu = menu
