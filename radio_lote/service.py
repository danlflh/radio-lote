"""Ciclos de trabalho; a interface de terminal apenas conduz o operador."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from . import core
from .storage import Attempt, check_client, check_radio_id, read_json, sha256, utc_now, write_json


class OperationFailed(core.RadioLoteError):
    def __init__(self, message: str, attempt: Attempt):
        super().__init__(f"{message}\nRegistro da tentativa: {attempt.path}")
        self.attempt = attempt


class Programmer:
    def __init__(self, root: Path, connection_factory=None, progress=None):
        self.root = root.expanduser().resolve()
        self.connection_factory = connection_factory or core.RadioConnection
        self.progress = progress or (lambda message: None)

    @contextmanager
    def _guard(self, attempt: Attempt):
        try:
            yield
        except (Exception, KeyboardInterrupt) as exc:
            stage = attempt.record["status"]
            message = str(exc) or "Operação interrompida pelo operador."
            try:
                attempt.update("falhou", failed_stage=stage, error=message)
            except OSError:
                # Um disco sem espaço não pode transformar a falha em sucesso.
                pass
            if isinstance(exc, KeyboardInterrupt):
                message += " O rádio pode precisar ser desligado e ligado."
            raise OperationFailed(message, attempt) from exc

    def _backup(self, attempt: Attempt, connection) -> bytes:
        attempt.update("lendo_original")
        self.progress("Lendo o rádio e salvando o backup individual...")
        raw = connection.download()
        attempt.save_raw("original", raw)
        # Relê o arquivo e confere também os hashes registrados antes de qualquer upload.
        durable = attempt.read_raw("original")
        attempt.update("backup_conferido")
        return durable

    def prepare(self, client: str, radio_id: str, port: str) -> Attempt:
        check_client(client)
        if (self.root / client / "perfil.json").exists():
            raise core.RadioLoteError("Este cliente já tem perfil ativo. Use programar ou outro identificador de cliente.")
        attempt = Attempt.create(self.root, client, radio_id, "preparacao", port)
        with self._guard(attempt):
            with self.connection_factory(port) as connection:
                raw = self._backup(attempt, connection)
            profile = core.profile_from_raw(raw, client=client)
            write_json(attempt.path / "perfil-proposto.json", profile)
            attempt.update("perfil_proposto", profile=profile,
                           profile_file_sha256=sha256((attempt.path / "perfil-proposto.json").read_bytes()))
        return attempt

    def activate_profile(self, attempt: Attempt) -> Path:
        if (attempt.record.get("kind") != "preparacao"
                or attempt.record.get("status") not in ("perfil_proposto", "perfil_ativado")):
            raise core.RadioLoteError("Somente uma preparação física concluída pode ativar o perfil.")
        attempt.read_raw("original")
        proposed = attempt.path / "perfil-proposto.json"
        if sha256(proposed.read_bytes()) != attempt.record["profile_file_sha256"]:
            raise core.RadioLoteError("O perfil proposto foi alterado após a leitura.")
        profile = read_json(proposed)
        core.validate_profile(profile)
        client_dir = self.root / check_client(profile["client"])
        destination = client_dir / "perfil.json"
        expected_origin = {
            "preparation": str(attempt.path),
            "profile_file_sha256": sha256(proposed.read_bytes()),
            "original_raw_sha256": attempt.record["artifacts"]["original"]["raw_sha256"],
        }
        origin_path = client_dir / "perfil-origem.json"
        if origin_path.exists():
            if read_json(origin_path) != expected_origin:
                raise core.RadioLoteError("Já existe origem de outro perfil para este cliente; nada foi sobrescrito.")
        else:
            if destination.exists():
                raise core.RadioLoteError("Existe perfil sem origem correspondente; nada foi sobrescrito.")
            write_json(origin_path, expected_origin)
        # Retoma a mesma ativação após interrupção, sem aceitar outro perfil.
        if destination.exists():
            if sha256(destination.read_bytes()) != expected_origin["profile_file_sha256"]:
                raise core.RadioLoteError("O perfil existente difere da proposta; nada foi sobrescrito.")
        else:
            write_json(destination, profile)
        attempt.update("perfil_ativado")
        return destination

    def load_profile(self, client: str) -> dict:
        directory = self.root / check_client(client)
        profile = read_json(directory / "perfil.json")
        origin = read_json(directory / "perfil-origem.json")
        if sha256((directory / "perfil.json").read_bytes()) != origin.get("profile_file_sha256"):
            raise core.RadioLoteError("O perfil ativo mudou desde a conferência inicial. Não será usado para gravar.")
        core.validate_profile(profile)
        if profile["client"] != client:
            raise core.RadioLoteError("O perfil pertence a outro cliente.")
        return profile

    def records(self, client: str) -> list[dict]:
        records = []
        for path in sorted((self.root / check_client(client) / "tentativas").glob("*/registro.json")):
            record = read_json(path)
            record["path"] = str(path.parent)
            records.append(record)
        return records

    def next_radio_id(self, client: str) -> str:
        completed = {
            record["radio_id"] for record in self._latest_physical_records(client).values()
            if record["kind"] == "programacao" and record["status"] == "verificado"
        }
        for number in range(1, 1000):
            label = f"R{number:02d}"
            if label not in completed:
                return label
        raise core.RadioLoteError("Limite de R999 atingido para este cliente.")

    def _latest_physical_records(self, client: str) -> dict[str, dict]:
        latest = {}
        for record in self.records(client):
            if record["kind"] not in ("programacao", "restauracao"):
                continue
            physical_at = max(record.get("verified_at", ""), record.get("write_started_at", ""))
            if not physical_at:
                continue
            label = record["radio_id"]
            if label not in latest or physical_at > latest[label]["physical_at"]:
                latest[label] = {**record, "physical_at": physical_at}
        return latest

    def program_one(self, client: str, radio_id: str, port: str) -> Attempt:
        check_radio_id(radio_id)
        profile = self.load_profile(client)
        record = self._latest_physical_records(client).get(radio_id)
        if record and record["kind"] == "programacao" and record["status"] == "verificado":
            raise core.RadioLoteError(f"{radio_id} já foi verificado neste lote. Confira a etiqueta do próximo aparelho.")
        attempt = Attempt.create(self.root, client, radio_id, "programacao", port)
        with self._guard(attempt):
            attempt.update("perfil_validado", profile=profile)
            with self.connection_factory(port) as connection:
                original = self._backup(attempt, connection)
                candidate = core.build_candidate(original, profile)
                self._transfer(attempt, connection, original, candidate)
        return attempt

    def _transfer(self, attempt: Attempt, connection, original: bytes, candidate: bytes) -> None:
        attempt.save_raw("preparada", candidate)
        expected = attempt.read_raw("preparada")
        # Ambos os arquivos precisam continuar íntegros imediatamente antes da gravação.
        core.verify_raw(attempt.read_raw("original"), original)
        attempt.update("pronto_para_gravar")
        if expected != original:
            self.progress("Backup conferido. Gravando a configuração...")
            attempt.update("gravacao_iniciada", write_started=True, write_started_at=utc_now())
            connection.upload(expected)
            attempt.update("gravacao_concluida")
        else:
            self.progress("A configuração já corresponde ao perfil. Conferindo novamente...")
            attempt.update("sem_alteracoes")
        self._readback(attempt, connection, expected)

    def _readback(self, attempt: Attempt, connection, expected: bytes) -> None:
        self.progress("Relendo o rádio para conferir o conteúdo completo...")
        attempt.update("conferindo")
        actual = connection.download()
        count = 1
        while (f"conferencia-{count:03d}" in attempt.record["artifacts"]
               or (attempt.path / f"conferencia-{count:03d}.img").exists()):
            count += 1
        attempt.save_raw(f"conferencia-{count:03d}", actual)
        core.verify_raw(actual, expected)
        attempt.update("verificado", verified_raw_sha256=sha256(actual), verified_at=utc_now(),
                       voice_test="pendente")

    def verify_attempt(self, source: Path, radio_id: str, port: str) -> Attempt:
        attempt = Attempt.load(source)
        self._check_physical_label(attempt, radio_id)
        if attempt.record["kind"] not in ("programacao", "restauracao"):
            raise core.RadioLoteError("Esta tentativa não corresponde a uma gravação/restauração física.")
        expected = attempt.read_raw("preparada")
        attempt.read_raw("original")
        with self._guard(attempt):
            attempt.update("retomando_conferencia", port=port)
            with self.connection_factory(port) as connection:
                self._readback(attempt, connection, expected)
        return attempt

    @staticmethod
    def _check_physical_label(attempt: Attempt, radio_id: str) -> None:
        check_radio_id(radio_id)
        if attempt.record["radio_id"] != radio_id:
            raise core.RadioLoteError(f"A etiqueta informada não corresponde ao backup de {attempt.record['radio_id']}.")

    def restore(self, source: Path, radio_id: str, port: str) -> Attempt:
        previous = Attempt.load(source)
        self._check_physical_label(previous, radio_id)
        if previous.record["kind"] not in ("preparacao", "programacao", "restauracao"):
            raise core.RadioLoteError("Uma simulação não pode ser usada para restaurar um rádio.")
        target = previous.read_raw("original")
        client = previous.record["client"]
        attempt = Attempt.create(self.root, client, radio_id, "restauracao", port)
        with self._guard(attempt):
            attempt.update("restauracao_preparada", source_attempt=str(previous.path))
            with self.connection_factory(port) as connection:
                current = self._backup(attempt, connection)
                self._transfer(attempt, connection, current, target)
        return attempt

    def simulate(self, image: Path, client: str, profile_path: Path | None = None) -> Attempt:
        raw = core.load_image(image)
        profile = read_json(profile_path) if profile_path else core.profile_from_raw(raw, client=client)
        core.validate_profile(profile)
        attempt = Attempt.create(self.root / "simulacoes", client, "R01", "simulacao", None)
        with self._guard(attempt):
            attempt.save_raw("original", raw)
            candidate = core.build_candidate(raw, profile)
            attempt.save_raw("preparada", candidate)
            core.verify_raw(attempt.read_raw("preparada"), candidate)
            write_json(attempt.path / "perfil-proposto.json", profile)
            attempt.update("simulacao_concluida", profile=profile,
                           changed_bytes=sum(a != b for a, b in zip(raw, candidate)))
        return attempt

    def record_voice_test(self, source: Path, radio_id: str, passed: bool) -> Attempt:
        attempt = Attempt.load(source)
        self._check_physical_label(attempt, radio_id)
        if attempt.record["status"] != "verificado":
            raise core.RadioLoteError("Primeiro conclua a conferência de memória deste aparelho.")
        attempt.update("verificado", voice_test="aprovado" if passed else "falhou")
        return attempt
