"""Clipboard/keyboard regressions without writing to the user's OS clipboard."""
import sys
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from review_ui import HighlightApp
from ui_editing import handle_edit_key, install_editing


class EditingTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.entry = ttk.Entry(self.root)
        install_editing(self.entry)

    def tearDown(self):
        self.root.destroy()

    @unittest.skipUnless(sys.platform == "win32", "Windows virtual key mapping")
    def test_ctrl_shortcuts_use_physical_keys_and_suppress_duplicate_paste(self):
        for symbol in ("v", "Cyrillic_em", "??"):
            event = SimpleNamespace(keycode=86, keysym=symbol, state=4)
            with patch.object(self.entry, "event_generate") as generate:
                self.assertEqual(handle_edit_key(self.entry, event), "break")
                generate.assert_called_once_with("<<Paste>>")
        for key, symbol, virtual in ((67, "Cyrillic_es", "<<Copy>>"), (88, "Cyrillic_che", "<<Cut>>")):
            with patch.object(self.entry, "event_generate") as generate:
                self.assertEqual(handle_edit_key(self.entry, SimpleNamespace(keycode=key, keysym=symbol, state=4)), "break")
                generate.assert_called_once_with(virtual)
        self.entry.insert(0, "Existing URL")
        handle_edit_key(self.entry, SimpleNamespace(keycode=65, keysym="Cyrillic_ef", state=4))
        self.assertEqual(self.entry.index("sel.first"), 0)
        self.assertEqual(self.entry.index("sel.last"), len(self.entry.get()))
        with patch.object(self.entry, "event_generate") as generate:
            self.assertIsNone(handle_edit_key(self.entry, SimpleNamespace(keycode=86, keysym="v", state=4 | 0x20000)))
            self.assertIsNone(handle_edit_key(self.entry, SimpleNamespace(keycode=86, keysym="v", state=0)))
            generate.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows virtual key mapping")
    def test_disabled_readonly_and_context_menu(self):
        event = SimpleNamespace(keycode=86, keysym="Cyrillic_em", state=4)
        with patch.object(self.entry, "event_generate") as generate:
            self.entry.state(["disabled"])
            self.assertEqual(handle_edit_key(self.entry, event), "break")
            generate.assert_not_called()
            self.entry.state(["!disabled", "readonly"])
            handle_edit_key(self.entry, event)
            generate.assert_not_called()
            handle_edit_key(self.entry, SimpleNamespace(keycode=67, keysym="c", state=4))
            generate.assert_called_once_with("<<Copy>>")
            generate.reset_mock()
            self.entry.state(["!readonly"])
            self.assertEqual(self.entry._edit_menu.entrycget(2, "label"), "Вставить")
            self.entry._edit_menu.invoke(2)
            generate.assert_called_once_with("<<Paste>>")

    def test_url_button_replaces_existing_url_and_honors_busy_state(self):
        app = HighlightApp(self.root)
        app.variables["youtube_url"].set("old URL")
        with patch.object(self.root, "clipboard_get", return_value=" https://www.youtube.com/watch?v=new-test \n") as clipboard:
            app.paste_url()
            clipboard.assert_called_once_with()
        self.assertEqual(app.variables["youtube_url"].get(), "https://www.youtube.com/watch?v=new-test")
        for busy, disabled in ((True, False), (False, True)):
            app.busy = busy
            app.url_entry.state(["disabled"] if disabled else ["!disabled"])
            with patch.object(self.root, "clipboard_get", side_effect=AssertionError("Blocked field must not read clipboard")):
                app.paste_url()
        self.assertEqual(app.variables["youtube_url"].get(), "https://www.youtube.com/watch?v=new-test")

    def test_empty_or_unavailable_clipboard_does_not_erase_url(self):
        app = HighlightApp(self.root)
        app.variables["youtube_url"].set("existing URL")
        with patch.object(self.root, "clipboard_get", side_effect=tk.TclError("No text")):
            app.paste_url()
        self.assertEqual(app.variables["youtube_url"].get(), "existing URL")
        with patch.object(self.root, "clipboard_get", return_value=" \n "):
            app.paste_url()
        self.assertEqual(app.variables["youtube_url"].get(), "existing URL")

    @unittest.skipUnless(sys.platform == "win32", "Windows keyboard paste integration")
    def test_real_clipboard_paste_reads_without_overwriting_clipboard(self):
        try:
            content = self.root.clipboard_get()
        except tk.TclError:
            self.skipTest("No text clipboard available; clipboard is never modified for this test")
        if not content:
            self.skipTest("Empty clipboard")
        self.root.title("Paste integration check")
        self.root.geometry("240x60+30+30")
        self.entry.pack()
        self.root.deiconify()
        self.root.update()
        self.root.focus_force()
        self.entry.focus_force()
        self.root.update()
        self.entry.insert(0, "Existing URL")
        self.entry.event_generate("<KeyPress>", keycode=65, state=4)
        self.entry.event_generate("<KeyPress>", keycode=86, state=4)
        self.root.update()
        self.assertTrue(self.entry.get() == content, "Physical Ctrl+V must paste exactly once and replace selection")
        self.assertTrue(self.root.clipboard_get() == content, "Integration test must not overwrite clipboard")


if __name__ == "__main__":
    unittest.main()
