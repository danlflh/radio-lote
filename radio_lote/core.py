"""Leitura e transformação conservadora de imagens Baofeng BF-888/777.

Somente ``RadioConnection`` abre a porta serial. As outras funções operam em
arquivos ou bytes, permitindo preparar e conferir o lote sem acessar um rádio.
"""

from __future__ import annotations

import base64
import binascii
import builtins
import copy
from importlib import metadata as import_metadata
import json
import logging
import os
from pathlib import Path
import tempfile
import time
import secrets
from typing import Any, Callable

import serial
from serial.tools import list_ports

# Alguns módulos CHIRP esperam o tradutor instalado pela aplicação principal.
if not hasattr(builtins, "_"):
    builtins._ = lambda value: value

from chirp import chirp_common, memmap
from chirp.drivers.h777 import H777Radio

from . import platforms


CHIRP_REVISION = "0a9e92cb21d121bf7caa137592d1885e4969871e"
RAW_SIZE = 0x3E0
DRIVER = "Baofeng_BF-888"
DEFAULT_DCS = (743, 731, 712, 703)
TRANSFER_SETTLE_SECONDS = 0.5
UPLOAD_SETTLE_SECONDS = 5.0
LOG = logging.getLogger(__name__)
_PROFILE_KEYS = {"schema_version", "client", "driver", "chirp_revision", "channels"}
_CHANNEL_KEYS = {"channel", "rx_hz", "tx_hz", "mode", "dcs", "polarity"}
_CHANNEL_V2_KEYS = _CHANNEL_KEYS | {"tone_mode", "ctcss"}


class RadioLoteError(Exception):
    """Falha que deve interromper a etapa atual sem afirmar sucesso."""


def _check_raw(raw: bytes) -> None:
    if not isinstance(raw, bytes) or len(raw) != RAW_SIZE:
        size = len(raw) if isinstance(raw, (bytes, bytearray)) else "inválido"
        raise RadioLoteError(f"Imagem bruta inválida: esperado {RAW_SIZE} bytes; recebido {size}.")


def _radio(raw: bytes) -> H777Radio:
    _check_raw(raw)
    try:
        return H777Radio(memmap.MemoryMapBytes(raw))
    except Exception as exc:
        raise RadioLoteError(f"Não foi possível interpretar a imagem BF-888: {exc}") from exc


def _packed(radio: H777Radio) -> bytes:
    raw = bytes(radio.get_mmap().get_byte_compatible().get_packed())
    _check_raw(raw)
    return raw


def load_image(path: str | Path) -> bytes:
    """Carrega .img identificada como BF-888; não adivinha modelo pelo tamanho."""
    path = Path(path)
    try:
        if path.stat().st_size > 1024 * 1024:
            raise RadioLoteError("Arquivo de imagem excessivamente grande para BF-888.")
        data = path.read_bytes()
    except OSError as exc:
        raise RadioLoteError(f"Não foi possível ler {path}: {exc}") from exc
    magic = chirp_common.CloneModeRadio.MAGIC
    if data[RAW_SIZE:RAW_SIZE + len(magic)] != magic:
        raise RadioLoteError("Imagem sem metadados CHIRP BF-888 válidos ou com tamanho incorreto.")
    try:
        metadata = json.loads(base64.b64decode(data[RAW_SIZE + len(magic):], validate=True))
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise RadioLoteError("Metadados CHIRP corrompidos.") from exc
    if not isinstance(metadata, dict) or (
        metadata.get("vendor"), metadata.get("model"), metadata.get("rclass"),
        metadata.get("variant", ""),
    ) != ("Baofeng", "BF-888", "H777Radio", ""):
        raise RadioLoteError("A imagem não identifica o driver Baofeng BF-888/H777Radio esperado.")
    raw = data[:RAW_SIZE]
    _radio(raw)
    return raw


