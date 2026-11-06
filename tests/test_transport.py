"""Run the installed CHIRP H777 protocol against an in-memory serial endpoint."""

import struct
import unittest
from unittest import mock

from radio_lote import core, platforms
from tests.test_core import synthetic_raw


class H777SerialEmulator:
    """Only the BF-888 clone protocol is emulated; the driver is not mocked."""

    # Exercise our actual logging adapter's method as well as the CHIRP driver.
    log = core._LoggingSerial.log

    def __init__(self, raw, ident=b"P3107abc", fail_write_ack_address=None):
        self.memory = bytearray(raw)
        self.ident = ident
        self.is_open = False
        self.state = "idle"
        self.pending = bytearray()
        self.commands = []
        self.read_addresses = []
        self.write_addresses = []
        self.ends = 0
        self.options = None
        self.open_count = 0
        self.close_count = 0
        self.drained = []
        self.fail_write_ack_address = fail_write_ack_address

    def factory(self, **options):
        self.options = options
        for key, value in options.items():
            setattr(self, key, value)
        return self

    def open(self):
        if self.is_open:
            raise AssertionError("Serial already open")
        self.opened_with = {"port": self.port, "rts": self.rts, "dtr": self.dtr}
        self.is_open = True
        self.open_count += 1

    def close(self):
        if self.is_open:
            self.close_count += 1
        self.is_open = False

    def reset_input_buffer(self):
        if not self.is_open:
            raise AssertionError("Reset on closed serial")
        self.drained.append(bytes(self.pending))
        self.pending.clear()

    def write(self, data):
        if not self.is_open:
            raise AssertionError("Write on closed serial")
        data = bytes(data)
        self.commands.append(data)
        if data == b"\x02" and self.state == "idle":
            self.state = "prefix"
        elif data == b"PROGRAM" and self.state == "prefix":
            self.pending.extend(b"\x06")
            self.state = "await_ident"
        elif data == b"\x02" and self.state == "await_ident":
            self.pending.extend(self.ident)
            self.state = "await_ident_ack"
        elif data == b"\x06" and self.state in ("await_ident_ack", "await_read_ack"):
            self.pending.extend(b"\x06")
            self.state = "programming"
        elif data[:1] in (b"R", b"W") and self.state == "programming":
            command, address, length = struct.unpack(">cHb", data[:4])
            if length != 8 or address % 8 or address + length > len(self.memory):
                raise AssertionError("Invalid block geometry")
            if command == b"R":
                if len(data) != 4:
                    raise AssertionError("Unexpected read payload")
                self.read_addresses.append(address)
                self.pending.extend(b"W" + data[1:4] + self.memory[address : address + length])
                self.state = "await_read_ack"
            else:
                if len(data) != 4 + length:
                    raise AssertionError("Unexpected write payload")
                self.write_addresses.append(address)
                self.memory[address : address + length] = data[4:]
                self.pending.extend(b"\x15" if address == self.fail_write_ack_address else b"\x06")
        elif data == b"E" and self.state == "programming":
            if self.pending:
                raise AssertionError("Exit before consuming response")
            self.state = "idle"
            self.ends += 1
            # Observed on physical R01: the driver sends E but does not consume F.
            # Preserve the byte across close/open so explicit input draining is tested.
            self.pending.extend(b"F")
        else:
            raise AssertionError(f"Unexpected command {data!r} in state {self.state}")
        return len(data)

    def read(self, size):
        if not self.is_open:
            raise AssertionError("Read on closed serial")
        result = bytes(self.pending[:size])
        del self.pending[:size]
        if len(result) != size:
            raise AssertionError(f"Driver requested {size} bytes; emulator had {len(result)}")
        return result


