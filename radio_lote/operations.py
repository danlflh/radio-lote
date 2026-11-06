"""Shared hardware workflows for CLI and TUI, including durable recovery."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import logging
from pathlib import Path
import signal
import threading

import platformdirs

from . import core
from .catalog import Catalog
from .service import Programmer, OperationFailed
from .storage import Attempt, check_radio_id, operation_lock, sha256

LOG = logging.getLogger(__name__)


class StopRequested(core.RadioLoteError):
    pass


class _BatchProgrammer(Programmer):
    def __init__(self, owner):
        self.owner = owner
        super().__init__(owner.catalog.root / "batches", connection_factory=owner._connection,
                         progress=owner._progress)

    def load_profile(self, client: str) -> dict:
        profile = deepcopy(self.owner.catalog.batch_profile(client)["config"])
        core.validate_profile(profile)
        return profile

    def _backup(self, attempt, connection):
        self.owner._check_stop()
        return super()._backup(attempt, connection)

    def _transfer(self, attempt, connection, original, candidate):
        self.owner._check_stop()
        return super()._transfer(attempt, connection, original, candidate)

    def _readback(self, attempt, connection, expected):
        self.owner._check_stop()
        return super()._readback(attempt, connection, expected)


class Operations:
    def __init__(self, catalog: Catalog, on_event=None, connection_factory=None):
        self.catalog = catalog
        self.on_event = on_event or (lambda event: None)
        self.connection_factory = connection_factory
        self._stop = threading.Event()
        self._batch_id = None
        self._radio_id = None
        self._stage = "idle"
        self._percent = 0
        self.programmer = _BatchProgrammer(self)

    def request_stop(self) -> None:
        self._stop.set()

    def reset_stop(self) -> None:
        self._stop.clear()

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise StopRequested("Operação pausada entre etapas. Retome pelo mesmo aparelho.")

    def _emit(self, stage: str, message: str, *, current=0, total=100, persist=True, **values):
        if self._stage != stage:
            self._percent = 0
        self._stage = stage
        event = {"stage": stage, "message": message, "current": current, "total": total,
                 "batch_id": self._batch_id, "radio_id": self._radio_id, **values}
        if persist and self._batch_id:
            self.catalog.event(self._batch_id, stage, message, radio_id=self._radio_id,
                               current=current, total=total, **values)
        try:
            self.on_event(event)
        except Exception:
            LOG.exception("Interface rejected progress; hardware workflow remains authoritative")

    def _progress(self, message: str):
        stage = ("writing" if "Gravando" in message else
                 "verifying" if "Relendo" in message or "já corresponde" in message else "reading")
        self._emit(stage, message)

    def _driver_progress(self, event: dict):
        total = max(1, int(event.get("total", 1)))
        percent = min(99, int(event.get("current", 0) * 100 / total))
        self._percent = max(self._percent, percent)
        self._emit(self._stage, event.get("message", ""), current=self._percent, total=100, persist=False)

    def _connection(self, port: str):
        if self.connection_factory:
            return self.connection_factory(port)
        return core.RadioConnection(port, on_progress=self._driver_progress)

    @contextmanager
    def _session(self, batch_id: str, radio_id: str, port: str | None, *, archived=False):
        check_radio_id(radio_id)
        self._check_stop()
        with self.catalog.locked():
            batch = self.catalog._batch(batch_id)
            if not archived and batch["status"] in ("archived", "completed"):
                raise core.RadioLoteError("Reabra o lote antes de iniciar outra operação.")
            actual_port = (port or "simulated") if self.connection_factory else core.resolve_port(port)
            # A lock independent of the selected data root covers the entire
            # cycle, including the periods when pyserial closes between stages.
            lock_root = ((self.catalog.root / ".ports") if self.connection_factory else
                         Path(platformdirs.user_cache_path("radio-lote", appauthor=False)) / "ports")
            with operation_lock(lock_root / sha256(actual_port.encode())[:24]):
                self._batch_id, self._radio_id = batch_id, radio_id
                self._emit("starting", "Iniciando operação.", port=actual_port)
                previous_handler = None
                if threading.current_thread() is threading.main_thread():
                    previous_handler = signal.signal(signal.SIGINT, lambda signum, frame: self.request_stop())
                try:
                    yield actual_port
                except OperationFailed as exc:
                    paused = isinstance(exc.__cause__, StopRequested)
                    if paused:
                        exc.attempt.update("pausado")
                    try:
                        self._emit("paused" if paused else "failed", str(exc), attempt_id=exc.attempt.path.name)
                    except OSError:
                        pass
                    raise
                except Exception as exc:
                    try:
                        self._emit("failed", str(exc))
                    except OSError:
                        pass
                    raise
                finally:
                    if previous_handler is not None:
                        signal.signal(signal.SIGINT, previous_handler)
                    self._batch_id = self._radio_id = None

    def _mark_active(self, batch_id: str):
        batch = self.catalog._batch(batch_id)
        if batch["status"] != "archived":
            batch["status"] = "in_progress"
            self.catalog._save_batch(batch)

    def _finish(self, attempt: Attempt, stage="verified") -> Attempt:
        self._emit(stage, "Operação concluída.", current=100, attempt_id=attempt.path.name)
        return attempt

    def program(self, batch_id: str, radio_id: str, port: str | None = None) -> Attempt:
        with self._session(batch_id, radio_id, port) as actual:
            profile = self.catalog.batch_profile(batch_id)
            core.validate_profile(profile["config"])
            state = next((r for r in self.catalog.list_radios(batch_id) if r["radio_id"] == radio_id), None)
            self._mark_active(batch_id)
            if state and state["status"] == "recovery_required":
                self._emit("verifying", "Retomando somente a conferência da gravação anterior.")
                attempt = self.programmer.verify_attempt(self.catalog.attempt_path(batch_id, state["attempt_id"]),
                                                         radio_id, actual)
            else:
                attempt = self.programmer.program_one(batch_id, radio_id, actual)
            return self._finish(attempt)

    def _backup_locked(self, batch_id, radio_id, port, kind="backup") -> Attempt:
        attempt = Attempt.create(self.catalog.root / "batches", batch_id, radio_id, kind, port)
        with self.programmer._guard(attempt):
            with self._connection(port) as connection:
                self.programmer._backup(attempt, connection)
        return self._finish(attempt, "backed_up")

    def backup(self, batch_id: str, radio_id: str, port: str | None = None) -> Attempt:
        with self._session(batch_id, radio_id, port, archived=True) as actual:
            return self._backup_locked(batch_id, radio_id, actual)

    def capture_profile(self, batch_id: str, name: str, count: int = 4, new_groups: bool = True,
                        port: str | None = None, radio_id: str = "R01") -> dict:
        with self._session(batch_id, radio_id, port) as actual:
            if any(a["kind"] in ("programacao", "restauracao") for a in self.catalog.list_attempts(batch_id)):
                raise core.RadioLoteError("O lote já foi iniciado; duplique-o para outra configuração.")
            attempt = self._backup_locked(batch_id, radio_id, actual, "preparacao")
            self._check_stop()
            config = core.make_profile(attempt.read_raw("original"), count=count, new_groups=new_groups)
            profile = self.catalog.save_profile(name, config, source={"kind": "radio", "batch_id": batch_id,
                                                                      "attempt_id": attempt.path.name})
            self.catalog.activate_batch(batch_id, profile["id"])
            self._emit("profile_ready", "Configuração de referência salva.", profile_id=profile["id"], current=100)
            return profile

    def verify(self, batch_id: str, attempt_id: str, radio_id: str, port: str | None = None) -> Attempt:
        path = self.catalog.attempt_path(batch_id, attempt_id)
        source = Attempt.load(path)
        self.programmer._check_physical_label(source, radio_id)
        if source.record["client"] != batch_id:
            raise core.RadioLoteError("Tentativa pertence a outro lote.")
        with self._session(batch_id, radio_id, port, archived=True) as actual:
            return self._finish(self.programmer.verify_attempt(path, radio_id, actual))

    def restore(self, batch_id: str, attempt_id: str, radio_id: str, port: str | None = None) -> Attempt:
        path = self.catalog.attempt_path(batch_id, attempt_id)
        previous = Attempt.load(path)
        self.programmer._check_physical_label(previous, radio_id)
        if previous.record["client"] != batch_id:
            raise core.RadioLoteError("Backup pertence a outro lote.")
        if previous.record["kind"] not in ("backup", "preparacao", "programacao", "restauracao"):
            raise core.RadioLoteError("Esta imagem não é um backup físico restaurável.")
        target = previous.read_raw("original")
        with self._session(batch_id, radio_id, port, archived=True) as actual:
            state = next((r for r in self.catalog.list_radios(batch_id) if r["radio_id"] == radio_id), None)
            if state and state["status"] == "recovery_required" and state["kind"] == "restauracao":
                pending = self.catalog.attempt_path(batch_id, state["attempt_id"])
                if Attempt.load(pending).record.get("source_attempt_id") == attempt_id:
                    self._emit("verifying", "Retomando somente a conferência da restauração anterior.")
                    return self._finish(self.programmer.verify_attempt(pending, radio_id, actual), "restored")
            attempt = Attempt.create(self.catalog.root / "batches", batch_id, radio_id, "restauracao", actual)
            with self.programmer._guard(attempt):
                attempt.update("restauracao_preparada", source_attempt_id=attempt_id)
                with self._connection(actual) as connection:
                    original = self.programmer._backup(attempt, connection)
                    self.programmer._transfer(attempt, connection, original, target)
            return self._finish(attempt, "restored")
