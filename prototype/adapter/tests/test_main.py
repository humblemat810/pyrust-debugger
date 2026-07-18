from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from prototype.adapter.__main__ import _default_codelldb_command


class CodeLldbCommandTests(unittest.TestCase):
    def test_explicit_paths_bypass_extension_discovery(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / "codelldb"
            liblldb = root / "liblldb.so"
            adapter.touch()
            liblldb.touch()

            command = _default_codelldb_command(str(adapter), str(liblldb))

        self.assertEqual(
            command,
            [str(adapter), "--liblldb", str(liblldb)],
        )

    def test_environment_paths_are_supported(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / "codelldb"
            liblldb = root / "liblldb.so"
            adapter.touch()
            liblldb.touch()
            with patch.dict(
                os.environ,
                {
                    "PYRUST_CODELLDB": str(adapter),
                    "PYRUST_LIBLLDB": str(liblldb),
                },
                clear=False,
            ):
                command = _default_codelldb_command()

        self.assertEqual(
            command,
            [str(adapter), "--liblldb", str(liblldb)],
        )

    def test_partial_configuration_is_rejected(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "configured together"):
                _default_codelldb_command("/tmp/codelldb", None)

    def test_missing_configured_file_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            liblldb = root / "liblldb.so"
            liblldb.touch()
            with self.assertRaisesRegex(ValueError, "does not exist"):
                _default_codelldb_command(
                    str(root / "missing-codelldb"),
                    str(liblldb),
                )


if __name__ == "__main__":
    unittest.main()
