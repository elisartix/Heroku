"""Heroku Web Dashboard - Credential management"""

import json
import logging
import os
import secrets
import string

logger = logging.getLogger(__name__)

CREDENTIALS_FILE = "web_credentials.json"


class WebCredentials:
    def __init__(self, data_root: str):
        self.data_root = data_root
        self.path = os.path.join(data_root, CREDENTIALS_FILE)
        self.username = ""
        self.password = ""
        self.auth_key = ""
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    data = json.load(f)
                self.username = data.get("username", "")
                self.password = data.get("password", "")
                self.auth_key = data.get("auth_key", "")
                if not all([self.username, self.password, self.auth_key]):
                    raise ValueError("Incomplete credentials")
                return
            except (json.JSONDecodeError, ValueError, KeyError):
                logger.warning("Web credentials corrupted, regenerating")

        self._generate()
        self._save()

    def _generate(self):
        prefix = "admin_"
        self.username = prefix + "".join(
            secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8)
        )
        self.password = "".join(
            secrets.choice(string.ascii_letters + string.digits + string.punctuation)
            for _ in range(20)
        )
        self.auth_key = secrets.token_urlsafe(32)

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(
                {
                    "username": self.username,
                    "password": self.password,
                    "auth_key": self.auth_key,
                },
                f,
                indent=2,
            )
        logger.debug("Web credentials saved to %s", self.path)

    def log_credentials(self, port: int = 8080):
        dash_url = f"http://localhost:{port}/dashboard"
        login_url = f"http://localhost:{port}/"
        cred_box = (
            "\n"
            "╔══════════════════════════════════════════════════╗\n"
            "║         🪐 Heroku Web Dashboard Credentials      ║\n"
            "╠══════════════════════════════════════════════════╣\n"
            f"║  Username:  {self.username:<37}║\n"
            f"║  Password:  {self.password:<37}║\n"
            f"║  Auth Key:  {self.auth_key[:36]:<36}║\n"
            "╠══════════════════════════════════════════════════╣\n"
            f"║  Dashboard: {dash_url:<46}║\n"
            f"║  Login:     {login_url:<46}║\n"
            "║  Use .auth <key> in Telegram as alternative      ║\n"
            "╚══════════════════════════════════════════════════╝"
        )
        print(cred_box)
        logger.info("Web dashboard credentials generated. Username: %s", self.username)
