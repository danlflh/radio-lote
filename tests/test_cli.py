"""Retry the current labelled radio without repeating a potentially completed write."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from radio_lote import cli, core, service


class BatchRetryTests(unittest.TestCase):
    def setUp(self):
        self.programmer = mock.Mock(spec=service.Programmer)
        self.completed = SimpleNamespace(
            path=Path("/memory-only/completed-R05"),
            record={"radio_id": "R05", "kind": "programacao", "status": "verificado"},
        )
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(redirect_stderr(io.StringIO()))

    def failure(self, stage="lendo_original", write_started=False):
        artifacts = {"original": {}, "preparada": {}} if stage != "lendo_original" else {}
        attempt = SimpleNamespace(
            path=Path("/memory-only/failed-R05"),
            record={
                "radio_id": "R05", "kind": "programacao", "status": "falhou",
                "failed_stage": stage, "write_started": write_started,
                "artifacts": artifacts,
            },
        )
        return service.OperationFailed("Connection interrupted", attempt)

    def test_initial_read_failure_retries_same_label_and_detects_port_again(self):
        self.programmer.program_one.side_effect = [self.failure(), self.completed]
        with mock.patch.object(cli, "ask", return_value="") as ask, \
                mock.patch.object(core, "resolve_port", side_effect=["/dev/cu.usb-one", "/dev/cu.usb-two"]) as resolve:
            result = cli.run_radio_with_retry(self.programmer, "futebol", "R05", None)
        self.assertIs(result, self.completed)
        self.assertEqual(self.programmer.program_one.call_args_list, [
            mock.call("futebol", "R05", "/dev/cu.usb-one"),
            mock.call("futebol", "R05", "/dev/cu.usb-two"),
        ])
        self.assertEqual(resolve.call_args_list, [mock.call(None), mock.call(None)])
        ask.assert_called_once()
        self.programmer.verify_attempt.assert_not_called()
        self.programmer.next_radio_id.assert_not_called()

    def test_readback_failure_retries_verification_even_without_a_new_write(self):
        failure = self.failure(stage="conferindo", write_started=False)
        self.programmer.program_one.side_effect = failure
        self.programmer.verify_attempt.return_value = self.completed
        with mock.patch.object(cli, "ask", return_value=""), \
                mock.patch.object(core, "resolve_port", side_effect=["/dev/cu.usb-one", "/dev/cu.usb-two"]):
            result = cli.run_radio_with_retry(self.programmer, "futebol", "R05", None)
        self.assertIs(result, self.completed)
        self.programmer.program_one.assert_called_once_with("futebol", "R05", "/dev/cu.usb-one")
        self.programmer.verify_attempt.assert_called_once_with(failure.attempt.path, "R05", "/dev/cu.usb-two")

    def test_port_failure_after_write_keeps_verification_only_mode(self):
        failure = self.failure(stage="conferindo", write_started=True)
        self.programmer.program_one.side_effect = failure
        self.programmer.verify_attempt.return_value = self.completed
        with mock.patch.object(cli, "ask", side_effect=["", ""]) as ask, \
                mock.patch.object(core, "resolve_port", side_effect=[
                    "/dev/cu.usb-one", core.RadioLoteError("Cable missing"), "/dev/cu.usb-three",
                ]) as resolve:
            result = cli.run_radio_with_retry(self.programmer, "futebol", "R05", None)
        self.assertIs(result, self.completed)
        self.assertEqual(resolve.call_count, 3)
        self.assertEqual(ask.call_count, 2)
        self.programmer.program_one.assert_called_once_with("futebol", "R05", "/dev/cu.usb-one")
        self.programmer.verify_attempt.assert_called_once_with(failure.attempt.path, "R05", "/dev/cu.usb-three")

    def test_quit_after_failure_does_not_advance_or_retry(self):
        self.programmer.program_one.side_effect = self.failure()
        with mock.patch.object(cli, "ask", return_value="q") as ask, \
                mock.patch.object(core, "resolve_port", return_value="/dev/cu.usb-one") as resolve:
            result = cli.run_radio_with_retry(self.programmer, "futebol", "R05", None)
        self.assertIsNone(result)
        ask.assert_called_once()
        resolve.assert_called_once()
        self.programmer.program_one.assert_called_once()
        self.programmer.verify_attempt.assert_not_called()
        self.programmer.next_radio_id.assert_not_called()

    def test_eof_ends_instead_of_repeating_program_or_prompt(self):
        self.programmer.program_one.side_effect = [self.failure(), AssertionError("Program repeated after EOF")]
        with mock.patch("builtins.input", side_effect=[EOFError(), AssertionError("Prompt repeated after EOF")]) as input_mock, \
                mock.patch.object(core, "resolve_port", return_value="/dev/cu.usb-one") as resolve:
            try:
                result = cli.run_radio_with_retry(self.programmer, "futebol", "R05", None)
            except (core.RadioLoteError, EOFError):
                pass  # Both propagate EOF to main's controlled exit.
            else:
                self.assertIsNone(result)
        input_mock.assert_called_once()
        resolve.assert_called_once()
        self.programmer.program_one.assert_called_once()
        self.programmer.verify_attempt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
