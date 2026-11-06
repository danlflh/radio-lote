"""Exercise the batch lifecycle using a memory-only cable, never pyserial."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from radio_lote import core, service, storage
from tests.test_core import synthetic_raw


class MemoryCable:
    def __init__(self, raw):
        self.raw = raw
        self.opens = 0
        self.downloads = 0
        self.uploads = []
        self.before_upload = None
        self.after_upload_error = None
        self.readback_override = None

    def __call__(self, port):
        self.port = port
        return self

    def __enter__(self):
        self.opens += 1
        return self

    def __exit__(self, *args):
        return False

    def download(self):
        self.downloads += 1
        if self.uploads and self.readback_override is not None:
            return self.readback_override
        return self.raw

    def upload(self, raw):
        if self.before_upload:
            self.before_upload(raw)
        self.uploads.append(raw)
        self.raw = raw
        error = self.after_upload_error
        self.after_upload_error = None
        if error is not None:
            raise error


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "dados"
        self.original = synthetic_raw()
        self.cable = MemoryCable(self.original)
        self.programmer = service.Programmer(self.root, connection_factory=self.cable)

    def activate(self):
        preparation = self.programmer.prepare("futebol", "R01", "memory-only")
        self.programmer.activate_profile(preparation)
        return preparation

    def latest_attempt(self, kind):
        records = [item for item in self.programmer.records("futebol") if item["kind"] == kind]
        return storage.Attempt.load(Path(records[-1]["path"]))

    def test_prepare_is_read_only_and_program_has_durable_backups_before_upload(self):
        preparation = self.activate()
        self.assertEqual(self.cable.uploads, [])
        self.assertEqual(preparation.read_raw("original"), self.original)
        expected = core.build_candidate(self.original, self.programmer.load_profile("futebol"))
        observed = []

        def inspect_files_before_hardware_write(raw):
            attempt = self.latest_attempt("programacao")
            self.assertEqual(attempt.record["status"], "gravacao_iniciada")
            self.assertTrue(attempt.record["write_started"])
            self.assertEqual(attempt.read_raw("original"), self.original)
            self.assertEqual(attempt.read_raw("preparada"), expected)
            self.assertEqual(raw, expected)
            observed.append(attempt.path)

        self.cable.before_upload = inspect_files_before_hardware_write
        result = self.programmer.program_one("futebol", "R01", "memory-only")
        self.assertEqual(observed, [result.path])
        self.assertNotEqual(preparation.path, result.path)
        self.assertEqual(result.record["status"], "verificado")
        self.assertEqual(result.record["voice_test"], "pendente")
        self.assertEqual(result.read_raw("conferencia-001"), expected)
        self.assertEqual(self.cable.uploads, [expected])
        self.assertEqual(self.cable.downloads, 3)

    def test_readback_mismatch_fails_and_resume_only_reads(self):
        self.activate()
        expected = core.build_candidate(self.original, self.programmer.load_profile("futebol"))
        mismatched = bytearray(expected)
        mismatched[0x2B9] ^= 1
        self.cable.readback_override = bytes(mismatched)
        with self.assertRaises(service.OperationFailed) as failure:
            self.programmer.program_one("futebol", "R01", "memory-only")
        attempt = failure.exception.attempt
        self.assertEqual(attempt.record["status"], "falhou")
        self.assertEqual(attempt.record["failed_stage"], "conferindo")
        self.assertEqual(attempt.read_raw("conferencia-001"), bytes(mismatched))
        self.assertEqual(attempt.read_raw("original"), self.original)

        self.cable.readback_override = None
        uploads_before = list(self.cable.uploads)
        result = self.programmer.verify_attempt(attempt.path, "R01", "memory-only")
        self.assertEqual(self.cable.uploads, uploads_before)
        self.assertEqual(result.record["status"], "verificado")
        self.assertEqual(result.read_raw("conferencia-002"), expected)

    def test_interruption_after_write_keeps_backup_and_resume_never_rewrites(self):
        self.activate()
        self.cable.after_upload_error = KeyboardInterrupt()
        with self.assertRaises(service.OperationFailed) as failure:
            self.programmer.program_one("futebol", "R01", "memory-only")
        attempt = failure.exception.attempt
        self.assertTrue(attempt.record["write_started"])
        self.assertEqual(attempt.record["failed_stage"], "gravacao_iniciada")
        self.assertEqual(attempt.read_raw("original"), self.original)
        self.assertEqual(len(self.cable.uploads), 1)

        result = self.programmer.verify_attempt(attempt.path, "R01", "memory-only")
        self.assertEqual(result.record["status"], "verificado")
        self.assertEqual(len(self.cable.uploads), 1)

    def test_resume_preserves_orphan_readback_file_and_uses_a_new_filename(self):
        self.activate()
        self.cable.after_upload_error = KeyboardInterrupt()
        with self.assertRaises(service.OperationFailed) as failure:
            self.programmer.program_one("futebol", "R01", "memory-only")
        attempt = failure.exception.attempt
        orphan = attempt.path / "conferencia-001.img"
        core.save_image(orphan, self.cable.raw)
        orphan_bytes = orphan.read_bytes()
        self.assertNotIn("conferencia-001", attempt.record["artifacts"])

        result = self.programmer.verify_attempt(attempt.path, "R01", "memory-only")
        self.assertEqual(result.record["status"], "verificado")
        self.assertEqual(orphan.read_bytes(), orphan_bytes)
        self.assertEqual(result.read_raw("conferencia-002"), self.cable.raw)
        self.assertEqual(len(self.cable.uploads), 1)

    def test_backup_save_failure_prevents_upload(self):
        self.activate()
        with mock.patch.object(storage, "save_image", side_effect=OSError("disk full")):
            with self.assertRaises(service.OperationFailed) as failure:
                self.programmer.program_one("futebol", "R01", "memory-only")
        self.assertEqual(self.cable.uploads, [])
        self.assertFalse(failure.exception.attempt.record["write_started"])

    def test_backup_reopen_failure_prevents_upload(self):
        self.activate()
        with mock.patch.object(storage.Attempt, "read_raw", side_effect=core.RadioLoteError("unreadable backup")):
            with self.assertRaises(service.OperationFailed) as failure:
                self.programmer.program_one("futebol", "R01", "memory-only")
        self.assertEqual(self.cable.uploads, [])
        self.assertFalse(failure.exception.attempt.record["write_started"])
        self.assertEqual(core.load_image(failure.exception.attempt.path / "original.img"), self.original)

    def test_restore_saves_current_state_before_writing_original_backup(self):
        self.activate()
        programmed = self.programmer.program_one("futebol", "R01", "memory-only")
        configured = self.cable.raw
        original_backup = (programmed.path / "original.img").read_bytes()
        observed = []

        def inspect_restore_backup(raw):
            attempt = self.latest_attempt("restauracao")
            self.assertEqual(attempt.read_raw("original"), configured)
            self.assertEqual(attempt.read_raw("preparada"), self.original)
            self.assertEqual(raw, self.original)
            observed.append(attempt.path)

        self.cable.before_upload = inspect_restore_backup
        restored = self.programmer.restore(programmed.path, "R01", "memory-only")
        self.assertEqual(observed, [restored.path])
        self.assertEqual(restored.record["status"], "verificado")
        self.assertEqual(self.cable.raw, self.original)
        self.assertEqual((programmed.path / "original.img").read_bytes(), original_backup)
        self.assertEqual(restored.read_raw("original"), configured)
        self.assertEqual(len(self.cable.uploads), 2)

    def test_restored_radio_can_be_programmed_again_and_is_not_counted_as_ready(self):
        self.activate()
        first = self.programmer.program_one("futebol", "R01", "memory-only")
        self.programmer.restore(first.path, "R01", "memory-only")
        self.assertEqual(self.programmer.next_radio_id("futebol"), "R01")
        second = self.programmer.program_one("futebol", "R01", "memory-only")
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(second.record["status"], "verificado")
        self.assertEqual(self.programmer.next_radio_id("futebol"), "R02")
        self.assertEqual(len(self.cable.uploads), 3)

    def test_voice_note_on_old_program_does_not_supersede_later_restore(self):
        self.activate()
        programmed = self.programmer.program_one("futebol", "R01", "memory-only")
        verified_at = programmed.record["verified_at"]
        self.programmer.restore(programmed.path, "R01", "memory-only")
        opens_before = self.cable.opens
        annotated = self.programmer.record_voice_test(programmed.path, "R01", True)
        self.assertEqual(annotated.record["voice_test"], "aprovado")
        self.assertEqual(annotated.record["verified_at"], verified_at)
        self.assertEqual(self.programmer.next_radio_id("futebol"), "R01")
        self.assertEqual(self.cable.opens, opens_before)

    def test_profile_activation_can_resume_after_origin_was_saved(self):
        preparation = self.programmer.prepare("futebol", "R01", "memory-only")
        real_write = service.write_json

        def interrupted_write(path, *args, **kwargs):
            if path.name == "perfil.json":
                raise OSError("interrupted after origin was saved")
            return real_write(path, *args, **kwargs)

        with mock.patch.object(service, "write_json", side_effect=interrupted_write):
            with self.assertRaises((core.RadioLoteError, OSError)):
                self.programmer.activate_profile(preparation)
        self.assertTrue((self.root / "futebol" / "perfil-origem.json").exists())
        self.assertFalse((self.root / "futebol" / "perfil.json").exists())
        activated = self.programmer.activate_profile(preparation)
        self.assertEqual(activated, (self.root / "futebol" / "perfil.json").resolve())
        self.programmer.load_profile("futebol")
        self.assertEqual(self.cable.uploads, [])

    def test_simulation_never_opens_connection_and_cannot_activate_or_restore(self):
        image = Path(self.temporary.name) / "synthetic.img"
        core.save_image(image, self.original)
        factory = mock.Mock(side_effect=AssertionError("Simulation accessed hardware"))
        programmer = service.Programmer(self.root, connection_factory=factory)
        simulated = programmer.simulate(image, "futebol")
        self.assertEqual(simulated.record["status"], "simulacao_concluida")
        self.assertFalse((self.root / "futebol" / "perfil.json").exists())
        with self.assertRaises(core.RadioLoteError):
            programmer.activate_profile(simulated)
        with self.assertRaises(core.RadioLoteError):
            programmer.restore(simulated.path, "R01", "memory-only")
        factory.assert_not_called()

    def test_modified_active_profile_is_rejected_before_connection(self):
        self.activate()
        opens_before = self.cable.opens
        profile_path = self.root / "futebol" / "perfil.json"
        profile = storage.read_json(profile_path)
        profile["channels"][0]["dcs"] = 754
        storage.write_json(profile_path, profile, replace=True)
        with self.assertRaises(core.RadioLoteError):
            self.programmer.program_one("futebol", "R01", "memory-only")
        self.assertEqual(self.cable.opens, opens_before)
        self.assertEqual(self.cable.uploads, [])

    def test_wrong_label_blocks_verification_and_restore_before_connection(self):
        self.activate()
        programmed = self.programmer.program_one("futebol", "R01", "memory-only")
        opens_before = self.cable.opens
        uploads_before = list(self.cable.uploads)
        for method in (self.programmer.verify_attempt, self.programmer.restore):
            with self.subTest(method=method.__name__):
                with self.assertRaises(core.RadioLoteError):
                    method(programmed.path, "R02", "memory-only")
        self.assertEqual(self.cable.opens, opens_before)
        self.assertEqual(self.cable.uploads, uploads_before)

    def test_completed_label_cannot_accidentally_be_reused(self):
        self.activate()
        self.programmer.program_one("futebol", "R01", "memory-only")
        self.assertEqual(self.programmer.next_radio_id("futebol"), "R02")
        opens_before = self.cable.opens
        with self.assertRaises(core.RadioLoteError):
            self.programmer.program_one("futebol", "R01", "memory-only")
        self.assertEqual(self.cable.opens, opens_before)
        self.assertEqual(len(self.cable.uploads), 1)

    def test_already_configured_radio_is_backed_up_and_verified_without_upload(self):
        self.activate()
        configured = core.build_candidate(self.original, self.programmer.load_profile("futebol"))
        self.cable.raw = configured
        result = self.programmer.program_one("futebol", "R01", "memory-only")
        self.assertEqual(result.record["status"], "verificado")
        self.assertFalse(result.record["write_started"])
        self.assertEqual(result.read_raw("original"), configured)
        self.assertEqual(result.read_raw("conferencia-001"), configured)
        self.assertEqual(self.cable.uploads, [])


if __name__ == "__main__":
    unittest.main()
