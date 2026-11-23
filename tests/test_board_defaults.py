#!/usr/bin/env python3
"""Defaults do board.toml: touch ligado, sondas experimentais desligadas."""
import tomllib
import unittest
from pathlib import Path

BOARD = tomllib.loads((Path(__file__).resolve().parents[1] / "board.toml").read_text())


def _kernel_args(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "kernel_args":
                yield from v
            else:
                yield from _kernel_args(v)
    elif isinstance(node, list):
        for v in node:
            yield from _kernel_args(v)


class BoardDefaultsTests(unittest.TestCase):
    def test_touch_enabled_by_default(self):
        self.assertIn("sm6125-touch=5", list(_kernel_args(BOARD)))

    def test_gpu_probe_not_in_default_boot(self):
        self.assertFalse([a for a in _kernel_args(BOARD) if a.startswith("sm6125-gpu")],
                         "sm6125-gpu e sonda experimental: so via --kernel-arg")


if __name__ == "__main__":
    unittest.main()