class RealDriverTransportTests(unittest.TestCase):
    def test_real_driver_download_build_upload_readback_with_exit_reply(self):
        original = synthetic_raw()
        endpoint = H777SerialEmulator(original)
        with mock.patch.object(core, "_LoggingSerial", side_effect=endpoint.factory), \
                mock.patch("chirp.drivers.h777.time.sleep") as sleeps:
            with core.RadioConnection("/dev/cu.usbserial-emulated") as connection:
                downloaded = connection.download()
                self.assertEqual(downloaded, original)
                profile = core.profile_from_raw(downloaded)
                candidate = core.build_candidate(downloaded, profile)
                connection.upload(candidate)
                readback = connection.download()
                core.verify_raw(readback, candidate)

        self.assertEqual(bytes(endpoint.memory), candidate)
        self.assertFalse(endpoint.is_open)
        self.assertEqual(endpoint.state, "idle")
        self.assertEqual(endpoint.pending, bytearray(b"F"))
        self.assertEqual(endpoint.ends, 3)
        self.assertEqual(endpoint.open_count, 3)
        self.assertEqual(endpoint.close_count, 3)
        self.assertEqual(endpoint.drained, [b"", b"F", b"F"])
        self.assertEqual(endpoint.read_addresses, list(range(0, 0x3E0, 8)) * 2)
        self.assertEqual(endpoint.write_addresses,
                         list(range(0, 0x110, 8)) + list(range(0x380, 0x3E0, 8))
                         + list(range(0x2B0, 0x2C0, 8)))
        self.assertEqual(endpoint.options["baudrate"], 9600)
        self.assertEqual(endpoint.options["timeout"], 0.5)
        self.assertFalse(endpoint.options["rtscts"])
        if platforms.is_windows():
            self.assertNotIn("exclusive", endpoint.options)
        else:
            self.assertTrue(endpoint.options["exclusive"])
        self.assertTrue(any(call.args[0] > 4 for call in sleeps.call_args_list),
                        "Post-upload stabilization must retain the observed five-second wait")
        self.assertEqual(endpoint.opened_with,
                         {"port": "/dev/cu.usbserial-emulated", "rts": True, "dtr": True})

    def test_wrong_family_is_rejected_before_any_memory_write(self):
        original = synthetic_raw()
        for action in ("download", "upload"):
            with self.subTest(action=action):
                endpoint = H777SerialEmulator(original, ident=b"BADMODEL")
                with mock.patch.object(core, "_LoggingSerial", side_effect=endpoint.factory), \
                        mock.patch("chirp.drivers.h777.time.sleep"), \
                        self.assertLogs("chirp.drivers.h777", level="WARNING"):
                    with self.assertRaises(core.RadioLoteError):
                        with core.RadioConnection("/dev/cu.usbserial-emulated") as connection:
                            if action == "download":
                                connection.download()
                            else:
                                connection.upload(original)
                self.assertFalse(endpoint.is_open)
                self.assertEqual(endpoint.write_addresses, [])
                self.assertFalse(any(command[:1] == b"W" for command in endpoint.commands))
                self.assertEqual(bytes(endpoint.memory), original)

    def test_failed_write_ack_closes_serial_and_never_retries_the_block(self):
        original = synthetic_raw()
        candidate = core.build_candidate(original, core.profile_from_raw(original))
        endpoint = H777SerialEmulator(original, fail_write_ack_address=0x10)
        with mock.patch.object(core, "_LoggingSerial", side_effect=endpoint.factory), \
                mock.patch("chirp.drivers.h777.time.sleep"):
            with self.assertRaises(core.RadioLoteError):
                with core.RadioConnection("/dev/cu.usbserial-emulated") as connection:
                    connection.upload(candidate)
        self.assertFalse(endpoint.is_open)
        self.assertEqual(endpoint.open_count, 1)
        self.assertEqual(endpoint.close_count, 1)
        self.assertEqual(endpoint.write_addresses, [0x00, 0x08, 0x10])
        self.assertEqual(endpoint.commands.count(b"PROGRAM"), 1)
        self.assertEqual(bytes(endpoint.memory[0x18:]), original[0x18:])

    def test_windows_com_transport_omits_posix_exclusive_option(self):
        endpoint = H777SerialEmulator(synthetic_raw())
        with mock.patch.object(platforms, "is_windows", return_value=True), \
                mock.patch.object(core, "_LoggingSerial", side_effect=endpoint.factory), \
                mock.patch("chirp.drivers.h777.time.sleep"):
            with core.RadioConnection("COM12") as connection:
                self.assertEqual(connection.download(), synthetic_raw())
        self.assertNotIn("exclusive", endpoint.options)
        self.assertEqual(endpoint.opened_with["port"], "COM12")
        self.assertEqual(endpoint.close_count, 1)

    def test_network_serial_urls_are_rejected_before_serial_creation(self):
        with mock.patch.object(core, "_LoggingSerial") as serial:
            for url in ("socket://localhost:9999", "rfc2217://radio:123", "loop://"):
                with self.subTest(url=url), self.assertRaises(core.RadioLoteError):
                    with core.RadioConnection(url):
                        pass
            serial.assert_not_called()

    def test_progress_reports_driver_status_without_ui_accessing_the_port(self):
        endpoint = H777SerialEmulator(synthetic_raw())
        events = []
        with mock.patch.object(core, "_LoggingSerial", side_effect=endpoint.factory), \
                mock.patch("chirp.drivers.h777.time.sleep"):
            with core.RadioConnection("/dev/ttyUSB0", on_progress=events.append) as connection:
                connection.download()
        self.assertEqual(len(events), core.RAW_SIZE // 8)
        self.assertEqual(events[-1]["current"], core.RAW_SIZE)
        self.assertEqual(events[-1]["total"], core.RAW_SIZE)
        self.assertTrue(events[-1]["message"])

    def test_progress_presentation_failure_does_not_abort_an_upload(self):
        endpoint = H777SerialEmulator(synthetic_raw())
        events = []

        def progress(event):
            events.append(event)
            if len(events) == 1:
                raise RuntimeError("UI unavailable")

        with mock.patch.object(core, "_LoggingSerial", side_effect=endpoint.factory), \
                mock.patch("chirp.drivers.h777.time.sleep"), \
                self.assertLogs("radio_lote.core", level="ERROR"):
            with core.RadioConnection("/dev/ttyUSB0", on_progress=progress) as connection:
                connection.upload(synthetic_raw())
        self.assertEqual(endpoint.ends, 1)
        self.assertGreater(len(events), 1)
        self.assertEqual(endpoint.commands.count(b"PROGRAM"), 1)


if __name__ == "__main__":
    unittest.main()
