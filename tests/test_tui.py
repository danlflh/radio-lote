"""Headless terminal flows with synthetic images and no serial access."""

import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from textual.widgets import Button, DataTable, Input, Select, Static, TabbedContent

from radio_lote import core
from radio_lote.catalog import Catalog
from radio_lote.storage import Attempt, utc_now
from radio_lote.tui import (
    BatchDetailsScreen, BatchScreen, HomeScreen, LanguageScreen, NewBatchScreen,
    ProfileEditorScreen, PromptScreen, RadioLoteApp, SettingsScreen, frequency_hz,
)
from tests.test_core import synthetic_raw


class FakeOperations:
    def __init__(self, catalog, on_event):
        self.catalog, self.on_event = catalog, on_event
        self.calls = []
        self.fail_next = False
        self.hold = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.stop_requested = False

    def reset_stop(self):
        self.stop_requested = False

    def request_stop(self):
        self.stop_requested = True
        self.release.set()

    def program(self, batch_id, radio_id, port=None):
        self.calls.append(("program", batch_id, radio_id, port))
        self.entered.set()
        self.on_event({"stage": "reading", "message": "Synthetic read", "current": 1, "total": 2})
        if self.hold:
            self.release.wait(timeout=5)
        if self.stop_requested:
            raise core.RadioLoteError("Synthetic cooperative pause")
        if self.fail_next:
            self.fail_next = False
            raise core.RadioLoteError("Synthetic read failed")
        attempt = Attempt.create(self.catalog.root / "batches", batch_id, radio_id, "programacao", port)
        attempt.save_raw("original", synthetic_raw())
        attempt.update("verificado", write_started=True, verified_at=utc_now())
        return attempt

    def backup(self, batch_id, radio_id, port=None):
        self.calls.append(("backup", batch_id, radio_id, port))
        attempt = Attempt.create(self.catalog.root / "batches", batch_id, radio_id, "backup", port)
        attempt.save_raw("original", synthetic_raw())
        return attempt

    def restore(self, batch_id, attempt_id, radio_id, port=None):
        self.calls.append(("restore", batch_id, attempt_id, radio_id, port))
        return Attempt.load(self.catalog.attempt_path(batch_id, attempt_id))

    def verify(self, batch_id, attempt_id, radio_id, port=None):
        self.calls.append(("verify", batch_id, attempt_id, radio_id, port))
        return Attempt.load(self.catalog.attempt_path(batch_id, attempt_id))


