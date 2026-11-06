"""Persistência durável dos backups e do andamento de cada tentativa."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from .core import CHIRP_REVISION, RadioLoteError, load_image, save_image
from . import platforms
from .platforms import ensure_directory, sync_dir


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_client(client: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", client):
        raise RadioLoteError("Cliente deve usar letras minúsculas, números, _ ou - (até 48 caracteres).")
    return client


def check_radio_id(radio_id: str) -> str:
    if not re.fullmatch(r"R(?:0[1-9]|[1-9][0-9]{1,2})", radio_id):
        raise RadioLoteError("Identifique o aparelho como R01, R02... até R999.")
    return radio_id


def write_json(path: Path, value: dict, *, replace: bool = False) -> None:
    """Backups/perfis são exclusivos; somente manifestos podem ser atualizados."""
    ensure_directory(path.parent)
    blob = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=".json-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        platforms.publish_file(Path(temporary), path, replace=replace)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RadioLoteError(f"Não foi possível ler {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RadioLoteError(f"JSON inválido em {path}.")
    return value


@contextmanager
def operation_lock(root: Path):
    """Impede dois ciclos deste programa simultâneos, inclusive em clientes distintos."""
    try:
        with platforms.operation_lock(root):
            yield
    except platforms.OperationLockedError as exc:
        raise RadioLoteError(str(exc)) from exc


class Attempt:
    """Uma tentativa tem seu próprio diretório; nunca recicla o backup de outra."""

    def __init__(self, path: Path, record: dict):
        self.path = path
        self.record = record

    @classmethod
    def create(cls, root: Path, client: str, radio_id: str, kind: str, port: str | None):
        check_client(client)
        check_radio_id(radio_id)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = root / client / "tentativas" / f"{timestamp}-{radio_id}-{kind}-{uuid4().hex[:8]}"
        ensure_directory(path.parent)
        path.mkdir(exist_ok=False)
        sync_dir(path.parent)
        record = {
            "schema_version": 1, "client": client, "radio_id": radio_id,
            "kind": kind, "port": port, "chirp_revision": CHIRP_REVISION,
            "created_at": utc_now(), "status": "iniciado", "events": [],
            "artifacts": {}, "write_started": False,
        }
        attempt = cls(path, record)
        attempt.update("iniciado")
        return attempt

    @classmethod
    def load(cls, path: Path):
        path = path.expanduser().resolve()
        record = read_json(path / "registro.json")
        if record.get("schema_version") != 1 or record.get("chirp_revision") != CHIRP_REVISION:
            raise RadioLoteError("Registro incompatível com esta versão do programa.")
        check_client(record.get("client", ""))
        check_radio_id(record.get("radio_id", ""))
        return cls(path, record)

    def update(self, status: str, **values) -> None:
        self.record.update(values)
        self.record["status"] = status
        self.record["updated_at"] = utc_now()
        self.record["events"].append({"at": utc_now(), "status": status})
        write_json(self.path / "registro.json", self.record, replace=True)

    def save_raw(self, name: str, raw: bytes) -> None:
        if not re.fullmatch(r"[a-z0-9_-]+", name):
            raise RadioLoteError("Nome de artefato inválido.")
        path = self.path / f"{name}.img"
        save_image(path, raw)
        if load_image(path) != raw:
            raise RadioLoteError(f"Imagem persistida não corresponde à leitura: {path}")
        self.record["artifacts"][name] = {
            "file": path.name, "raw_sha256": sha256(raw),
            "file_sha256": sha256(path.read_bytes()),
        }
        self.update(self.record["status"])

    def read_raw(self, name: str) -> bytes:
        artifact = self.record.get("artifacts", {}).get(name)
        if not artifact or artifact.get("file") != f"{name}.img":
            raise RadioLoteError(f"A tentativa não possui imagem íntegra registrada: {name}.")
        path = self.path / artifact["file"]
        if sha256(path.read_bytes()) != artifact.get("file_sha256"):
            raise RadioLoteError(f"Arquivo alterado desde seu registro: {path}")
        raw = load_image(path)
        if sha256(raw) != artifact.get("raw_sha256"):
            raise RadioLoteError(f"Conteúdo do backup diverge do hash registrado: {path}")
        return raw