def save_image(path: str | Path, raw: bytes) -> None:
    """Salva imagem CHIRP completa, durável e sem substituir arquivo existente."""
    radio = _radio(raw)
    path = Path(path)
    if path.suffix.lower() != ".img":
        raise RadioLoteError("O arquivo de backup deve usar a extensão .img.")
    temporary: Path | None = None
    try:
        platforms.ensure_directory(path.parent)
        fd, filename = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".img", dir=path.parent)
        temporary = Path(filename)
        os.close(fd)
        radio.metadata = {"radio_lote_chirp_revision": CHIRP_REVISION}
        radio.save_mmap(str(temporary))
        with temporary.open("r+b") as saved:
            os.fsync(saved.fileno())
        platforms.publish_file(temporary, path)
        temporary.unlink(missing_ok=True)
        temporary = None
    except FileExistsError as exc:
        raise RadioLoteError(f"Arquivo já existe e não será sobrescrito: {path}") from exc
    except OSError as exc:
        raise RadioLoteError(f"Falha ao salvar a imagem {path}: {exc}") from exc
    except Exception as exc:
        if isinstance(exc, RadioLoteError):
            raise
        raise RadioLoteError(f"Falha ao criar imagem CHIRP: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _tx_frequency(memory: Any) -> int | None:
    if memory.empty or memory.duplex == "off":
        return None
    if memory.duplex == "split":
        return int(memory.offset)
    if memory.duplex == "+":
        return int(memory.freq + memory.offset)
    if memory.duplex == "-":
        return int(memory.freq - memory.offset)
    if memory.duplex == "":
        return int(memory.freq)
    raise RadioLoteError(f"Duplex desconhecido no canal {memory.number}: {memory.duplex!r}.")


def summarize(raw: bytes) -> list[dict[str, Any]]:
    """Expõe os 16 canais sem modificar os bytes da imagem."""
    radio = _radio(raw)
    result: list[dict[str, Any]] = []
    try:
        for number in range(1, 17):
            memory = radio.get_memory(number)
            if memory.empty:
                result.append({"channel": number, "empty": True, "rx_hz": None, "tx_hz": None,
                               "rx_mhz": None, "tx_mhz": None, "mode": None,
                               "tone_mode": "", "dcs": None, "rx_dcs": None, "ctcss": None,
                               "polarity": None, "power": None, "skip": "", "extras": {}})
                continue
            tx, rx = chirp_common.split_tone_encode(memory)
            tx_hz = _tx_frequency(memory)
            result.append({
                "channel": number, "empty": False,
                "rx_hz": int(memory.freq), "tx_hz": tx_hz,
                "rx_mhz": f"{memory.freq / 1_000_000:.6f}",
                "tx_mhz": f"{tx_hz / 1_000_000:.6f}" if tx_hz is not None else None,
                "mode": memory.mode, "tone_mode": memory.tmode,
                "dcs": tx[1] if tx[0] == "DTCS" else None,
                "rx_dcs": rx[1] if rx[0] == "DTCS" else None,
                "ctcss": rx[1] if tx[0] == rx[0] == "Tone" and tx[1] == rx[1] else None,
                "polarity": memory.dtcs_polarity if "DTCS" in (tx[0], rx[0]) else None,
                "power": str(memory.power), "skip": memory.skip,
                "extras": {setting.get_name(): str(setting.value) for setting in memory.extra},
            })
    except Exception as exc:
        if isinstance(exc, RadioLoteError):
            raise
        raise RadioLoteError(f"Não foi possível ler os canais da imagem: {exc}") from exc
    return result


def profile_from_raw(raw: bytes, client: str = "futebol") -> dict[str, Any]:
    """Usa frequências e largura dos quatro canais do primeiro rádio do cliente."""
    channels = []
    for channel, dcs in zip(summarize(raw)[:4], DEFAULT_DCS):
        if channel["empty"]:
            raise RadioLoteError(f"Canal {channel['channel']} vazio; não é possível derivar o perfil.")
        channels.append({
            "channel": channel["channel"], "rx_hz": channel["rx_hz"],
            "tx_hz": channel["tx_hz"], "mode": channel["mode"],
            "dcs": dcs, "polarity": "NN",
        })
    profile = {"schema_version": 1, "client": client, "driver": DRIVER,
               "chirp_revision": CHIRP_REVISION, "channels": channels}
    validate_profile(profile)
    return profile


def validate_profile(profile: dict[str, Any]) -> None:
    """Aceita apenas os campos operacionais suportados por este fluxo de lote."""
    if not isinstance(profile, dict) or set(profile) != _PROFILE_KEYS:
        raise RadioLoteError("Perfil deve conter somente schema_version, client, driver, chirp_revision e channels.")
    version = profile["schema_version"]
    if type(version) is not int or version not in (1, 2):
        raise RadioLoteError("Versão de perfil não suportada; esperado schema_version 1 ou 2.")
    if not isinstance(profile["client"], str) or not profile["client"].strip() or len(profile["client"]) > 120:
        raise RadioLoteError("Informe um nome de cliente entre 1 e 120 caracteres.")
    if profile["driver"] != DRIVER or profile["chirp_revision"] != CHIRP_REVISION:
        raise RadioLoteError("Driver ou revisão CHIRP do perfil não corresponde à versão deste programa.")
    channels = profile["channels"]
    if not isinstance(channels, list) or not 1 <= len(channels) <= 16:
        raise RadioLoteError("O perfil deve conter de 1 a 16 canais.")
    if version == 1 and len(channels) != 4:
        raise RadioLoteError("O perfil legado deve conter exatamente os canais 1–4.")
    numbers: set[int] = set()
    frequencies: set[int] = set()
    for channel in channels:
        expected_keys = _CHANNEL_KEYS if version == 1 else _CHANNEL_V2_KEYS
        if not isinstance(channel, dict) or (
                set(channel) != expected_keys
                and not (version == 2 and set(channel) == expected_keys | {"label"})):
            raise RadioLoteError("Campos de canal ausentes ou não suportados pela versão do perfil.")
        number = channel["channel"]
        limit = 4 if version == 1 else 16
        if type(number) is not int or not 1 <= number <= limit or number in numbers:
            raise RadioLoteError(f"O perfil exige posições de 1 a {limit} sem repetições.")
        numbers.add(number)
        for field in ("rx_hz", "tx_hz"):
            value = channel[field]
            if type(value) is not int or not 400_000_000 <= value <= 490_000_000 or value % 10:
                raise RadioLoteError(f"Canal {number}: {field} deve ser inteiro de 400–490 MHz, em passos de 10 Hz.")
        if channel["rx_hz"] != channel["tx_hz"]:
            raise RadioLoteError(f"Canal {number}: este perfil exige operação simplex (RX igual a TX).")
        if version == 1 and channel["rx_hz"] in frequencies:
            raise RadioLoteError("Os quatro canais devem usar frequências distintas.")
        frequencies.add(channel["rx_hz"])
        if channel["mode"] not in ("FM", "NFM"):
            raise RadioLoteError(f"Canal {number}: modo deve ser FM ou NFM.")
        if type(channel["dcs"]) is not int or channel["dcs"] not in chirp_common.DTCS_CODES:
            raise RadioLoteError(f"Canal {number}: código DCS não suportado.")
        if channel["polarity"] not in ("NN", "NR", "RN", "RR"):
            raise RadioLoteError(f"Canal {number}: polaridade DCS inválida.")
        if version == 2:
            tone_mode = channel["tone_mode"]
            if tone_mode not in ("", "TSQL", "DTCS"):
                raise RadioLoteError(f"Canal {number}: use sem tom, TSQL ou DTCS; tons cruzados não são suportados.")
            tone = channel["ctcss"]
            if tone_mode == "TSQL":
                if type(tone) not in (int, float) or tone not in chirp_common.TONES:
                    raise RadioLoteError(f"Canal {number}: tom CTCSS não suportado.")
            elif tone is not None:
                raise RadioLoteError(f"Canal {number}: ctcss deve ser nulo quando não usa TSQL.")
            if "label" in channel and (not isinstance(channel["label"], str)
                                       or len(channel["label"]) > 80
                                       or any(ord(char) < 32 for char in channel["label"])):
                raise RadioLoteError(f"Canal {number}: nome deve ter até 80 caracteres, sem controles.")


def normalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Retorna cópia independente schema2 sem perder a configuração do legado."""
    validate_profile(profile)
    normalized = copy.deepcopy(profile)
    if normalized["schema_version"] == 1:
        normalized["schema_version"] = 2
        for channel in normalized["channels"]:
            channel.update(tone_mode="DTCS", ctcss=None)
    for channel in normalized["channels"]:
        if channel["ctcss"] is not None:
            channel["ctcss"] = float(channel["ctcss"])
    validate_profile(normalized)
    return normalized


def make_profile(raw: bytes, client: str = "default", count: int = 4,
                 new_groups: bool = True) -> dict[str, Any]:
    """Cria uma proposta schema2 a partir dos primeiros N canais da referência.

    Os códigos de novos grupos são sorteados somente ao criar a proposta. Quem
    programa os aparelhos deve persistir e reutilizar esse perfil, não gerá-lo
    novamente a cada operação.
    """
    if type(count) is not int or not 1 <= count <= 16:
        raise RadioLoteError("Escolha entre 1 e 16 canais de referência.")
    if type(new_groups) is not bool:
        raise RadioLoteError("new_groups deve ser verdadeiro ou falso.")
    radio = _radio(raw)
    generated_codes = secrets.SystemRandom().sample(list(chirp_common.DTCS_CODES), count) if new_groups else None
    channels = []
    try:
        for number in range(1, count + 1):
            memory = radio.get_memory(number)
            if memory.empty:
                raise RadioLoteError(f"Canal {number} vazio na referência; revise a quantidade de canais.")
            if not new_groups and memory.tmode not in ("", "TSQL", "DTCS"):
                raise RadioLoteError(f"Canal {number} usa tons assimétricos/cruzados ({memory.tmode}); "
                                     "a integração exige sem tom, CTCSS igual TX/RX ou DCS igual TX/RX.")
            tone_mode = "DTCS" if new_groups else memory.tmode
            channels.append({
                "channel": number, "rx_hz": int(memory.freq), "tx_hz": _tx_frequency(memory),
                "mode": memory.mode, "tone_mode": tone_mode,
                "dcs": generated_codes[number - 1] if generated_codes is not None else int(memory.dtcs),
                "ctcss": float(memory.ctone) if tone_mode == "TSQL" else None,
                "polarity": memory.dtcs_polarity if not new_groups and tone_mode == "DTCS" else "NN",
            })
    except Exception as exc:
        if isinstance(exc, RadioLoteError):
            raise
        raise RadioLoteError(f"Não foi possível ler os canais de referência: {exc}") from exc
    profile = {"schema_version": 2, "client": client, "driver": DRIVER,
               "chirp_revision": CHIRP_REVISION, "channels": channels}
    validate_profile(profile)
    if new_groups and len({item["rx_hz"] for item in channels}) != count:
        raise RadioLoteError("Novos grupos precisam de frequências distintas; a referência contém repetições.")
    if not new_groups:
        encoded = build_candidate(raw, profile)
        for item in channels:
            start = 0x10 + 16 * (item["channel"] - 1)
            if encoded[start + 8:start + 12] != raw[start + 8:start + 12]:
                raise RadioLoteError(f"Canal {item['channel']} contém tom não representável sem alteração; "
                                     "revise a referência antes de integrar.")
    return profile


def build_candidate(raw: bytes, profile: dict[str, Any]) -> bytes:
    """Edita somente frequências, tons e FM/NFM das posições presentes no perfil.

    O driver normaliza bits reservados ao salvar um canal. O merge abaixo aceita
    apenas os campos pedidos, conservando todos os demais bits de cada aparelho.
    """
    profile = normalize_profile(profile)
    radio = _radio(raw)
    try:
        for channel in profile["channels"]:
            memory = radio.get_memory(channel["channel"])
            memory.empty = False
            memory.freq = channel["rx_hz"]
            memory.duplex = ""
            memory.offset = 0
            memory.mode = channel["mode"]
            memory.tmode = channel["tone_mode"]
            memory.dtcs = channel["dcs"]
            memory.rx_dtcs = channel["dcs"]
            memory.dtcs_polarity = channel["polarity"]
            if channel["tone_mode"] == "TSQL":
                memory.ctone = channel["ctcss"]
                memory.rtone = channel["ctcss"]
            errors = [issue for issue in radio.validate_memory(memory)
                      if isinstance(issue, chirp_common.ValidationError)]
            if errors:
                raise RadioLoteError(f"Canal {memory.number}: " + "; ".join(map(str, errors)))
            radio.set_memory(memory)
        edited = _packed(radio)
        merged = bytearray(raw)
        for number in (channel["channel"] for channel in profile["channels"]):
            start = 0x10 + 16 * (number - 1)
            merged[start:start + 12] = edited[start:start + 12]
            merged[start + 12] = (raw[start + 12] & ~0x04) | (edited[start + 12] & 0x04)
        candidate = bytes(merged)
        checked = _radio(candidate)
        for channel in profile["channels"]:
            memory = checked.get_memory(channel["channel"])
            tx, rx = chirp_common.split_tone_encode(memory)
            if channel["tone_mode"] == "DTCS":
                expected_tx = ("DTCS", channel["dcs"], channel["polarity"][0])
                expected_rx = ("DTCS", channel["dcs"], channel["polarity"][1])
            elif channel["tone_mode"] == "TSQL":
                expected_tx = expected_rx = ("Tone", channel["ctcss"], None)
            else:
                expected_tx = expected_rx = ("", None, None)
            if (memory.empty or memory.freq != channel["rx_hz"]
                    or _tx_frequency(memory) != channel["tx_hz"]
                    or memory.mode != channel["mode"] or tx != expected_tx or rx != expected_rx):
                raise RadioLoteError(f"Conferência semântica falhou no canal {channel['channel']}.")
        return candidate
    except Exception as exc:
        if isinstance(exc, RadioLoteError):
            raise
        raise RadioLoteError(f"Não foi possível preparar a imagem: {exc}") from exc


def verify_raw(actual: bytes, expected: bytes) -> None:
    """Compara todos os bytes; sucesso de upload sozinho não prova gravação."""
    _check_raw(actual)
    _check_raw(expected)
    differences = [offset for offset, (got, wanted) in enumerate(zip(actual, expected)) if got != wanted]
    if differences:
        detail = ", ".join(f"0x{offset:03X}: lido {actual[offset]:02X}, esperado {expected[offset]:02X}"
                           for offset in differences[:8])
        raise RadioLoteError(f"Verificação divergiu em {len(differences)} byte(s): {detail}.")


def list_usb_ports() -> list[dict[str, str]]:
    """Enumera portas locais de programação no macOS, Linux e Windows."""
    try:
        ports = []
        for port in list_ports.comports():
            if not platforms.serial_port_is_local(port.device):
                continue
            usb = (getattr(port, "vid", None) is not None or getattr(port, "pid", None) is not None
                   or "usb" in port.device.lower() or "USB" in (port.hwid or "").upper())
            # COM e ttyACM podem não expor VID/PID dependendo do driver. No Mac,
            # filtre os dispositivos cu.Bluetooth que não são cabos de programação.
            if usb or not port.device.startswith("/dev/cu."):
                ports.append({"device": port.device, "description": port.description or "",
                              "hwid": port.hwid or ""})
        return sorted(ports, key=lambda item: item["device"])
    except Exception as exc:
        raise RadioLoteError(f"Falha ao enumerar portas USB: {exc}") from exc


def resolve_port(requested: str | None = None) -> str:
    ports = list_usb_ports()
    if requested is not None:
        if not platforms.serial_port_is_local(requested):
            raise RadioLoteError("Porta inválida: informe COMn, /dev/ttyUSBn, /dev/ttyACMn ou /dev/cu.* local.")
        for port in ports:
            if port["device"] == requested or (port["device"].upper().startswith("COM")
                                                 and port["device"].upper() == requested.upper()):
                return port["device"]
        raise RadioLoteError(f"Porta USB {requested!r} não encontrada; confira o cabo e a conexão.")
    if not ports:
        raise RadioLoteError("Nenhuma porta USB serial encontrada. Conecte o cabo de programação.")
    if len(ports) > 1:
        raise RadioLoteError("Há várias portas USB; selecione uma com --porta: "
                             + ", ".join(port["device"] for port in ports))
    return ports[0]["device"]


class _LoggingSerial(serial.Serial):
    """Fornece o método exigido pelo driver sem importar wx/SerialTrace."""

    def log(self, message: str) -> None:
        LOG.debug("serial: %s", message)


def _check_installed_revision() -> None:
    """Impede acesso físico quando o pacote instalado não comprova a revisão fixada."""
    try:
        origin = json.loads(import_metadata.distribution("chirp").read_text("direct_url.json") or "null")
        if not isinstance(origin, dict):
            raise ValueError("origem da instalação ausente")
        url = origin.get("url", "")
        archive_urls = {
            f"https://github.com/kk7ds/chirp/archive/{CHIRP_REVISION}.zip",
            f"https://github.com/kk7ds/chirp/archive/{CHIRP_REVISION}.tar.gz",
            f"https://codeload.github.com/kk7ds/chirp/zip/{CHIRP_REVISION}",
            f"https://codeload.github.com/kk7ds/chirp/tar.gz/{CHIRP_REVISION}",
        }
        vcs = origin.get("vcs_info", {})
        vcs_valid = (url in ("https://github.com/kk7ds/chirp", "https://github.com/kk7ds/chirp.git")
                     and isinstance(vcs, dict) and vcs.get("vcs") == "git"
                     and vcs.get("commit_id") == CHIRP_REVISION)
        if url not in archive_urls and not vcs_valid:
            raise ValueError("origem ou revisão diferente da esperada")
        if H777Radio._memsize != RAW_SIZE or H777Radio.IDENT != [b"P3107"]:
            raise ValueError("geometria ou identificação do driver mudou")
    except Exception as exc:
        raise RadioLoteError("CHIRP instalado não corresponde à revisão fixada. "
                             "Execute novamente o preparo do ambiente antes de conectar ao rádio.") from exc


class RadioConnection:
    """Contexto explícito com uma abertura de porta por transferência.

    Algumas unidades respondem ``F`` ao comando de saída ``E`` do driver. Fechar,
    aguardar e drenar a entrada ao reabrir evita usar essa resposta como ACK do
    próximo handshake. Uma falha nunca provoca nova tentativa automática de upload.
    """

    def __init__(self, port: str, on_progress: Callable[[dict[str, Any]], None] | None = None):
        self.port = port
        self.on_progress = on_progress
        self.pipe: _LoggingSerial | None = None
        self._active = False
        self._next_transfer_at = 0.0

    def __enter__(self) -> "RadioConnection":
        if self._active:
            raise RadioLoteError("O contexto de conexão serial já está ativo.")
        if not platforms.serial_port_is_local(self.port):
            raise RadioLoteError("Use COMn, /dev/ttyUSBn, /dev/ttyACMn ou /dev/cu.* local; URLs não são aceitas.")
        _check_installed_revision()
        self._open_serial()
        self._active = True
        return self

    def _open_serial(self) -> None:
        pipe = None
        try:
            pipe = _LoggingSerial(port=None, baudrate=H777Radio.BAUD_RATE,
                                  rtscts=H777Radio.HARDWARE_FLOW, timeout=0.5,
                                  write_timeout=2, **platforms.serial_exclusive_options())
            pipe.rts = H777Radio.WANTS_RTS
            pipe.dtr = H777Radio.WANTS_DTR
            pipe.port = self.port
            pipe.open()
            pipe.reset_input_buffer()
            self.pipe = pipe
        except Exception as exc:
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:
                    LOG.exception("Falha ao fechar a porta após erro de abertura")
            raise RadioLoteError(f"Não foi possível abrir {self.port}: {exc}") from exc

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._active = False
        self._close_serial(suppress_errors=exc_type is not None)

    def _close_serial(self, *, suppress_errors: bool = False) -> None:
        pipe, self.pipe = self.pipe, None
        if pipe is None:
            return
        try:
            pipe.close()
        except Exception as exc:
            if suppress_errors:
                LOG.exception("Falha adicional ao fechar %s", self.port)
            else:
                raise RadioLoteError(f"Falha ao fechar {self.port}: {exc}") from exc

    def _finish_transfer(self, *, suppress_errors: bool, delay: float = TRANSFER_SETTLE_SECONDS) -> None:
        try:
            self._close_serial(suppress_errors=suppress_errors)
        finally:
            self._next_transfer_at = time.monotonic() + delay

    def _connected(self) -> _LoggingSerial:
        if not self._active:
            raise RadioLoteError("Use a conexão dentro de seu contexto antes de transferir.")
        if self.pipe is None:
            remaining = self._next_transfer_at - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            self._open_serial()
        if self.pipe is None or not self.pipe.is_open:
            raise RadioLoteError("A porta serial foi fechada inesperadamente.")
        return self.pipe

    def _status(self, status: Any) -> None:
        LOG.debug("%s: %s/%s", status.msg, status.cur, status.max)
        if self.on_progress is not None:
            try:
                self.on_progress({"current": status.cur, "total": status.max, "message": status.msg})
            except Exception:
                # Uma falha de apresentação não deve abortar uma escrita já em
                # andamento nem mudar o resultado do protocolo do rádio.
                LOG.exception("Falha ao apresentar progresso da transferência")

    def download(self) -> bytes:
        """Lê a imagem completa; o driver exige a assinatura compatível P3107."""
        succeeded = False
        try:
            radio = H777Radio(self._connected())
            radio.status_fn = self._status
            radio.sync_in()
            raw = _packed(radio)
            succeeded = True
            return raw
        except Exception as exc:
            if isinstance(exc, RadioLoteError):
                raise
            raise RadioLoteError(f"Falha ao ler o rádio; confira modelo, alimentação e cabo: {exc}") from exc
        finally:
            self._finish_transfer(suppress_errors=not succeeded)

    def upload(self, raw: bytes) -> None:
        """Grava pelo protocolo oficial; o chamador deve reler e conferir depois."""
        succeeded = False
        try:
            radio = _radio(raw)
            radio.set_pipe(self._connected())
            radio.status_fn = self._status
            radio.sync_out()
            succeeded = True
        except Exception as exc:
            if isinstance(exc, RadioLoteError):
                raise
            raise RadioLoteError(f"Falha durante a gravação; estado do rádio requer conferência: {exc}") from exc
        finally:
            # No R01 a gravação foi aceita, mas 0,5 s ainda era cedo para reler.
            # Aguarda o firmware estabilizar; não repete o upload em caso de falha.
            self._finish_transfer(suppress_errors=not succeeded, delay=UPLOAD_SETTLE_SECONDS)
