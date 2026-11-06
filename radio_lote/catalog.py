"""Versioned local batches, immutable profiles and portable backup bundles."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import threading
from uuid import uuid4, uuid5, NAMESPACE_URL
import zipfile

from . import core
from .platforms import default_data_dir, publish_file, publish_directory, sync_dir, ensure_directory
from .storage import Attempt, check_radio_id, operation_lock, read_json, sha256, utc_now, write_json

SCHEMA = 2
ID_PATTERN = re.compile(r"[a-f0-9]{32}")
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
ATTEMPT_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,180}")


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise core.RadioLoteError("Identificador de lote/perfil inválido.")
    return value


def _text(value: str, *, optional=False) -> str:
    if not isinstance(value, str) or len(value.strip()) > 120 or (not optional and not value.strip()):
        raise core.RadioLoteError("Use um nome de até 120 caracteres.")
    return value.strip()


def config_hash(config: dict) -> str:
    return sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode())


def mutation(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with self.locked():
            return fn(self, *args, **kwargs)
    return wrapped


class Catalog:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or default_data_dir()).expanduser().resolve()
        self._local = threading.local()

    @contextmanager
    def locked(self):
        if getattr(self._local, "held", False):
            yield
            return
        with operation_lock(self.root):
            self._local.held = True
            try:
                yield
            finally:
                self._local.held = False

    def batch_path(self, batch_id: str) -> Path:
        path = self.root / "batches" / _identifier(batch_id)
        if not path.resolve().is_relative_to(self.root):
            raise core.RadioLoteError("Caminho de lote fora do catálogo.")
        return path

    def _batch(self, batch_id: str) -> dict:
        value = read_json(self.batch_path(batch_id) / "batch.json")
        self._validate_batch_record(value)
        if value["id"] != batch_id:
            raise core.RadioLoteError("Manifesto de lote incompatível.")
        return value

    @staticmethod
    def _validate_batch_record(value: dict) -> None:
        _identifier(value.get("id"))
        _text(value.get("name"))
        _text(value.get("client"), optional=True)
        if (value.get("schema_version") != SCHEMA or value.get("model") != core.DRIVER
                or value.get("status") not in ("draft", "ready", "in_progress", "completed", "archived")
                or not isinstance(value.get("created_at"), str)
                or not isinstance(value.get("updated_at"), str)
                or type(value.get("group_count")) is not int or not 1 <= value["group_count"] <= 16
                or type(value.get("new_groups")) is not bool):
            raise core.RadioLoteError("Manifesto de lote incompatível.")
        quantity = value.get("quantity")
        if quantity is not None and (type(quantity) is not int or not 1 <= quantity <= 999):
            raise core.RadioLoteError("Quantidade inválida no manifesto do lote.")
        if value.get("profile_id") is not None:
            _identifier(value["profile_id"])
            if not isinstance(value.get("profile_hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", value["profile_hash"]):
                raise core.RadioLoteError("Hash de perfil inválido no lote.")
        elif value["status"] not in ("draft", "archived"):
            raise core.RadioLoteError("Lote ativo sem configuração de referência.")

    def _save_batch(self, batch: dict) -> None:
        batch["updated_at"] = utc_now()
        write_json(self.batch_path(batch["id"]) / "batch.json", batch, replace=True)

    def settings(self) -> dict:
        path = self.root / "settings.json"
        return read_json(path) if path.exists() else {}

    @mutation
    def save_settings(self, **values) -> dict:
        settings = self.settings()
        if set(values) - {"lang", "theme", "no_color"}:
            raise core.RadioLoteError("Opção de configuração desconhecida.")
        if "lang" in values and values["lang"] not in ("pt", "en"):
            raise core.RadioLoteError("Idioma deve ser pt ou en.")
        if "theme" in values and values["theme"] not in ("light", "dark", "auto"):
            raise core.RadioLoteError("Tema deve ser light, dark ou auto.")
        settings.update(values)
        write_json(self.root / "settings.json", settings, replace=True)
        return settings

    @mutation
    def save_profile(self, name: str, config: dict, source: dict | None = None) -> dict:
        name = _text(name)
        config = deepcopy(config)
        core.validate_profile(config)
        revision = 1 + max((p["revision"] for p in self.list_profiles() if p["name"] == name), default=0)
        profile = {"schema_version": SCHEMA, "id": uuid4().hex, "name": name,
                   "revision": revision, "created_at": utc_now(), "config": config,
                   "config_hash": config_hash(config), "source": deepcopy(source or {})}
        write_json(self.root / "profiles" / f"{profile['id']}.json", profile)
        return profile

    @staticmethod
    def _validate_profile_record(profile: dict) -> dict:
        if not isinstance(profile, dict):
            raise core.RadioLoteError("Formato de perfil inválido.")
        _identifier(profile.get("id"))
        if profile.get("schema_version") != SCHEMA or profile.get("config_hash") != config_hash(profile.get("config", {})):
            raise core.RadioLoteError("Perfil corrompido ou alterado desde sua criação.")
        core.validate_profile(profile["config"])
        _text(profile.get("name"))
        if type(profile.get("revision")) is not int or profile["revision"] < 1:
            raise core.RadioLoteError("Número de revisão de perfil inválido.")
        return profile

    def list_profiles(self) -> list[dict]:
        return [self._validate_profile_record(read_json(path))
                for path in sorted((self.root / "profiles").glob("*.json"))]

    def get_profile(self, profile_id: str) -> dict:
        return self._validate_profile_record(read_json(self.root / "profiles" / f"{_identifier(profile_id)}.json"))

    def batch_profile(self, batch_id: str) -> dict:
        batch = self._batch(batch_id)
        if not batch.get("profile_id"):
            raise core.RadioLoteError("Escolha ou leia uma configuração de referência para este lote.")
        profile = self._validate_profile_record(read_json(self.batch_path(batch_id) / "profile.json"))
        if profile["id"] != batch["profile_id"] or profile["config_hash"] != batch["profile_hash"]:
            raise core.RadioLoteError("O perfil do lote não corresponde ao manifesto.")
        return profile

    @mutation
    def create_batch(self, name: str, client: str = "", quantity: int | None = None,
                     profile_id: str | None = None, group_count: int = 4, new_groups: bool = True) -> dict:
        if quantity is not None and (type(quantity) is not int or not 1 <= quantity <= 999):
            raise core.RadioLoteError("Quantidade deve ser de 1 a 999, ou ficar em branco.")
        if type(group_count) is not int or not 1 <= group_count <= 16:
            raise core.RadioLoteError("Escolha de 1 a 16 grupos.")
        batch = {"schema_version": SCHEMA, "id": uuid4().hex, "name": _text(name),
                 "client": _text(client, optional=True), "model": core.DRIVER,
                 "quantity": quantity, "profile_id": None, "status": "draft",
                 "group_count": group_count, "new_groups": bool(new_groups),
                 "created_at": utc_now(), "updated_at": utc_now()}
        write_json(self.batch_path(batch["id"]) / "batch.json", batch)
        self.event(batch["id"], "created", "Lote criado.")
        if profile_id:
            self.activate_batch(batch["id"], profile_id)
        return self.get_batch(batch["id"])

    @mutation
    def update_batch(self, batch_id: str, **values) -> dict:
        if set(values) - {"name", "client", "quantity", "group_count", "new_groups"}:
            raise core.RadioLoteError("Campo do lote não editável.")
        batch = self._batch(batch_id)
        if batch["status"] == "archived":
            raise core.RadioLoteError("Reabra o lote arquivado antes de editar.")
        for key in ("name", "client"):
            if key in values:
                values[key] = _text(values[key], optional=key == "client")
        if "quantity" in values and values["quantity"] is not None:
            if type(values["quantity"]) is not int or not 1 <= values["quantity"] <= 999:
                raise core.RadioLoteError("Quantidade deve ser de 1 a 999.")
            if values["quantity"] < len(self.list_radios(batch_id)):
                raise core.RadioLoteError("Quantidade menor que os aparelhos já registrados.")
        if {"group_count", "new_groups"} & set(values):
            if batch.get("profile_id"):
                raise core.RadioLoteError("A configuração já tem perfil; crie uma nova revisão antes de ativar.")
            if "group_count" in values and not 1 <= values["group_count"] <= 16:
                raise core.RadioLoteError("Escolha de 1 a 16 grupos.")
        batch.update(values)
        self._save_batch(batch)
        return self.get_batch(batch_id)

    @mutation
    def activate_batch(self, batch_id: str, profile_id: str) -> dict:
        batch = self._batch(batch_id)
        if batch["status"] == "archived" or any(a["kind"] in ("programacao", "restauracao")
                                               for a in self.list_attempts(batch_id)):
            raise core.RadioLoteError("Lote iniciado: o perfil é imutável. Duplique o lote para outra configuração.")
        profile = self.get_profile(profile_id)
        core.validate_profile(profile["config"])
        write_json(self.batch_path(batch_id) / "profile.json", profile, replace=True)
        batch.update(profile_id=profile_id, profile_hash=profile["config_hash"], status="ready")
        self._save_batch(batch)
        self.event(batch_id, "profile", f"Perfil ativado: {profile['name']} r{profile['revision']}.")
        return self.get_batch(batch_id)

    @mutation
    def duplicate_batch(self, batch_id: str, name: str) -> dict:
        batch = self._batch(batch_id)
        return self.create_batch(name, batch["client"], batch.get("quantity"), batch.get("profile_id"),
                                 batch.get("group_count", 4), batch.get("new_groups", True))

    @mutation
    def archive_batch(self, batch_id: str, archived: bool = True) -> dict:
        batch = self._batch(batch_id)
        if archived:
            if any(r["status"] == "recovery_required" for r in self.list_radios(batch_id)):
                raise core.RadioLoteError("Conclua a recuperação pendente antes de arquivar.")
            if batch["status"] != "archived":
                batch["previous_status"] = batch["status"]
                batch["status"] = "archived"
        else:
            batch["status"] = batch.pop("previous_status", "ready" if batch.get("profile_id") else "draft")
        self._save_batch(batch)
        self.event(batch_id, "archive" if archived else "reopen", "Estado do lote atualizado.")
        return self.get_batch(batch_id)

    @mutation
    def complete_batch(self, batch_id: str) -> dict:
        batch = self.get_batch(batch_id)
        radios = self.list_radios(batch_id)
        if not radios or any(r["status"] != "verified" for r in radios):
            raise core.RadioLoteError("Ainda há rádios sem programação e conferência concluídas.")
        if batch.get("quantity") and len(radios) < batch["quantity"]:
            raise core.RadioLoteError("A quantidade prevista ainda não foi atingida.")
        stored = self._batch(batch_id)
        if stored["status"] == "archived":
            raise core.RadioLoteError("Reabra o lote antes de concluir.")
        stored["status"] = "completed"
        self._save_batch(stored)
        self.event(batch_id, "completed", "Programação do lote concluída; voz tem resultado separado.")
        return self.get_batch(batch_id)

    def list_attempts(self, batch_id: str, radio_id: str | None = None) -> list[dict]:
        self._batch(batch_id)
        records = []
        for path in sorted((self.batch_path(batch_id) / "tentativas").glob("*/registro.json")):
            if path.is_symlink() or not path.resolve().is_relative_to(self.batch_path(batch_id)):
                raise core.RadioLoteError("Referência de tentativa fora do lote.")
            record = read_json(path)
            check_radio_id(record.get("radio_id", ""))
            if radio_id is None or record["radio_id"] == radio_id:
                records.append({**record, "id": path.parent.name, "path": str(path.parent)})
        return records

    def attempt_path(self, batch_id: str, attempt_id: str) -> Path:
        if not isinstance(attempt_id, str) or not ATTEMPT_PATTERN.fullmatch(attempt_id):
            raise core.RadioLoteError("Identificador de tentativa inválido.")
        path = self.batch_path(batch_id) / "tentativas" / attempt_id
        if not path.resolve().is_relative_to(self.batch_path(batch_id)):
            raise core.RadioLoteError("Tentativa fora do lote.")
        return path

    def list_radios(self, batch_id: str) -> list[dict]:
        latest, fallback = {}, {}
        for record in self.list_attempts(batch_id):
            label = record["radio_id"]
            fallback[label] = record
            physical = max(record.get("verified_at", ""), record.get("write_started_at", ""))
            if not physical and record.get("failed_stage") in ("conferindo", "retomando_conferencia"):
                physical = record.get("updated_at", record.get("created_at", ""))
            if record["kind"] in ("programacao", "restauracao") and physical:
                if label not in latest or physical > latest[label][0]:
                    latest[label] = (physical, record)
        result = []
        for label in sorted(fallback, key=lambda value: int(value[1:])):
            record = latest[label][1] if label in latest else fallback[label]
            status = "pending"
            if record["status"] == "verificado":
                status = "verified" if record["kind"] == "programacao" else "restored"
            elif record.get("write_started") or record.get("failed_stage") in ("conferindo", "retomando_conferencia"):
                status = "recovery_required"
            elif record["status"] == "falhou":
                status = "failed"
            elif record["kind"] in ("backup", "preparacao") and "original" in record.get("artifacts", {}):
                status = "backed_up"
            result.append({"radio_id": label, "status": status, "voice_test": record.get("voice_test", "pendente"),
                           "attempt_id": record["id"], "kind": record["kind"], "updated_at": record.get("updated_at"),
                           "error": record.get("error", "")})
        return result

    def get_batch(self, batch_id: str) -> dict:
        batch = self._batch(batch_id)
        radios = self.list_radios(batch_id)
        ready = {r["radio_id"] for r in radios if r["status"] == "verified"}
        batch["counts"] = {"verified": len(ready), "voice": sum(r["radio_id"] in ready and r["voice_test"] == "aprovado" for r in radios),
                           "failed": sum(r["status"] == "failed" for r in radios),
                           "pending": sum(r["status"] != "verified" for r in radios)}
        pending = next((r["radio_id"] for r in radios if r["status"] != "verified"), None)
        target_reached = batch.get("quantity") is not None and len(ready) >= batch["quantity"]
        batch["next_radio"] = pending or (None if target_reached else
            next((f"R{n:02d}" for n in range(1, 1000) if f"R{n:02d}" not in ready), None))
        return batch

    def list_batches(self, include_archived: bool = False) -> list[dict]:
        batches = [self.get_batch(path.parent.name) for path in (self.root / "batches").glob("*/batch.json")]
        return sorted((b for b in batches if include_archived or b["status"] != "archived"),
                      key=lambda batch: batch["updated_at"], reverse=True)

    @mutation
    def event(self, batch_id: str, stage: str, message: str, **details) -> None:
        path = self.batch_path(batch_id) / "events.jsonl"
        ensure_directory(path.parent)
        event = {"at": utc_now(), "stage": stage, "message": message, **details}
        # A crash can leave a partial last line. Preserve those bytes as a
        # diagnostic file before continuing, instead of gluing a new event to it.
        if path.exists():
            blob = path.read_bytes()
            tail_start = blob.rfind(b"\n") + 1
            tail = blob[tail_start:]
            if tail:
                try:
                    json.loads(tail)
                except (ValueError, UnicodeDecodeError):
                    diagnostic = path.parent / f"events-interrupted-{uuid4().hex}.log"
                    with diagnostic.open("xb") as handle:
                        handle.write(tail)
                        handle.flush()
                        os.fsync(handle.fileno())
                    with path.open("r+b") as handle:
                        handle.truncate(tail_start)
                        handle.flush()
                        os.fsync(handle.fileno())
                else:
                    with path.open("ab") as handle:
                        handle.write(b"\n")
                        handle.flush()
                        os.fsync(handle.fileno())
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        sync_dir(path.parent)

    def logs(self, batch_id: str) -> list[dict]:
        self._batch(batch_id)
        path = self.batch_path(batch_id) / "events.jsonl"
        if not path.exists():
            return []
        lines = path.read_bytes().splitlines()
        events = []
        for number, line in enumerate(lines):
            try:
                events.append(json.loads(line))
            except ValueError:
                if number != len(lines) - 1:
                    raise core.RadioLoteError("Log contém uma entrada corrompida.")
                events.append({"stage": "interrupted", "message": "Última entrada incompleta após interrupção."})
        return events

    @mutation
    def set_voice(self, batch_id: str, radio_id: str, passed: bool) -> None:
        radio = next((r for r in self.list_radios(batch_id) if r["radio_id"] == radio_id), None)
        if not radio or radio["status"] != "verified":
            raise core.RadioLoteError("O aparelho precisa estar programado e verificado antes do teste de voz.")
        attempt = Attempt.load(self.attempt_path(batch_id, radio["attempt_id"]))
        attempt.update("verificado", voice_test="aprovado" if passed else "falhou")
        self.event(batch_id, "voice", "Teste manual de voz registrado.", radio_id=radio_id, passed=bool(passed))

    @mutation
    def export_batch(self, batch_id: str, destination: Path) -> Path:
        batch_dir = self.batch_path(batch_id)
        self._batch(batch_id)
        destination = Path(destination).expanduser().resolve()
        if destination.is_relative_to(batch_dir):
            raise core.RadioLoteError("Exporte para um local fora do diretório do lote.")
        ensure_directory(destination.parent)
        files = {}
        for path in sorted(batch_dir.rglob("*")):
            if path.is_symlink():
                raise core.RadioLoteError("O lote contém links simbólicos; exportação interrompida.")
            if path.is_file() and path.suffix in (".json", ".jsonl", ".img", ".log"):
                files[path.relative_to(batch_dir).as_posix()] = sha256(path.read_bytes())
        fd, name = tempfile.mkstemp(dir=destination.parent, suffix=".zip")
        os.close(fd)
        temporary = Path(name)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps({"schema_version": SCHEMA, "batch_id": batch_id, "files": files}))
                for relative in files:
                    archive.write(batch_dir / relative, relative)
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            publish_file(temporary, destination, replace=False)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    @mutation
    def import_batch(self, source: Path) -> dict:
        ensure_directory(self.root)
        with tempfile.TemporaryDirectory(prefix=".import-", dir=self.root) as temporary:
            staging = Path(temporary)
            try:
                with zipfile.ZipFile(source) as archive:
                    infos = archive.infolist()
                    if len(infos) > 20000 or sum(info.file_size for info in infos) > MAX_BUNDLE_BYTES:
                        raise core.RadioLoteError("Arquivo de lote excede o limite de importação.")
                    names = [info.filename for info in infos]
                    if len(set(names)) != len(names):
                        raise core.RadioLoteError("Arquivo ZIP possui caminhos duplicados.")
                    for info in infos:
                        path = PurePosixPath(info.filename)
                    if (path.is_absolute() or ".." in path.parts or "\\" in info.filename or ":" in info.filename
                                or path.as_posix() != info.filename
                                or stat.S_ISLNK(info.external_attr >> 16) or info.is_dir()):
                            raise core.RadioLoteError("Caminho inseguro no arquivo de lote.")
                    manifest = json.loads(archive.read("manifest.json"))
                    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA or not isinstance(manifest.get("files"), dict):
                        raise core.RadioLoteError("Manifesto de exportação incompatível.")
                    files = manifest["files"]
                    if set(names) != set(files) | {"manifest.json"}:
                        raise core.RadioLoteError("Arquivos não correspondem ao manifesto.")
                    for relative, expected in files.items():
                        blob = archive.read(relative)
                        if sha256(blob) != expected:
                            raise core.RadioLoteError(f"Integridade inválida: {relative}")
                        path = staging / relative
                        ensure_directory(path.parent)
                        with path.open("xb") as handle:
                            handle.write(blob)
                            handle.flush()
                            os.fsync(handle.fileno())
                        sync_dir(path.parent)
                        if path.suffix == ".img":
                            core.load_image(path)
            except (zipfile.BadZipFile, KeyError, ValueError) as exc:
                raise core.RadioLoteError(f"Exportação inválida: {exc}") from exc
            batch = read_json(staging / "batch.json")
            self._validate_batch_record(batch)
            batch_id = _identifier(batch.get("id"))
            if batch_id != manifest["batch_id"] or batch.get("schema_version") != SCHEMA:
                raise core.RadioLoteError("Identidade do lote divergente no ZIP.")
            profile = None
            if batch.get("profile_id"):
                profile = self._validate_profile_record(read_json(staging / "profile.json"))
                if profile["id"] != batch["profile_id"] or profile["config_hash"] != batch["profile_hash"]:
                    raise core.RadioLoteError("Perfil não corresponde ao lote importado.")
            for record_path in staging.glob("tentativas/*/registro.json"):
                record = read_json(record_path)
                if (not ATTEMPT_PATTERN.fullmatch(record_path.parent.name)
                        or record.get("schema_version") != 1
                        or record.get("chirp_revision") != core.CHIRP_REVISION
                        or record.get("client") != batch_id
                        or record.get("kind") not in ("programacao", "preparacao", "restauracao", "backup")
                        or not isinstance(record.get("status"), str)
                        or not isinstance(record.get("events"), list)
                        or not isinstance(record.get("artifacts"), dict)
                        or any(key in record and not isinstance(record[key], str)
                               for key in ("created_at", "updated_at", "verified_at", "write_started_at"))):
                    raise core.RadioLoteError("Registro de tentativa incompatível com o lote importado.")
                check_radio_id(record.get("radio_id", ""))
                for artifact_name, artifact in record["artifacts"].items():
                    if not isinstance(artifact, dict) or not re.fullmatch(r"[a-z0-9_-]+", artifact_name):
                        raise core.RadioLoteError("Registro de imagem inválido no lote.")
                    filename = artifact.get("file", "")
                    if filename != f"{artifact_name}.img":
                        raise core.RadioLoteError("Referência de imagem inválida no lote.")
                    image_path = record_path.parent / filename
                    if sha256(image_path.read_bytes()) != artifact.get("file_sha256"):
                        raise core.RadioLoteError("Hash de imagem divergente do registro.")
                    if sha256(core.load_image(image_path)) != artifact.get("raw_sha256"):
                        raise core.RadioLoteError("Memória da imagem diverge do registro.")
            target = self.batch_path(batch_id)
            if target.exists():
                if all((target / relative).is_file() and sha256((target / relative).read_bytes()) == digest
                       for relative, digest in files.items()):
                    return self.get_batch(batch_id)
                raise core.RadioLoteError("Já existe um lote com este ID e conteúdo diferente; nada foi sobrescrito.")
            if profile:
                self._install_profile(profile)
            ensure_directory(target.parent)
            publish_directory(staging, target)
        return self.get_batch(batch_id)

    def _install_profile(self, profile: dict) -> None:
        target = self.root / "profiles" / f"{profile['id']}.json"
        if target.exists():
            if read_json(target) != profile:
                raise core.RadioLoteError("Colisão de identificador de perfil; importação interrompida.")
        else:
            write_json(target, profile)

    @mutation
    def migrate_legacy(self, source: Path) -> list[dict]:
        source = Path(source).expanduser().resolve()
        if not source.is_dir() or source == self.root or self.root.is_relative_to(source):
            raise core.RadioLoteError("Escolha a pasta de dados antiga, distinta do novo catálogo.")
        results = []
        with operation_lock(source):
            for client in sorted(source.iterdir()):
                if not client.is_dir() or client.name.startswith(".") or client.name == "simulacoes":
                    continue
                attempt_files = sorted((client / "tentativas").glob("*/registro.json"))
                if not attempt_files:
                    continue
                key = sha256(client.name.encode() + attempt_files[0].read_bytes())
                batch_id = uuid5(NAMESPACE_URL, f"radio-lote:legacy:{key}").hex
                if self.batch_path(batch_id).exists():
                    results.append(self.get_batch(batch_id))
                    continue
                ensure_directory(self.root)
                with tempfile.TemporaryDirectory(prefix=".migration-", dir=self.root) as temporary:
                    staging = Path(temporary)
                    for record_path in attempt_files:
                        attempt_dir = record_path.parent
                        destination = staging / "tentativas" / attempt_dir.name
                        ensure_directory(destination)
                        for path in attempt_dir.iterdir():
                            if path.is_symlink():
                                raise core.RadioLoteError("Migração não aceita links simbólicos.")
                            if path.is_file() and path.suffix in (".img", ".json", ".log", ".jsonl"):
                                shutil.copy2(path, destination / path.name)
                                with (destination / path.name).open("r+b") as handle:
                                    os.fsync(handle.fileno())
                                if sha256(path.read_bytes()) != sha256((destination / path.name).read_bytes()):
                                    raise core.RadioLoteError("Cópia de migração divergiu da origem.")
                        original = record_path.read_bytes()
                        with (destination / "registro-legado.json").open("xb") as handle:
                            handle.write(original)
                            handle.flush()
                            os.fsync(handle.fileno())
                        working = json.loads(original)
                        working.update(client=batch_id, legacy_client=client.name)
                        write_json(destination / "registro.json", working, replace=True)
                        for artifact in working.get("artifacts", {}).values():
                            file = artifact.get("file", "")
                            if Path(file).name != file:
                                raise core.RadioLoteError("Referência inválida no backup antigo.")
                            path = destination / file
                            if sha256(path.read_bytes()) != artifact["file_sha256"] or sha256(core.load_image(path)) != artifact["raw_sha256"]:
                                raise core.RadioLoteError("Backup antigo não corresponde aos hashes registrados.")
                    profile = None
                    if (client / "perfil.json").exists():
                        config = read_json(client / "perfil.json")
                        core.validate_profile(config)
                        profile_id = uuid5(NAMESPACE_URL, f"radio-lote:profile:{key}").hex
                        profile = {"schema_version": SCHEMA, "id": profile_id, "name": client.name,
                                   "revision": 1, "config": config, "config_hash": config_hash(config),
                                   "created_at": utc_now(), "source": {"kind": "legacy", "key": key}}
                        write_json(staging / "profile.json", profile)
                    batch = {"schema_version": SCHEMA, "id": batch_id, "name": client.name, "client": client.name,
                             "model": core.DRIVER, "quantity": None, "profile_id": profile["id"] if profile else None,
                             "profile_hash": profile["config_hash"] if profile else None,
                             "status": "in_progress" if profile else "draft", "group_count": 4, "new_groups": False,
                             "created_at": json.loads(attempt_files[0].read_bytes()).get("created_at", utc_now()),
                             "updated_at": utc_now(), "migration": {"legacy_key": key, "originals_retained": True}}
                    write_json(staging / "batch.json", batch)
                    if profile:
                        self._install_profile(profile)
                    target = self.batch_path(batch_id)
                    ensure_directory(target.parent)
                    publish_directory(staging, target)
                results.append(self.get_batch(batch_id))
        return results
