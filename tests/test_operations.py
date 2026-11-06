"""Hardware workflow recovery with durable files and a memory-only connection."""

from pathlib import Path
import signal
import tempfile
import unittest
from unittest import mock

from radio_lote import core
from radio_lote.catalog import Catalog
from radio_lote.operations import Operations, StopRequested
from radio_lote.service import OperationFailed
from radio_lote.storage import Attempt
from tests.test_core import synthetic_raw


class MemoryCable:
    def __init__(self, raw):
        self.raw = raw
        self.opens = 0
        self.closes = 0
        self.reads = 0
        self.writes = []
        self.after_read = None
        self.before_write = None
        self.after_write_error = None

    def __call__(self, port):
        owner = self

        class Connection:
            def __enter__(self):
                owner.opens += 1
                return self

            def __exit__(self, *args):
                owner.closes += 1

            def download(self):
                owner.reads += 1
                result = owner.raw
                if owner.after_read:
                    owner.after_read()
                return result

            def upload(self, raw):
                if owner.before_write:
                    owner.before_write(raw)
                owner.writes.append(raw)
                owner.raw = raw
                if owner.after_write_error:
                    error, owner.after_write_error = owner.after_write_error, None
                    raise error

        return Connection()


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.catalog = Catalog(Path(self.temporary.name) / "catalog")
        self.raw = synthetic_raw()
        self.cable = MemoryCable(self.raw)
        self.config = core.make_profile(self.raw, client="fixture")
        self.profile = self.catalog.save_profile("Reference", self.config)
        self.batch = self.catalog.create_batch("Batch", profile_id=self.profile["id"])
        self.events = []
        self.operations = self.fresh_operations()

    def fresh_operations(self):
        return Operations(Catalog(self.catalog.root), connection_factory=self.cable, on_event=self.events.append)

    def test_program_requires_durable_original_and_prepared_before_first_write(self):
        observed = []

        def before_write(expected):
            record = self.catalog.list_attempts(self.batch["id"])[-1]
            attempt = Attempt.load(Path(record["path"]))
            self.assertEqual(attempt.read_raw("original"), self.raw)
            self.assertEqual(attempt.read_raw("preparada"), expected)
            self.assertTrue(attempt.record["write_started"])
            self.assertEqual(attempt.record["status"], "gravacao_iniciada")
            observed.append(attempt.path)

        self.cable.before_write = before_write
        attempt = self.operations.program(self.batch["id"], "R01")
        self.assertEqual(observed, [attempt.path])
        self.assertEqual(self.cable.reads, 2)
        self.assertEqual(len(self.cable.writes), 1)
        self.assertEqual(attempt.record["status"], "verificado")
        self.assertEqual(self.catalog.list_radios(self.batch["id"])[0]["status"], "verified")
        self.assertEqual(self.events[-1]["current"], 100)

    def test_restart_after_interrupted_upload_recovers_by_reading_only(self):
        self.cable.after_write_error = KeyboardInterrupt()
        with self.assertRaises(OperationFailed) as raised:
            self.operations.program(self.batch["id"], "R01")
        previous = raised.exception.attempt
        self.assertEqual(len(self.cable.writes), 1)
        self.assertEqual(self.catalog.list_radios(self.batch["id"])[0]["status"], "recovery_required")
        # Simulate a fresh process: all Operations state is discarded; only disk survives.
        resumed = self.fresh_operations().program(self.batch["id"], "R01")
        self.assertEqual(resumed.path, previous.path)
        self.assertEqual(len(self.cable.writes), 1, "Recovery must never replay the interrupted upload")
        self.assertEqual(len(self.catalog.list_attempts(self.batch["id"])), 1)
        self.assertEqual(resumed.record["status"], "verificado")
        self.assertEqual(self.cable.opens, self.cable.closes)

    def test_restart_with_mismatched_radio_stays_in_recovery_without_rewriting(self):
        self.cable.after_write_error = RuntimeError("disconnected after write")
        with self.assertRaises(OperationFailed):
            self.operations.program(self.batch["id"], "R01")
        different = bytearray(self.cable.raw)
        different[0x2B9] ^= 1
        self.cable.raw = bytes(different)
        with self.assertRaises(OperationFailed):
            self.fresh_operations().program(self.batch["id"], "R01")
        self.assertEqual(len(self.cable.writes), 1)
        self.assertEqual(self.catalog.list_radios(self.batch["id"])[0]["status"], "recovery_required")

    def test_stop_after_backup_prevents_write_and_next_run_can_continue(self):
        self.cable.after_read = self.operations.request_stop
        with self.assertRaises(OperationFailed) as raised:
            self.operations.program(self.batch["id"], "R01")
        self.assertIsInstance(raised.exception.__cause__, StopRequested)
        self.assertEqual(self.cable.writes, [])
        self.assertEqual(raised.exception.attempt.read_raw("original"), self.raw)
        self.assertEqual(raised.exception.attempt.record["status"], "pausado")
        self.cable.after_read = None
        self.fresh_operations().program(self.batch["id"], "R01")
        self.assertEqual(len(self.cable.writes), 1)

    def test_stop_requested_during_upload_defers_to_safe_boundary_then_resume_reads(self):
        self.cable.before_write = lambda raw: self.operations.request_stop()
        with self.assertRaises(OperationFailed) as raised:
            self.operations.program(self.batch["id"], "R01")
        self.assertIsInstance(raised.exception.__cause__, StopRequested)
        self.assertEqual(len(self.cable.writes), 1)
        self.assertEqual(self.catalog.list_radios(self.batch["id"])[0]["status"], "recovery_required")
        self.cable.before_write = None
        self.fresh_operations().program(self.batch["id"], "R01")
        self.assertEqual(len(self.cable.writes), 1)

    def test_sigint_handler_finishes_upload_then_pauses_before_readback(self):
        original_handler = signal.getsignal(signal.SIGINT)

        def interrupt_during_write(raw):
            handler = signal.getsignal(signal.SIGINT)
            self.assertTrue(callable(handler))
            handler(signal.SIGINT, None)

        self.cable.before_write = interrupt_during_write
        with self.assertRaises(OperationFailed) as raised:
            self.operations.program(self.batch["id"], "R01")
        self.assertIsInstance(raised.exception.__cause__, StopRequested)
        self.assertIs(signal.getsignal(signal.SIGINT), original_handler)
        self.assertEqual(len(self.cable.writes), 1)
        self.assertEqual(self.cable.reads, 1, "Readback must wait until the operator resumes")
        self.assertEqual(self.catalog.list_radios(self.batch["id"])[0]["status"], "recovery_required")
        self.cable.before_write = None
        self.fresh_operations().program(self.batch["id"], "R01")
        self.assertEqual(len(self.cable.writes), 1)

    def test_archived_batch_blocks_program_but_allows_read_and_explicit_restore(self):
        programmed = self.operations.program(self.batch["id"], "R01")
        configured = self.cable.raw
        self.catalog.archive_batch(self.batch["id"])
        opens = self.cable.opens
        with self.assertRaises(core.RadioLoteError):
            self.operations.program(self.batch["id"], "R02")
        self.assertEqual(self.cable.opens, opens)
        self.operations.verify(self.batch["id"], programmed.path.name, "R01")
        self.assertEqual(len(self.cable.writes), 1)
        restored = self.operations.restore(self.batch["id"], programmed.path.name, "R01")
        self.assertEqual(restored.read_raw("original"), configured)
        self.assertEqual(self.cable.raw, self.raw)
        self.assertEqual(len(self.cable.writes), 2)
        self.assertEqual(self.catalog.get_batch(self.batch["id"])["status"], "archived")
        self.assertEqual(self.catalog.list_radios(self.batch["id"])[0]["status"], "restored")

    def test_backup_only_and_wrong_label_restore_never_write(self):
        backup = self.operations.backup(self.batch["id"], "R01")
        self.assertEqual(backup.read_raw("original"), self.raw)
        self.assertEqual(self.cable.writes, [])
        opens = self.cable.opens
        with self.assertRaises(core.RadioLoteError):
            self.operations.restore(self.batch["id"], backup.path.name, "R02")
        self.assertEqual(self.cable.opens, opens)

    def test_standalone_backup_can_restore_and_modified_backup_is_rejected_before_open(self):
        backup = self.operations.backup(self.batch["id"], "R01")
        configured = core.build_candidate(self.raw, self.config)
        self.cable.raw = configured
        restored = self.operations.restore(self.batch["id"], backup.path.name, "R01")
        self.assertEqual(self.cable.raw, self.raw)
        self.assertEqual(restored.read_raw("original"), configured)
        original = backup.path / "original.img"
        original.write_bytes(bytes([original.read_bytes()[0] ^ 1]) + original.read_bytes()[1:])
        opens = self.cable.opens
        with self.assertRaises(core.RadioLoteError):
            self.operations.restore(self.batch["id"], backup.path.name, "R01")
        self.assertEqual(self.cable.opens, opens)
        self.assertEqual(len(self.cable.writes), 1)

    def test_restore_retry_after_failed_readback_only_verifies_existing_restore(self):
        programmed = self.operations.program(self.batch["id"], "R01")
        fail_on_read = self.cable.reads + 2  # Current-state backup succeeds; restoration readback fails.

        def fail_readback_once():
            if self.cable.reads == fail_on_read:
                self.cable.after_read = None
                raise RuntimeError("readback connection lost")

        self.cable.after_read = fail_readback_once
        with self.assertRaises(OperationFailed) as raised:
            self.operations.restore(self.batch["id"], programmed.path.name, "R01")
        pending = raised.exception.attempt
        self.assertEqual(len(self.cable.writes), 2)
        self.assertEqual(self.catalog.list_radios(self.batch["id"])[0]["status"], "recovery_required")
        restored = self.fresh_operations().restore(self.batch["id"], programmed.path.name, "R01")
        self.assertEqual(restored.path, pending.path)
        self.assertEqual(len(self.cable.writes), 2, "Retry must not replay the already completed restoration upload")
        self.assertEqual(len(self.catalog.list_attempts(self.batch["id"])), 2)
        self.assertEqual(restored.record["status"], "verificado")

    def test_different_batch_identity_in_attempt_blocks_explicit_recovery_before_open(self):
        backup = self.operations.backup(self.batch["id"], "R01")
        backup.update("backup_conferido", client="0" * 32)
        opens = self.cable.opens
        for action in (self.operations.restore, self.operations.verify):
            with self.subTest(action=action.__name__), self.assertRaises(core.RadioLoteError):
                action(self.batch["id"], backup.path.name, "R01")
        self.assertEqual(self.cable.opens, opens)

    def test_reference_capture_reads_once_and_persists_one_shared_profile(self):
        draft = self.catalog.create_batch("New reference", group_count=3)
        with mock.patch.object(core.secrets.SystemRandom, "sample", return_value=[23, 25, 26]) as sample:
            profile = self.operations.capture_profile(draft["id"], "Captured", count=3)
        sample.assert_called_once()
        self.assertEqual(self.cable.reads, 1)
        self.assertEqual(self.cable.writes, [])
        self.assertEqual([channel["dcs"] for channel in profile["config"]["channels"]], [23, 25, 26])
        self.assertEqual(self.catalog.batch_profile(draft["id"])["id"], profile["id"])

    def test_interface_event_failure_does_not_change_hardware_result(self):
        calls = []

        def broken_interface(event):
            calls.append(event)
            if len(calls) == 1:
                raise RuntimeError("view disposed")

        operations = Operations(self.catalog, on_event=broken_interface, connection_factory=self.cable)
        with self.assertLogs("radio_lote.operations", level="ERROR"):
            attempt = operations.program(self.batch["id"], "R01")
        self.assertEqual(attempt.record["status"], "verificado")
        self.assertEqual(len(self.cable.writes), 1)


if __name__ == "__main__":
    unittest.main()
