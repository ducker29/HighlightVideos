import unittest

from quality_state import QualityState as S, QualityTableController, STATE_LABELS


class FakeButton:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)


class QualityStateTests(unittest.TestCase):
    def setUp(self):
        self.controller = QualityTableController([720, 1080, 2160])
        self.buttons = {h: FakeButton() for h in self.controller.states}
        for h, button in self.buttons.items():
            self.controller.bind_button(h, button)

    def test_download_success_select_and_deselect(self):
        c = self.controller
        self.assertIsNone(c.selected())
        self.assertEqual(c.click(1080), S.DOWNLOADING)
        c.set_state(1080, S.DOWNLOADED)
        self.assertFalse(c.blocked)
        self.assertIsNone(c.selected())
        self.assertEqual(c.click(1080), S.SELECTED)
        self.assertEqual(c.selected(), 1080)
        self.assertEqual(c.click(1080), S.DOWNLOADED)
        self.assertIsNone(c.selected())

    def test_download_error_allows_retry(self):
        c = self.controller
        c.click(720)
        c.set_state(720, S.NEUTRAL)
        self.assertFalse(c.blocked)
        self.assertEqual(self.buttons[720].options, {"text": "не загружено", "state": "normal"})
        self.assertEqual(c.click(720), S.DOWNLOADING)

    def test_download_blocks_all_clicks_and_buttons(self):
        c = self.controller
        c.set_state(720, S.SELECTED)
        c.click(1080)
        before = dict(c.states)
        for h in c.states:
            c.click(h)
            self.assertEqual(self.buttons[h].options["state"], "disabled")
        self.assertEqual(c.states, before)
        self.assertEqual(c.selected(), 720)
        with self.assertRaises(ValueError):
            c.set_state(2160, S.DOWNLOADING)
        self.assertEqual(c.states, before)
        c.block_all(False)
        self.assertTrue(c.blocked)
        c.set_state(1080, S.DOWNLOADED)
        self.assertTrue(all(b.options["state"] == "normal" for b in self.buttons.values()))

    def test_external_block_survives_download_completion(self):
        c = self.controller
        c.click(720)
        c.block_all(True)
        c.set_state(720, S.DOWNLOADED)
        self.assertEqual(c.click(720), S.DOWNLOADED)
        self.assertTrue(c.blocked)
        c.block_all(False)
        self.assertEqual(c.click(720), S.SELECTED)

    def test_selecting_other_quality_demotes_previous(self):
        c = self.controller
        c.set_state(720, S.SELECTED)
        c.set_state(1080, S.DOWNLOADED)
        c.click(1080)
        self.assertEqual(c.selected(), 1080)
        self.assertEqual(c.states[720], S.DOWNLOADED)
        self.assertEqual(self.buttons[720].options["text"], "загружено")
        self.assertEqual(self.buttons[1080].options["text"], "выбрано")
        c.set_state(2160, S.SELECTED)
        self.assertEqual(sum(s == S.SELECTED for s in c.states.values()), 1)

    def test_initialization_and_button_labels(self):
        c = self.controller
        for state in S:
            c.set_state(720, state)
            self.assertEqual(self.buttons[720].options["text"], STATE_LABELS[state])
        c.set_state(1440, "downloaded")
        button = FakeButton()
        c.bind_button(1440, button)
        self.assertEqual(button.options, {"text": "загружено", "state": "normal"})

    def test_invalid_input_does_not_change_state(self):
        c = self.controller
        before = dict(c.states)
        for height in (True, 0, -1, "720", 720.0):
            with self.subTest(height=height), self.assertRaises(ValueError):
                c.set_state(height, S.NEUTRAL)
        with self.assertRaises(ValueError):
            c.set_state(720, "invalid")
        with self.assertRaises(KeyError):
            c.click(1440)
        with self.assertRaises(ValueError):
            c.block_all("false")
        self.assertEqual(c.states, before)


if __name__ == "__main__":
    unittest.main()
