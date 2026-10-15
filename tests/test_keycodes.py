"""Regression tests for vimview key handling.

The SDLK_dollar AttributeError (2026-10-15) crashed the app on the first
normal-mode keypress: the keymap dict is built inside handle_keydown_normal,
so the bad constant only exploded at runtime. These tests pin every
sdl2 constant referenced in vimview.py and exercise the $ bindings.
Run: python3 -m unittest discover -s tests
"""
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import sdl2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vimview


class TestKeycodes(unittest.TestCase):
    def test_all_sdl_constants_exist(self):
        src = Path(vimview.__file__).read_text()
        names = sorted(set(re.findall(r"sdl2\.(SDLK_\w+|KMOD_\w+)", src)))
        self.assertTrue(names, "no sdl2 constants found in vimview.py?")
        missing = [n for n in names if not hasattr(sdl2, n)]
        self.assertEqual(missing, [], f"unknown sdl2 constants: {missing}")

    def _viewer(self):
        v = MagicMock()
        v.pending = None
        return v

    def test_dollar_key_goes_to_row_end(self):
        v = self._viewer()
        vimview.Viewer.handle_keydown_normal(v, sdl2.SDLK_DOLLAR, 0)
        v.row_edge.assert_called_once_with(False)

    def test_shift_4_is_dollar_on_us_layouts(self):
        # $ on US keyboards arrives as SDLK_4 + KMOD_SHIFT, not SDLK_DOLLAR
        v = self._viewer()
        vimview.Viewer.handle_keydown_normal(v, sdl2.SDLK_4, sdl2.KMOD_SHIFT)
        v.row_edge.assert_called_once_with(False)

    def test_zero_goes_to_row_start(self):
        v = self._viewer()
        vimview.Viewer.handle_keydown_normal(v, sdl2.SDLK_0, 0)
        v.row_edge.assert_called_once_with(True)

    def test_plain_4_does_not_jump(self):
        v = self._viewer()
        vimview.Viewer.handle_keydown_normal(v, sdl2.SDLK_4, 0)
        v.row_edge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
