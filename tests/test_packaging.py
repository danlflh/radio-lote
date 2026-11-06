"""Release metadata and built-archive checks, without opening a serial port.

Set RADIO_LOTE_WHEEL to a wheel file or to the dist directory to enable the
archive checks. They intentionally inspect the actual artifacts from a build.
"""

from email.parser import BytesParser
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tomllib
import unittest
import zipfile

from radio_lote import __version__
from radio_lote.core import CHIRP_REVISION


ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
HOME_PATTERNS = (
    re.compile(rb"/" + rb"Users/[^/\s\"']+/"),
    re.compile(rb"/" + rb"home/[^/\s\"']+/"),
    re.compile(rb"[A-Za-z]:\\" + rb"Users\\[^\\\s\"']+\\"),
)


class MetadataTests(unittest.TestCase):
    def test_runtime_lock_matches_installable_metadata(self):
        locked = {
            line.strip() for line in (ROOT / "requirements.lock").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertEqual(locked, set(PROJECT["dependencies"]))
        for requirement in PROJECT["dependencies"]:
            if requirement.startswith("chirp @ "):
                self.assertIn(f"/{CHIRP_REVISION}.zip#sha256=", requirement)
            else:
                self.assertRegex(requirement, r"^[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9_.+-]*$")
            self.assertNotIn("file:", requirement)

    def test_version_license_and_entrypoint_are_consistent(self):
        self.assertEqual(PROJECT["version"], __version__)
        self.assertEqual(PROJECT["license"], "GPL-3.0-or-later")
        self.assertEqual(PROJECT["scripts"]["radio-lote"], "radio_lote.cli:main")
        self.assertEqual(set(PROJECT["license-files"]), {"LICENSE", "NOTICE"})
        self.assertIn("GNU GENERAL PUBLIC LICENSE", (ROOT / "LICENSE").read_text())
        self.assertIn(CHIRP_REVISION, (ROOT / "NOTICE").read_text())


class DistributionTests(unittest.TestCase):
    def wheel_path(self):
        configured = os.environ.get("RADIO_LOTE_WHEEL")
        if not configured:
            self.skipTest("Set RADIO_LOTE_WHEEL=dist after building to inspect release artifacts")
        path = Path(configured).expanduser().resolve()
        if path.is_dir():
            candidates = list(path.glob("*.whl"))
            self.assertEqual(len(candidates), 1, "Build into a clean dist directory")
            path = candidates[0]
        self.assertTrue(path.is_file(), f"Wheel not found: {path}")
        return path

    def assert_safe_member(self, name, data):
        path = PurePosixPath(name)
        self.assertFalse(path.is_absolute(), name)
        self.assertNotIn("..", path.parts, name)
        self.assertFalse(set(path.parts) & {"dados", "outputs", ".venv", "__pycache__"}, name)
        self.assertNotIn(path.suffix.lower(), {".img", ".log", ".pyc", ".pyo", ".db", ".sqlite", ".zip"}, name)
        for pattern in HOME_PATTERNS:
            self.assertIsNone(pattern.search(data), f"User-specific home path in {name}")

    def test_wheel_contains_only_application_and_distribution_metadata(self):
        with zipfile.ZipFile(self.wheel_path()) as archive:
            names = archive.namelist()
            self.assertIn("radio_lote/cli.py", names)
            self.assertIn("radio_lote/core.py", names)
            self.assertIn("radio_lote/ui.tcss", names)
            for name in names:
                root_name = PurePosixPath(name).parts[0]
                self.assertTrue(root_name == "radio_lote" or root_name.endswith(".dist-info"), name)
                self.assert_safe_member(name, archive.read(name))

    def test_wheel_records_license_revision_and_console_entrypoint(self):
        with zipfile.ZipFile(self.wheel_path()) as archive:
            names = archive.namelist()
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            self.assertEqual(len(metadata_names), 1)
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
            self.assertEqual(metadata["Name"], "radio-lote")
            self.assertEqual(metadata["Version"], PROJECT["version"])
            self.assertEqual(metadata["License-Expression"], "GPL-3.0-or-later")
            requirements = metadata.get_all("Requires-Dist", [])
            chirp = [item for item in requirements if item.startswith("chirp @ ")]
            self.assertEqual(len(chirp), 1)
            self.assertIn(f"/{CHIRP_REVISION}.zip#sha256=", chirp[0])
            self.assertTrue(any(name.endswith("/licenses/LICENSE") for name in names))
            self.assertTrue(any(name.endswith("/licenses/NOTICE") for name in names))
            entries = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
            self.assertEqual(len(entries), 1)
            self.assertIn(b"radio-lote = radio_lote.cli:main", archive.read(entries[0]))

    def test_source_archive_contains_no_runtime_or_customer_artifacts(self):
        wheel = self.wheel_path()
        candidates = list(wheel.parent.glob("*.tar.gz"))
        self.assertEqual(len(candidates), 1, "Build both wheel and source archive into clean dist")
        with tarfile.open(candidates[0], "r:gz") as archive:
            names = {"/".join(PurePosixPath(item.name).parts[1:]) for item in archive.getmembers()}
            for required in ("README.en.md", "CONTRIBUTING.md", "SECURITY.md", "requirements.lock",
                             "LICENSE", "NOTICE", "PRODUCT.md", "DESIGN.md", "MANIFEST.in",
                             "docs/ARCHITECTURE.md", "docs/RELEASING.md", "tests/__init__.py"):
                self.assertIn(required, names)
            for member in archive.getmembers():
                self.assertTrue(member.isdir() or member.isfile(), member.name)
                if member.isfile():
                    handle = archive.extractfile(member)
                    self.assertIsNotNone(handle)
                    self.assert_safe_member(member.name, handle.read())


if __name__ == "__main__":
    unittest.main()
