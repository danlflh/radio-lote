"""Portability checks without opening any physical port."""

import ctypes
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from radio_lote import core, platforms, storage


class PortabilityTests(unittest.TestCase):
    def test_platform_data_directory_is_not_the_installation_directory(self):
        with mock.patch.object(platforms.platformdirs, "user_data_path", return_value=Path("/user/data")) as path:
            self.assertEqual(platforms.default_data_dir(), Path("/user/data"))
            path.assert_called_once_with("radio-lote", appauthor=False, ensure_exists=False)

    def test_valid_local_names_and_urls_or_arbitrary_paths_rejected(self):
        for name in ("COM1", "COM12", "com3", r"\\.\COM20", "/dev/ttyUSB0", "/dev/ttyACM2", "/dev/cu.usbserial-110"):
            with self.subTest(name=name):
                self.assertTrue(platforms.serial_port_is_local(name))
        for name in ("socket://localhost:9999", "rfc2217://host:123", "loop://", "/tmp/radio", "COM0", "COM1/other", None):
            with self.subTest(name=name):
                self.assertFalse(platforms.serial_port_is_local(name))

    def test_enumeration_supports_all_three_systems_without_opening(self):
        devices = [
            SimpleNamespace(device=name, description="Fixture", hwid=hardware, vid=None, pid=None)
            for name, hardware in (("COM12", ""), ("/dev/ttyUSB0", "USB TEST"),
                                   ("/dev/ttyACM0", ""), ("/dev/cu.usbserial-test", "USB TEST"),
                                   ("/dev/cu.Bluetooth-Incoming-Port", ""), ("socket://test", "USB"))
        ]
        with mock.patch.object(core.list_ports, "comports", return_value=devices), \
                mock.patch.object(core, "_LoggingSerial") as serial:
            names = {port["device"] for port in core.list_usb_ports()}
            self.assertEqual(names, {"COM12", "/dev/ttyUSB0", "/dev/ttyACM0", "/dev/cu.usbserial-test"})
            self.assertEqual(core.resolve_port("com12"), "COM12")
            serial.assert_not_called()

    def test_exclusive_option_is_posix_only(self):
        with mock.patch.object(platforms, "is_windows", return_value=True):
            self.assertEqual(platforms.serial_exclusive_options(), {})
        with mock.patch.object(platforms, "is_windows", return_value=False):
            self.assertEqual(platforms.serial_exclusive_options(), {"exclusive": True})

    def test_windows_publication_uses_write_through_without_overwrite_by_default(self):
        move = mock.Mock(return_value=1)
        kernel = SimpleNamespace(MoveFileExW=move)
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
            platforms._windows_move(Path("source"), Path("destination"), replace=False)
            self.assertEqual(move.call_args.args[2], 0x8)
            platforms._windows_move(Path("source"), Path("destination"), replace=True)
            self.assertEqual(move.call_args.args[2], 0x9)

    def test_windows_existing_destination_is_reported_without_fallback(self):
        move = mock.Mock(return_value=0)
        with mock.patch.object(ctypes, "WinDLL", return_value=SimpleNamespace(MoveFileExW=move), create=True), \
                mock.patch.object(ctypes, "get_last_error", return_value=183, create=True):
            with self.assertRaises(FileExistsError):
                platforms._windows_move(Path("source"), Path("destination"), replace=False)
        move.assert_called_once()

    def test_json_durable_publication_and_no_overwrite_with_unicode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "registro.json"
            storage.write_json(path, {"label": "Árbitro"})
            with self.assertRaises(FileExistsError):
                storage.write_json(path, {"label": "Outro"})
            self.assertEqual(storage.read_json(path), {"label": "Árbitro"})
            storage.write_json(path, {"label": "Revisão"}, replace=True)
            self.assertEqual(storage.read_json(path), {"label": "Revisão"})

    def test_operation_lock_rejects_second_holder_and_releases_after_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with storage.operation_lock(root):
                with self.assertRaises(core.RadioLoteError):
                    with storage.operation_lock(root):
                        self.fail("second holder acquired the lock")
            with self.assertRaises(RuntimeError):
                with storage.operation_lock(root):
                    raise RuntimeError("interrupted operation")
            with storage.operation_lock(root):
                pass

    def test_directory_publication_preserves_tree_and_never_replaces_empty_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            (staging / "original.img").write_bytes(b"backup fixture")
            parent = root / "batches"
            parent.mkdir()
            destination = parent / "existing"
            destination.mkdir()
            with self.assertRaises(FileExistsError):
                platforms.publish_directory(staging, destination)
            self.assertEqual((staging / "original.img").read_bytes(), b"backup fixture")
            self.assertEqual(list(destination.iterdir()), [])
            published = parent / "new"
            platforms.publish_directory(staging, published)
            self.assertFalse(staging.exists())
            self.assertEqual((published / "original.img").read_bytes(), b"backup fixture")


if __name__ == "__main__":
    unittest.main()
