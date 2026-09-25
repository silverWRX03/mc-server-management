"""How the web UI is protected: a password, a PIN, or nothing (this computer only).

The choice lives in ``.mcsm/web-auth.json`` as a salted PBKDF2 hash. A new server
starts with the password ``PASSWORD`` and the web UI asks to change it at the
first sign-in. ``[web] password`` in mcsm.toml overrides all of this.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass

from .config import Config, ConfigError

DEFAULT_PASSWORD = "PASSWORD"
MODES = ("password", "pin", "none")
FILE = "web-auth.json"
LEGACY_FILE = "web-password"   # mcsm 0.1 kept a generated password here in plain text
ITERATIONS = 200_000


def _hash(secret: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, iterations).hex()


def validate(mode: str, secret: str) -> None:
    if mode not in MODES:
        raise ConfigError(f"unknown sign-in mode {mode!r}")
    if mode == "pin" and not re.fullmatch(r"\d{4,8}", secret):
        raise ConfigError("a PIN is 4 to 8 digits")
    if mode == "password":
        if len(secret) < 4:
            raise ConfigError("use a password of at least 4 characters")
        if len(secret) > 200:
            raise ConfigError("that password is too long")
        if secret == DEFAULT_PASSWORD:
            raise ConfigError("pick something other than the default password")


@dataclass
class Auth:
    mode: str = "password"
    salt: str = ""
    hash: str = ""
    iterations: int = ITERATIONS
    default: bool = False      # still the built-in PASSWORD
    managed: bool = False      # set by [web] password in mcsm.toml; can't be changed from the UI

    def check(self, secret: str) -> bool:
        if self.mode == "none":
            return True
        if not self.hash:
            return False
        return hmac.compare_digest(_hash(secret, bytes.fromhex(self.salt), self.iterations), self.hash)

    def info(self) -> dict:
        return {"mode": self.mode, "default": self.default, "managed": self.managed}


def _hashed(mode: str, secret: str, default: bool = False) -> Auth:
    if mode == "none":
        return Auth(mode="none")
    salt = secrets.token_bytes(16)
    return Auth(mode=mode, salt=salt.hex(), hash=_hash(secret, salt), default=default)


class AuthStore:
    """Reads and writes the sign-in settings; notices changes made by `mcsm web-password`."""

    def __init__(self, config: Config):
        self.config = config
        self.path = config.state_dir / FILE
        self._lock = threading.Lock()
        self._auth: Auth | None = None
        self._mtime: float | None = None
        self._managed_for: str | None = None

    def get(self) -> Auth:
        if self.config.web.password:
            with self._lock:
                if self._auth is None or self._managed_for != self.config.web.password:
                    self._auth = _hashed("password", self.config.web.password)
                    self._auth.managed = True
                    self._managed_for = self.config.web.password
                return self._auth
        self._managed_for = None
        with self._lock:
            mtime = self.path.stat().st_mtime if self.path.exists() else None
            if self._auth is None or self._auth.managed or mtime != self._mtime:
                self._auth = self._load()
                self._mtime = self.path.stat().st_mtime
            return self._auth

    def _load(self) -> Auth:
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text())
                if d.get("mode") in MODES:
                    return Auth(mode=d["mode"], salt=d.get("salt", ""), hash=d.get("hash", ""),
                                iterations=int(d.get("iterations", ITERATIONS)), default=bool(d.get("default")))
            except (ValueError, TypeError):
                pass
        legacy = self.config.state_dir / LEGACY_FILE
        if legacy.exists() and legacy.read_text().strip():
            # Keep an existing mcsm 0.1 password working, but stop storing it in plain text.
            auth = _hashed("password", legacy.read_text().strip())
            self._save(auth)
            legacy.unlink(missing_ok=True)
            return auth
        auth = _hashed("password", DEFAULT_PASSWORD, default=True)
        self._save(auth)
        return auth

    def _save(self, auth: Auth) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"mode": auth.mode, "salt": auth.salt, "hash": auth.hash,
                                   "iterations": auth.iterations, "default": auth.default}, indent=2) + "\n")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def set(self, mode: str, secret: str = "") -> Auth:
        if self.config.web.password:
            raise ConfigError("the password is set in mcsm.toml ([web] password); change it there")
        validate(mode, secret)
        auth = _hashed(mode, secret)
        with self._lock:
            self._save(auth)
            self._auth, self._mtime = auth, self.path.stat().st_mtime
        return auth

    def reset(self) -> Auth:
        """Back to the default PASSWORD (for a forgotten password)."""
        auth = _hashed("password", DEFAULT_PASSWORD, default=True)
        with self._lock:
            self._save(auth)
            self._auth, self._mtime = auth, self.path.stat().st_mtime
        return auth


def describe(auth: Auth) -> str:
    """One line for the console: how to sign in to the control panel."""
    if auth.managed:
        return "the password from [web] in mcsm.toml"
    if auth.default:
        return f"{DEFAULT_PASSWORD}  (you'll be asked to choose your own)"
    if auth.mode == "none":
        return "none needed on this computer"
    return ("the PIN you chose" if auth.mode == "pin" else "the password you chose") + \
        "  (forgot it? run `mcsm web-password --reset`)"
