"""Web panel credential management"""

import json
import logging
import secrets
import string
from pathlib import Path

logger = logging.getLogger(__name__)

CREDS_FILENAME = "web_credentials.json"


def _generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _generate_username(length: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "admin_" + "".join(secrets.choice(alphabet) for _ in range(length))


def _generate_auth_key(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


class WebCredentials:
    def __init__(self, data_root: str):
        self._path = Path(data_root) / CREDS_FILENAME
        self.username: str = ""
        self.password: str = ""
        self.auth_key: str = ""
        self._load_or_create()

    def _load_or_create(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self.username = data["username"]
                self.password = data["password"]
                self.auth_key = data["auth_key"]
                return
            except (json.JSONDecodeError, KeyError):
                logger.warning("Corrupted credentials file, regenerating")

        self.username = _generate_username()
        self.password = _generate_password()
        self.auth_key = _generate_auth_key()
        self._save()

    def _save(self):
        data = {
            "username": self.username,
            "password": self.password,
            "auth_key": self.auth_key,
        }
        self._path.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8",
        )

    def regenerate(self):
        self.username = _generate_username()
        self.password = _generate_password()
        self.auth_key = _generate_auth_key()
        self._save()

    def log_credentials(self):
        logger.info(
            "\n"
            "╔══════════════════════════════════════════════════╗\n"
            "║          🪐 Heroku Web Dashboard                ║\n"
            "╠══════════════════════════════════════════════════╣\n"
            "║  Username: %-37s ║\n"
            "║  Password: %-37s ║\n"
            "║  Auth Key: %-37s ║\n"
            "╠══════════════════════════════════════════════════╣\n"
            "║  Use .auth <key> in Telegram as alternative     ║\n"
            "╚══════════════════════════════════════════════════╝",
            self.username,
            self.password,
            self.auth_key[:32] + "...",
        )
