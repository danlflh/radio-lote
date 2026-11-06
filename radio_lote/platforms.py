"""Caminhos, portas e publicação de arquivos nos sistemas suportados."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import sys

import platformdirs
import portalocker


class OperationLockedError(Exception):
    """Outro processo já possui a exclusividade do diretório de dados."""


def is_windows() -> bool:
    return sys.platform == "win32"


def default_data_dir() -> Path:
    """Resolve o diretório do usuário sem criar pastas ao consultar."""
    return Path(platformdirs.user_data_path("radio-lote", appauthor=False, ensure_exists=False))


def serial_port_is_local(device: str) -> bool:
    """Aceita nomes locais conhecidos; jamais encaminha URLs ao pyserial."""
    if not isinstance(device, str) or "://" in device:
        return False
    return bool(
        re.fullmatch(r"(?:\\\\\.\\)?COM[1-9][0-9]*", device, re.IGNORECASE)
        or re.fullmatch(r"/dev/tty(?:USB|ACM)[0-9]+", device)
        or re.fullmatch(r"/dev/cu\.[A-Za-z0-9_.-]+", device)
    )


def serial_exclusive_options() -> dict:
    # Serial Win32 já abre a porta sem compartilhamento. O argumento exclusive
    # pertence ao backend POSIX e não deve ser enviado ao backend Windows.
    return {} if is_windows() else {"exclusive": True}


def sync_dir(path: Path) -> None:
    """Persiste entradas POSIX; Windows usa publicação WRITE_THROUGH abaixo."""
    if is_windows():
        # Python não oferece fsync de diretórios no Windows. Não abrir diretório
        # com os.open: falha com PermissionError mesmo para uma pasta válida.
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_directory(path: Path) -> None:
    missing = []
    current = Path(path).absolute()
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        sync_dir(directory.parent)
        sync_dir(directory)


def _windows_move(source: Path, destination: Path, *, replace: bool) -> None:
    """Renomeia no mesmo volume, sem substituir por padrão, aguardando flush."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel32.MoveFileExW
    move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move.restype = wintypes.BOOL
    # MOVEFILE_WRITE_THROUGH; nunca usar COPY_ALLOWED (temp está no mesmo volume).
    flags = 0x8 | (0x1 if replace else 0)
    def absolute_name(path: Path) -> str:
        value = str(path.absolute())
        if value.startswith("\\\\?\\"):
            return value
        if value.startswith("\\\\"):
            return "\\\\?\\UNC\\" + value[2:]
        return "\\\\?\\" + value

    if not move(absolute_name(source), absolute_name(destination), flags):
        error = ctypes.get_last_error()
        if error in (80, 183):  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
            raise FileExistsError(error, "Arquivo de destino já existe", str(destination))
        raise ctypes.WinError(error)


def publish_file(temporary: Path, destination: Path, *, replace: bool = False) -> None:
    """Publica um arquivo já fsyncado; backups nunca substituem outro arquivo.

    O temporário deve estar no diretório de destino. No POSIX, hardlink garante
    create-if-absent atômico. No Windows, MoveFileExW sem REPLACE_EXISTING oferece
    o mesmo contrato, inclusive em volumes sem suporte a hardlinks.
    """
    temporary, destination = Path(temporary), Path(destination)
    if temporary.parent.resolve() != destination.parent.resolve():
        raise ValueError("A publicação atômica exige temporário no diretório de destino.")
    if is_windows():
        _windows_move(temporary, destination, replace=replace)
    elif replace:
        os.replace(temporary, destination)
    else:
        os.link(temporary, destination)
    sync_dir(destination.parent)


def publish_directory(staging: Path, destination: Path) -> None:
    """Publica uma árvore no mesmo volume sem substituir nem um destino vazio."""
    staging, destination = Path(staging), Path(destination)
    if not staging.is_dir() or staging.is_symlink():
        raise ValueError("A origem de publicação deve ser um diretório real.")
    if is_windows():
        _windows_move(staging, destination, replace=False)
    else:
        import ctypes
        import errno

        library = ctypes.CDLL(None, use_errno=True)
        source_bytes, destination_bytes = os.fsencode(staging), os.fsencode(destination)
        if sys.platform == "darwin":
            rename = library.renamex_np
            rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
            rename.restype = ctypes.c_int
            result = rename(source_bytes, destination_bytes, 0x4)  # RENAME_EXCL
        elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
            rename = library.renameat2
            rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
            rename.restype = ctypes.c_int
            result = rename(-100, source_bytes, -100, destination_bytes, 1)  # AT_FDCWD, RENAME_NOREPLACE
        else:
            raise OSError(errno.ENOTSUP, "Publicação de diretório sem substituição indisponível neste sistema.")
        if result:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
        sync_dir(staging.parent)
        if staging.parent.resolve() != destination.parent.resolve():
            sync_dir(destination.parent)


@contextmanager
def operation_lock(root: Path):
    """Lock de processo portátil, liberado pelo sistema também após crash."""
    root = Path(root)
    ensure_directory(root)
    lock = portalocker.Lock(str(root / ".operacao.lock"), mode="a+", timeout=0,
                            flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
    try:
        lock.acquire()
    except portalocker.exceptions.LockException as exc:
        raise OperationLockedError("Outro processo radio-lote já está operando. Aguarde sua conclusão.") from exc
    try:
        yield
    finally:
        lock.release()
