"""Public CLI wiring and machine-readable output, using only temporary data."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from radio_lote import cli, console, core
from tests.test_core import synthetic_raw


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.serial = self.enterContext(mock.patch.object(
            core, "_LoggingSerial", side_effect=AssertionError("Offline CLI test opened serial")))

    def tearDown(self):
        self.serial.assert_not_called()

    def invoke(self, *arguments, data=None, expected=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(["--dados", str(data or self.data), "--lang", "en", "--json", *arguments])
        self.assertEqual(result, expected, stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def invoke_json(self, *arguments, data=None):
        stdout, stderr = self.invoke(*arguments, data=data)
        self.assertEqual(stderr, "")
        return json.loads(stdout)

    def test_legacy_words_as_batch_names_are_not_dispatched_as_commands(self):
        with mock.patch.object(cli, "legacy_main", side_effect=AssertionError("Wrong legacy dispatch")):
            for name in ("voz", "programar"):
                with self.subTest(name=name):
                    created = self.invoke_json("batch", "create", name)
                    self.assertEqual(created["name"], name)
        listed = self.invoke_json("batch", "list")
        self.assertEqual({batch["name"] for batch in listed}, {"voz", "programar"})

    def test_status_is_json_for_empty_all_and_selected_batches(self):
        self.assertEqual(self.invoke_json("status"), [])
        created = self.invoke_json("batch", "create", "Synthetic event", "--quantity", "2")
        all_batches = self.invoke_json("status")
        self.assertEqual([batch["id"] for batch in all_batches], [created["id"]])
        selected = self.invoke_json("status", "--batch", created["id"])
        self.assertEqual(selected["id"], created["id"])
        self.assertEqual(selected["next_radio"], "R01")
        self.assertEqual(selected["radios"], [])
        self.assertEqual(selected["quantity"], 2)

    def test_profile_import_create_and_bundle_roundtrip_use_the_public_cli(self):
        config = core.make_profile(synthetic_raw(), client="synthetic", new_groups=False)
        profile_path = self.root / "synthetic-profile.json"
        profile_path.write_text(json.dumps(config), encoding="utf-8")
        profile = self.invoke_json("profile", "import", str(profile_path), "--name", "Reference")
        self.assertEqual(profile["config"], config)
        created = self.invoke_json("batch", "create", "Portable batch", "--client", "Synthetic",
                                   "--quantity", "2", "--profile", profile["id"])
        bundle = self.root / "portable.zip"
        exported = self.invoke_json("batch", "export", created["id"], str(bundle))
        self.assertEqual(Path(exported["file"]).resolve(), bundle.resolve())
        self.assertTrue(bundle.is_file())

        destination = self.root / "imported-data"
        imported = self.invoke_json("batch", "import", str(bundle), data=destination)
        self.assertEqual(imported["id"], created["id"])
        shown = self.invoke_json("batch", "show", imported["id"], data=destination)
        self.assertEqual(shown["name"], "Portable batch")
        self.assertEqual(shown["client"], "Synthetic")
        self.assertEqual(shown["profile_id"], profile["id"])
        self.assertEqual(shown["radios"], [])
        copied_profile = self.invoke_json("profile", "show", profile["id"], data=destination)
        self.assertEqual(copied_profile["config"], config)
        self.assertEqual(self.invoke_json("batch", "show", created["id"])["id"], created["id"])

    def test_program_once_keeps_progress_and_success_out_of_json_stdout(self):
        created = self.invoke_json("batch", "create", "One radio")
        calls = []

        class FakeOperations:
            def __init__(self, catalog, on_event):
                self.on_event = on_event

            def reset_stop(self):
                calls.append(("reset",))

            def program(self, batch_id, radio_id, port):
                calls.append(("program", batch_id, radio_id, port))
                self.on_event({"stage": "verified", "message": "Readback complete", "current": 100})
                return SimpleNamespace(path=Path("synthetic-attempt"),
                                       record={"kind": "programacao", "status": "verificado"})

        with mock.patch.object(console, "Operations", FakeOperations), \
                mock.patch("builtins.input", side_effect=AssertionError("Explicit --once must not prompt")):
            stdout, stderr = self.invoke("program", "--batch", created["id"], "--radio", "R05",
                                         "--port", "fixture-port", "--once")
        self.assertEqual(json.loads(stdout), {
            "attempt": "synthetic-attempt", "radio": "R05", "status": "verificado",
        })
        self.assertEqual(calls, [("reset",), ("program", created["id"], "R05", "fixture-port")])
        self.assertIn("Readback complete", stderr)
        self.assertIn("R05: programmed and verified", stderr)

    def test_program_once_failure_exits_without_retry_or_success_output(self):
        created = self.invoke_json("batch", "create", "Interrupted radio")
        fake = mock.Mock(spec=console.Operations)
        fake.program.side_effect = core.RadioLoteError("Synthetic connection failure")
        with mock.patch.object(console, "Operations", return_value=fake), \
                mock.patch("builtins.input", side_effect=AssertionError("Failure must not prompt")):
            stdout, stderr = self.invoke("program", "--batch", created["id"], "--radio", "R05",
                                         "--once", expected=1)
        self.assertEqual(stdout, "")
        self.assertIn("Synthetic connection failure", stderr)
        fake.program.assert_called_once_with(created["id"], "R05", None)
        fake.reset_stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