class TerminalFlows(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = Catalog(Path(self.temp.name))
        self.profile = self.catalog.save_profile("Synthetic", core.make_profile(synthetic_raw(), new_groups=False))
        self.instances = []
        guard = patch.object(core, "RadioConnection", side_effect=AssertionError("Real serial access forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def operations(self, catalog, on_event):
        instance = FakeOperations(catalog, on_event)
        self.instances.append(instance)
        return instance

    def app(self, lang="pt"):
        return RadioLoteApp(catalog=self.catalog, operations_factory=self.operations, lang=lang)

    def batch(self, name="Synthetic batch"):
        return self.catalog.create_batch(name, quantity=10, profile_id=self.profile["id"])

    async def open_batch(self, app, pilot, batch):
        app.push_screen(BatchScreen(batch["id"]))
        await pilot.pause()
        return app.screen

    async def finish_operation(self, app, pilot):
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not app.operation_busy:
                await pilot.pause()
                return
        self.fail("Fake operation did not finish")

    async def test_home_sizes_search_keyboard_and_no_automatic_hardware(self):
        self.batch("Match officials")
        self.batch("Club staff")
        for size in ((80, 24), (120, 40)):
            with self.subTest(size=size):
                app = self.app()
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    table = app.screen.query_one("#batch-table", DataTable)
                    self.assertEqual(table.row_count, 2)
                    self.assertGreater(table.size.height, 3)
                    await pilot.press("escape")
                    self.assertIsInstance(app.screen, HomeScreen)
                    app.screen.query_one("#batch-search", Input).value = "officials"
                    await pilot.pause()
                    self.assertEqual(table.row_count, 1)
                    self.assertEqual(table.get_row_at(0)[2], "Pronto")
                    table.focus()
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, BatchScreen)
                    self.assertEqual(self.instances[-1].calls, [])
                    self.assertLess(app.screen.query_one("#operation-program").region.bottom, size[1])
                    await pilot.press("escape")
                    self.assertIsInstance(app.screen, HomeScreen)

    async def test_language_first_run_persists_english(self):
        app = self.app(lang=None)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertIsInstance(app.screen, LanguageScreen)
            await pilot.click("#language-en")
            await pilot.pause()
            self.assertEqual(self.catalog.settings()["lang"], "en")
            self.assertIsInstance(app.screen, HomeScreen)
            self.assertEqual(str(app.screen.query_one("#home-new_batch", Button).label), "New batch")

    async def test_create_from_saved_profile_without_touching_radio(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("n")
            await pilot.pause()
            self.assertIsInstance(app.screen, NewBatchScreen)
            app.screen.query_one("#new-name", Input).value = "Match 2"
            app.screen.query_one("#new-client", Input).value = "Club"
            app.screen.query_one("#new-quantity", Input).value = "12"
            app.screen.query_one("#new-source", Select).value = "saved"
            app.screen.query_one("#new-profile", Select).value = self.profile["id"]
            await pilot.pause()
            button = app.screen.query_one("#new-create", Button)
            button.focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, BatchScreen)
            batch = self.catalog.list_batches()[0]
            self.assertEqual((batch["name"], batch["client"], batch["quantity"]), ("Match 2", "Club", 12))
            self.assertEqual(batch["profile_id"], self.profile["id"])
            self.assertEqual(self.instances[-1].calls, [])

    async def test_failed_read_keeps_r05_and_retry_repeats_same_operation(self):
        app = self.app()
        batch = self.batch()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await self.open_batch(app, pilot, batch)
            screen.query_one("#radio-label", Input).value = "R05"
            self.instances[-1].fail_next = True
            await pilot.press("enter")
            await self.finish_operation(app, pilot)
            self.assertEqual(screen.query_one("#radio-label", Input).value, "R05")
            self.assertFalse(screen.query_one("#operation-retry", Button).disabled)
            await pilot.click("#operation-retry")
            await self.finish_operation(app, pilot)
            self.assertEqual([call[2] for call in self.instances[-1].calls], ["R05", "R05"])
            self.assertEqual(self.catalog.list_radios(batch["id"])[0]["status"], "verified")

    async def test_one_worker_and_cooperative_pause_do_not_exit(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await self.open_batch(app, pilot, self.batch())
            fake = self.instances[-1]
            fake.hold = True
            screen.start_operation("program")
            screen.start_operation("program")
            for _ in range(50):
                await asyncio.sleep(.01)
                if fake.entered.is_set():
                    break
            self.assertTrue(app.operation_busy)
            self.assertEqual(len(fake.calls), 1)
            await pilot.press("ctrl+q")
            await self.finish_operation(app, pilot)
            self.assertTrue(fake.stop_requested)
            self.assertIs(app.screen, screen)
            self.assertEqual(screen.query_one("#radio-label", Input).value, "R01")

    async def test_restore_requires_matching_typed_label_and_shows_backup(self):
        app = self.app()
        batch = self.batch()
        attempt = Attempt.create(self.catalog.root / "batches", batch["id"], "R05", "backup", None)
        attempt.save_raw("original", synthetic_raw())
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await self.open_batch(app, pilot, batch)
            await pilot.click("#--content-tab-attempts-pane")
            await pilot.pause()
            await pilot.click("#attempt-restore")
            await pilot.pause()
            self.assertIsInstance(app.screen, PromptScreen)
            self.assertIn("462.125000", app.screen.hint)
            app.screen.query_one("#prompt-value", Input).value = "R04"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, PromptScreen)
            self.assertEqual(self.instances[-1].calls, [])
            app.screen.query_one("#prompt-value", Input).value = "R05"
            await pilot.press("enter")
            await self.finish_operation(app, pilot)
            self.assertEqual(self.instances[-1].calls[0][:4], ("restore", batch["id"], attempt.path.name, "R05"))

    async def test_ctrl_c_requests_pause_even_with_input_focused(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await self.open_batch(app, pilot, self.batch())
            fake = self.instances[-1]
            fake.hold = True
            screen.start_operation("program")
            for _ in range(50):
                await asyncio.sleep(.01)
                if fake.entered.is_set():
                    break
            await pilot.press("ctrl+c")
            await self.finish_operation(app, pilot)
            self.assertTrue(fake.stop_requested)
            self.assertIs(app.screen, screen)

    async def test_quantity_reached_leaves_empty_label_and_completion_hint(self):
        app = self.app()
        batch = self.catalog.create_batch("One radio", quantity=1, profile_id=self.profile["id"])
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await self.open_batch(app, pilot, batch)
            await pilot.press("enter")
            await self.finish_operation(app, pilot)
            self.assertIsNone(self.catalog.get_batch(batch["id"])["next_radio"])
            self.assertEqual(screen.query_one("#radio-label", Input).value, "")
            self.assertIn("Concluir", str(screen.query_one("#operation-help", Static).render()))

    async def test_pending_readback_uses_verify_button_without_programming(self):
        app = self.app()
        batch = self.batch()
        attempt = Attempt.create(self.catalog.root / "batches", batch["id"], "R05", "programacao", None)
        attempt.save_raw("original", synthetic_raw())
        attempt.update("falhou", write_started=True, failed_stage="conferindo")
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await self.open_batch(app, pilot, batch)
            await pilot.click("#--content-tab-attempts-pane")
            await pilot.pause()
            await pilot.click("#attempt-verify")
            await self.finish_operation(app, pilot)
            self.assertEqual(self.instances[-1].calls[0][0], "verify")
            self.assertEqual(len(self.instances[-1].calls), 1)

    async def test_advanced_editor_saves_revision_with_decimal_comma_and_label(self):
        app = self.app()
        batch = self.batch()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(ProfileEditorScreen(batch["id"], self.profile["id"]))
            await pilot.pause()
            editor = app.screen
            editor.query_one("#edit-frequency", Input).value = "462,150000"
            editor.query_one("#edit-label", Input).value = "Referees"
            editor.query_one("#edit-tone", Select).value = "TSQL"
            editor.query_one("#edit-ctcss", Input).value = "88,5"
            editor.apply_channel()
            self.assertEqual(editor.config["channels"][0]["rx_hz"], 462_150_000)
            self.assertEqual(editor.config["channels"][0]["ctcss"], 88.5)
            editor.save_profile()
            await pilot.pause()
            profile_id = self.catalog.get_batch(batch["id"])["profile_id"]
            self.assertNotEqual(profile_id, self.profile["id"])
            self.assertEqual(self.catalog.get_profile(profile_id)["config"]["channels"][0]["label"], "Referees")
            self.assertEqual(self.catalog.get_profile(self.profile["id"])["config"]["channels"][0]["rx_hz"], 462_125_000)

    async def test_batch_details_edit_metadata_and_quantity(self):
        app = self.app()
        batch = self.batch()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(BatchDetailsScreen(batch["id"]))
            await pilot.pause()
            app.screen.query_one("#batch-edit-name", Input).value = "Final"
            app.screen.query_one("#batch-edit-quantity", Input).value = "16"
            app.screen.query_one("#batch-edit-save", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            updated = self.catalog.get_batch(batch["id"])
            self.assertEqual((updated["name"], updated["quantity"]), ("Final", 16))

    async def test_settings_accept_auto_preference_and_no_color_env(self):
        self.catalog.save_settings(theme="auto")
        with patch.dict("os.environ", {"NO_COLOR": "1"}):
            app = self.app()
            async with app.run_test(size=(80, 24)) as pilot:
                app.push_screen(SettingsScreen())
                await pilot.pause()
                self.assertTrue(app.has_class("no-color"))
                self.assertEqual(app.screen.query_one("#theme-choice", Select).value, "dark")

    async def test_complete_then_reopen_batch_from_contextual_button(self):
        batch = self.catalog.create_batch("Finished batch", quantity=1, profile_id=self.profile["id"])
        attempt = Attempt.create(self.catalog.root / "batches", batch["id"], "R01", "programacao", None)
        attempt.save_raw("original", synthetic_raw())
        attempt.update("verificado", write_started=True, verified_at=utc_now(), voice_test="aprovado")
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await self.open_batch(app, pilot, batch)
            self.assertFalse(screen.query_one("#batch-reopen", Button).display)
            screen.query_one("#batch-complete", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(self.catalog.get_batch(batch["id"])["status"], "completed")
            self.assertTrue(screen.query_one("#batch-reopen", Button).display)
            self.assertFalse(screen.query_one("#batch-complete", Button).display)
            screen.query_one("#batch-reopen", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(self.catalog.get_batch(batch["id"])["status"], "ready")
            self.assertFalse(screen.query_one("#batch-reopen", Button).display)
            self.assertTrue(screen.query_one("#batch-complete", Button).display)
            self.assertEqual(self.instances[-1].calls, [])


class FrequencyParsing(unittest.TestCase):
    def test_exact_mhz_and_invalid_values(self):
        self.assertEqual(frequency_hz("462,125"), 462_125_000)
        for value in ("NaN", "Infinity", "462.1250001", "bad"):
            with self.subTest(value=value), self.assertRaises(core.RadioLoteError):
                frequency_hz(value)


if __name__ == "__main__":
    unittest.main()
