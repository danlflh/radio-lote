"""Batch history, migration and portable bundles using synthetic images only."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from radio_lote import core
from radio_lote.catalog import Catalog, config_hash
from radio_lote.storage import Attempt, sha256, write_json
from tests.test_core import synthetic_raw


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.catalog = Catalog(self.base / "catalog")
        self.raw = synthetic_raw()
        self.config = core.make_profile(self.raw, client="fixture", new_groups=False)
        self.profile = self.catalog.save_profile("Reference", self.config)

    def batch(self, name="First batch", client="Shared client"):
        return self.catalog.create_batch(name, client=client, profile_id=self.profile["id"])

    def attempt(self, batch, label="R01", *, kind="programacao", status="verificado",
                when="2026-01-01T10:00:00+00:00", write=True):
        attempt = Attempt.create(self.catalog.root / "batches", batch["id"], label, kind, "fixture-only")
        attempt.save_raw("original", self.raw)
        attempt.save_raw("preparada", core.build_candidate(self.raw, self.config))
        values = {"write_started": write}
        if write:
            values["write_started_at"] = when
        if status == "verificado":
            values.update(verified_at=when, voice_test="pendente")
        attempt.update(status, **values)
        return attempt

    def exported(self):
        batch = self.batch()
        self.attempt(batch)
        path = self.base / "bundle.zip"
        self.catalog.export_batch(batch["id"], path)
        return batch, path

    @staticmethod
    def entries(path):
        with zipfile.ZipFile(path) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def rewrite_bundle(self, source, change, *, refresh_manifest=True):
        entries = self.entries(source)
        change(entries)
        if refresh_manifest:
            manifest = json.loads(entries["manifest.json"])
            manifest["files"] = {name: sha256(blob) for name, blob in entries.items() if name != "manifest.json"}
            entries["manifest.json"] = json.dumps(manifest).encode()
        target = self.base / "altered.zip"
        with zipfile.ZipFile(target, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return target

    def test_same_client_has_independent_batches_and_radio_labels(self):
        first, second = self.batch("First"), self.batch("Second")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["client"], second["client"])
        self.attempt(first)
        self.assertEqual(self.catalog.get_batch(first["id"])["next_radio"], "R02")
        self.assertEqual(self.catalog.get_batch(second["id"])["next_radio"], "R01")
        self.assertEqual(self.catalog.list_radios(second["id"]), [])
        self.attempt(second)
        self.assertEqual(self.catalog.get_batch(second["id"])["counts"]["verified"], 1)

    def test_profile_revisions_are_immutable_and_started_batch_rejects_switch(self):
        first_path = self.catalog.root / "profiles" / f"{self.profile['id']}.json"
        original = first_path.read_bytes()
        revised = copy.deepcopy(self.config)
        revised["channels"][0].update(tone_mode="DTCS", dcs=743)
        second = self.catalog.save_profile("Reference", revised)
        self.assertEqual(second["revision"], 2)
        self.assertNotEqual(second["id"], self.profile["id"])
        self.assertEqual(first_path.read_bytes(), original)
        batch = self.batch()
        self.attempt(batch)
        with self.assertRaises(core.RadioLoteError):
            self.catalog.activate_batch(batch["id"], second["id"])
        self.assertEqual(self.catalog.batch_profile(batch["id"])["id"], self.profile["id"])
        duplicate = self.catalog.duplicate_batch(batch["id"], "New run")
        self.assertEqual(self.catalog.list_radios(duplicate["id"]), [])
        self.catalog.activate_batch(duplicate["id"], second["id"])

    def test_manual_profile_tampering_is_detected(self):
        path = self.catalog.root / "profiles" / f"{self.profile['id']}.json"
        changed = json.loads(path.read_bytes())
        changed["config"]["channels"][0]["rx_hz"] += 10
        path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(core.RadioLoteError):
            self.catalog.get_profile(self.profile["id"])

    def test_latest_physical_operation_controls_crash_recovery_not_stale_history(self):
        batch = self.batch()
        self.attempt(batch)
        crashed = self.attempt(batch, status="gravacao_iniciada", when="2026-01-01T11:00:00+00:00")
        self.assertEqual(self.catalog.list_radios(batch["id"])[0]["status"], "recovery_required")
        # A later read failure never clears uncertainty from an interrupted write.
        self.attempt(batch, status="falhou", when="2026-01-01T12:00:00+00:00", write=False)
        current = self.catalog.list_radios(batch["id"])[0]
        self.assertEqual(current["attempt_id"], crashed.path.name)
        with self.assertRaises(core.RadioLoteError):
            self.catalog.archive_batch(batch["id"])
        self.attempt(batch, when="2026-01-01T13:00:00+00:00")
        self.assertEqual(self.catalog.list_radios(batch["id"])[0]["status"], "verified")
        self.assertEqual(self.catalog.get_batch(batch["id"])["next_radio"], "R02")

    def test_archived_batch_keeps_history_profile_and_export_readable(self):
        batch = self.batch()
        attempt = self.attempt(batch)
        archived = self.catalog.archive_batch(batch["id"])
        self.assertEqual(archived["status"], "archived")
        self.assertEqual(self.catalog.list_batches(), [])
        self.assertEqual(len(self.catalog.list_batches(include_archived=True)), 1)
        self.assertEqual(self.catalog.batch_profile(batch["id"])["id"], self.profile["id"])
        self.assertEqual(Attempt.load(self.catalog.attempt_path(batch["id"], attempt.path.name)).read_raw("original"), self.raw)
        self.assertEqual(len(self.catalog.list_attempts(batch["id"])), 1)
        self.catalog.export_batch(batch["id"], self.base / "archived.zip")
        with self.assertRaises(core.RadioLoteError):
            self.catalog.update_batch(batch["id"], name="Changed")
        with self.assertRaises(core.RadioLoteError):
            self.catalog.activate_batch(batch["id"], self.profile["id"])
        self.assertEqual(self.catalog.archive_batch(batch["id"], False)["status"], "ready")

    def test_completed_count_and_voice_result_remain_separate(self):
        batch = self.catalog.create_batch("Two radios", quantity=2, profile_id=self.profile["id"])
        self.attempt(batch)
        with self.assertRaises(core.RadioLoteError):
            self.catalog.complete_batch(batch["id"])
        self.attempt(batch, "R02")
        completed = self.catalog.complete_batch(batch["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["counts"]["voice"], 0)
        self.catalog.set_voice(batch["id"], "R01", True)
        self.assertEqual(self.catalog.get_batch(batch["id"])["counts"]["voice"], 1)

    def test_expected_quantity_stops_new_labels_but_keeps_existing_recovery_first(self):
        batch = self.catalog.create_batch("Exactly two", quantity=2, profile_id=self.profile["id"])
        self.attempt(batch, "R01")
        self.attempt(batch, "R02")
        self.assertIsNone(self.catalog.get_batch(batch["id"])["next_radio"])
        self.attempt(batch, "R02", status="gravacao_iniciada", when="2026-01-01T11:00:00+00:00")
        self.assertEqual(self.catalog.get_batch(batch["id"])["next_radio"], "R02")

    def test_migration_preserves_source_and_backup_bytes_and_is_idempotent(self):
        source = self.base / "legacy"
        old_profile = core.profile_from_raw(self.raw, client="fixture")
        write_json(source / "fixture" / "perfil.json", old_profile)
        legacy = Attempt.create(source, "fixture", "R01", "programacao", "fixture-only")
        legacy.save_raw("original", self.raw)
        legacy.save_raw("preparada", core.build_candidate(self.raw, old_profile))
        legacy.update("verificado", verified_at="2026-01-01T00:00:00+00:00")
        before = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
        imported = self.catalog.migrate_legacy(source)
        self.assertEqual(len(imported), 1)
        batch_id = imported[0]["id"]
        for relative, blob in before.items():
            self.assertEqual((source / relative).read_bytes(), blob)
        migrated = self.catalog.batch_path(batch_id) / "tentativas" / legacy.path.name
        self.assertEqual((migrated / "original.img").read_bytes(), (legacy.path / "original.img").read_bytes())
        self.assertEqual((migrated / "registro-legado.json").read_bytes(), (legacy.path / "registro.json").read_bytes())
        self.assertEqual(Attempt.load(migrated).read_raw("original"), self.raw)
        self.assertEqual(self.catalog.migrate_legacy(source)[0]["id"], batch_id)
        self.assertEqual(len(self.catalog.list_attempts(batch_id)), 1)

    def test_zip_roundtrip_and_repeated_import_preserve_all_backup_bytes(self):
        batch, archive = self.exported()
        destination = Catalog(self.base / "imported")
        imported = destination.import_batch(archive)
        self.assertEqual(imported["id"], batch["id"])
        self.assertEqual(imported["counts"]["verified"], 1)
        for record in self.catalog.list_attempts(batch["id"]):
            for file in Path(record["path"]).glob("*.img"):
                restored = destination.attempt_path(batch["id"], record["id"]) / file.name
                self.assertEqual(restored.read_bytes(), file.read_bytes())
        self.assertEqual(destination.import_batch(archive)["id"], batch["id"])
        self.assertEqual(len(destination.list_attempts(batch["id"])), 1)

    def test_corrupt_archive_and_image_hash_mismatch_never_publish_batch(self):
        batch, archive = self.exported()

        def corrupt_image(entries):
            name = next(name for name in entries if name.endswith("original.img"))
            entries[name] = bytes([entries[name][0] ^ 1]) + entries[name][1:]

        for refresh in (False, True):
            with self.subTest(refresh=refresh):
                changed = self.rewrite_bundle(archive, corrupt_image, refresh_manifest=refresh)
                destination = Catalog(self.base / f"corrupted-{refresh}")
                with self.assertRaises(core.RadioLoteError):
                    destination.import_batch(changed)
                self.assertFalse(destination.batch_path(batch["id"]).exists())

    def test_path_traversal_archive_is_rejected_before_extracting_outside_staging(self):
        batch, archive = self.exported()
        for unsafe in ("../escaped.txt", "/absolute.txt", "C:escaped.txt", r"folder\escaped.txt"):
            with self.subTest(unsafe=unsafe):
                changed = self.rewrite_bundle(archive, lambda entries: entries.update({unsafe: b"not allowed"}))
                destination = Catalog(self.base / "unsafe")
                with self.assertRaises(core.RadioLoteError):
                    destination.import_batch(changed)
                self.assertFalse(destination.batch_path(batch["id"]).exists())
                self.assertFalse((destination.root / "escaped.txt").exists())

    def test_profile_with_self_consistent_hash_but_unsupported_tones_is_rejected(self):
        batch, archive = self.exported()

        def unsupported_profile(entries):
            profile = json.loads(entries["profile.json"])
            profile["config"]["channels"][0]["tone_mode"] = "Cross"
            profile["config_hash"] = config_hash(profile["config"])
            manifest = json.loads(entries["batch.json"])
            manifest["profile_hash"] = profile["config_hash"]
            entries["profile.json"] = json.dumps(profile).encode()
            entries["batch.json"] = json.dumps(manifest).encode()

        changed = self.rewrite_bundle(archive, unsupported_profile)
        destination = Catalog(self.base / "invalid-profile")
        with self.assertRaises(core.RadioLoteError):
            destination.import_batch(changed)
        self.assertFalse(destination.batch_path(batch["id"]).exists())

    def test_id_collision_does_not_overwrite_existing_batch(self):
        batch, archive = self.exported()
        destination = Catalog(self.base / "collision")
        destination.import_batch(archive)
        path = destination.batch_path(batch["id"]) / "batch.json"
        original = path.read_bytes()

        def collision(entries):
            value = json.loads(entries["batch.json"])
            value["name"] = "Different content with same ID"
            entries["batch.json"] = json.dumps(value).encode()

        changed = self.rewrite_bundle(archive, collision)
        with self.assertRaises(core.RadioLoteError):
            destination.import_batch(changed)
        self.assertEqual(path.read_bytes(), original)

    def test_self_consistent_bundle_with_invalid_attempt_identity_or_schema_is_rejected(self):
        batch, archive = self.exported()
        changes = (
            {"client": "0" * 32},
            {"schema_version": 99},
            {"chirp_revision": "unsupported-revision"},
            {"kind": "unsupported-operation"},
        )
        for index, change in enumerate(changes):
            with self.subTest(change=change):
                def alter(entries):
                    filename = next(name for name in entries if name.endswith("/registro.json"))
                    record = json.loads(entries[filename])
                    record.update(change)
                    entries[filename] = json.dumps(record).encode()

                changed = self.rewrite_bundle(archive, alter)
                destination = Catalog(self.base / f"invalid-attempt-{index}")
                with self.assertRaises(core.RadioLoteError):
                    destination.import_batch(changed)
                self.assertFalse(destination.batch_path(batch["id"]).exists())

    def test_bundle_attempt_directory_must_remain_addressable_after_import(self):
        batch, archive = self.exported()

        def invalid_directory(entries):
            for filename in list(entries):
                if filename.startswith("tentativas/"):
                    parts = filename.split("/")
                    parts[1] = "not an addressable attempt"
                    entries["/".join(parts)] = entries.pop(filename)

        changed = self.rewrite_bundle(archive, invalid_directory)
        destination = Catalog(self.base / "invalid-attempt-directory")
        with self.assertRaises(core.RadioLoteError):
            destination.import_batch(changed)
        self.assertFalse(destination.batch_path(batch["id"]).exists())

    def test_non_object_manifest_is_rejected_as_invalid_bundle(self):
        batch, archive = self.exported()
        changed = self.rewrite_bundle(archive, lambda entries: entries.update({"manifest.json": b"[]"}),
                                      refresh_manifest=False)
        destination = Catalog(self.base / "non-object-manifest")
        with self.assertRaises(core.RadioLoteError):
            destination.import_batch(changed)
        self.assertFalse(destination.batch_path(batch["id"]).exists())

    def test_profile_envelope_must_be_usable_before_importing_into_shared_library(self):
        batch, archive = self.exported()
        for field, value in (("name", None), ("revision", "not-a-number")):
            with self.subTest(field=field):
                def invalid_envelope(entries):
                    profile = json.loads(entries["profile.json"])
                    if value is None:
                        profile.pop(field)
                    else:
                        profile[field] = value
                    entries["profile.json"] = json.dumps(profile).encode()

                changed = self.rewrite_bundle(archive, invalid_envelope)
                destination = Catalog(self.base / f"bad-envelope-{field}")
                with self.assertRaises(core.RadioLoteError):
                    destination.import_batch(changed)
                self.assertFalse(destination.batch_path(batch["id"]).exists())
                self.assertEqual(destination.list_profiles(), [])

    def test_log_continues_after_interrupted_last_entry_without_losing_new_events(self):
        batch = self.batch()
        path = self.catalog.batch_path(batch["id"]) / "events.jsonl"
        with path.open("ab") as handle:
            handle.write(b'{"stage":"partially-written')
        self.assertEqual(self.catalog.logs(batch["id"])[-1]["stage"], "interrupted")
        self.catalog.event(batch["id"], "resumed", "First event after restart")
        self.catalog.event(batch["id"], "ready", "Second event after restart")
        events = self.catalog.logs(batch["id"])
        diagnostics = list(path.parent.glob("*.log"))
        self.assertTrue(any(item.read_bytes() == b'{"stage":"partially-written' for item in diagnostics))
        self.assertEqual([event["message"] for event in events[-2:]],
                         ["First event after restart", "Second event after restart"])

    def test_partial_utf8_at_end_of_log_is_recoverable(self):
        batch = self.batch()
        path = self.catalog.batch_path(batch["id"]) / "events.jsonl"
        fragment = b'{"message":"interrup\xc3'
        with path.open("ab") as handle:
            handle.write(fragment)
        self.assertEqual(self.catalog.logs(batch["id"])[-1]["stage"], "interrupted")
        self.catalog.event(batch["id"], "resumed", "Recovered")
        self.assertEqual(self.catalog.logs(batch["id"])[-1]["message"], "Recovered")
        self.assertTrue(any(item.read_bytes() == fragment for item in path.parent.glob("*.log")))


if __name__ == "__main__":
    unittest.main()
