from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from streamgate import ConfigError, load_config


ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_base_config_is_valid_for_development(self) -> None:
        settings = load_config(
            ROOT / "config/base.ini",
            ROOT / "config/base.ini",
            environ={},
        )
        self.assertEqual(settings.runtime.environment, "development")
        self.assertEqual(settings.runtime.workers, 2)

    def test_unknown_option_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            overlay = Path(temporary) / "overlay.ini"
            overlay.write_text("[delivery]\ntimeout_ms = 5000\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(ROOT / "config/base.ini", overlay, environ={})


if __name__ == "__main__":
    unittest.main()
