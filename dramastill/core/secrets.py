from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretStoreError(RuntimeError):
    pass


class SecretStore:
    """Ключи не попадают в базу проектов.

    На Windows файл шифруется DPAPI текущего пользователя.
    На остальных системах используется Fernet, ключ лежит рядом с правами 0600.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "secrets.bin"
        self.key_path = self.directory / "secrets.key"
        self._data: dict[str, str] = {}
        self._load()

    def get(self, name: str) -> str:
        return self._data.get(name, "")

    def set(self, name: str, value: str) -> None:
        if value:
            self._data[name] = value
        else:
            self._data.pop(name, None)
        self._save()

    def set_many(self, values: dict[str, str]) -> None:
        for name, value in values.items():
            if value:
                self._data[name] = value
            else:
                self._data.pop(name, None)
        self._save()

    def values(self) -> list[str]:
        return [value for value in self._data.values() if value]

    def _load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return
        raw = self.path.read_bytes()
        try:
            decoded = self._decrypt(raw)
            loaded = json.loads(decoded.decode("utf-8"))
        except (InvalidToken, json.JSONDecodeError, OSError, ValueError) as exc:
            raise SecretStoreError("Не удалось прочитать сохранённые API-ключи") from exc
        if not isinstance(loaded, dict):
            raise SecretStoreError("Файл ключей повреждён")
        self._data = {str(key): str(value) for key, value in loaded.items() if value}

    def _save(self) -> None:
        payload = json.dumps(self._data, ensure_ascii=False).encode("utf-8")
        blob = self._encrypt(payload)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(blob)
        self._restrict(temporary)
        temporary.replace(self.path)
        self._restrict(self.path)

    def _encrypt(self, payload: bytes) -> bytes:
        if sys.platform == "win32":
            return b"DPAPI" + _dpapi_encrypt(payload)
        return b"FERNET" + self._fernet().encrypt(payload)

    def _decrypt(self, blob: bytes) -> bytes:
        if blob.startswith(b"DPAPI"):
            if sys.platform != "win32":
                raise SecretStoreError("Ключи зашифрованы DPAPI и читаются только в Windows")
            return _dpapi_decrypt(blob[5:])
        if blob.startswith(b"FERNET"):
            return self._fernet().decrypt(blob[6:])
        raise SecretStoreError("Неизвестный формат файла ключей")

    def _fernet(self) -> Fernet:
        if not self.key_path.exists():
            self.key_path.write_bytes(Fernet.generate_key())
            self._restrict(self.key_path)
        return Fernet(self.key_path.read_bytes())

    @staticmethod
    def _restrict(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError:
            return


def _dpapi_encrypt(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_decrypt(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
