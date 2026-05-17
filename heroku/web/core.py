"""Responsible for web init and mandatory ops"""

#    Friendly Telegram (telegram userbot)
#    Copyright (C) 2018-2021 The Authors

#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.

#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.

#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.

# ©️ Dan Gazizullin, 2021-2023
# This file is a part of Heroku Userbot
# 🌐 https://github.com/hikariatama/Heroku
# You can redistribute it and/or modify it under the terms of the GNU AGPLv3
# 🔑 https://www.gnu.org/licenses/agpl-3.0.html

import asyncio
import contextlib
import hmac
import inspect
import logging
import os
import secrets
import subprocess
import time
import typing

import aiohttp_jinja2
import jinja2
from aiohttp import web

from ..database import Database
from ..loader import Modules
from ..tl_cache import CustomTelegramClient
from . import proxypass, root
from .credentials import WebCredentials

logger = logging.getLogger(__name__)

SESSION_TTL = 3600
MAX_AUTH_ATTEMPTS = 5
AUTH_LOCKOUT_SECONDS = 300

_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_STATIC_DIR = os.path.join(_PROJECT_ROOT, "web-resources", "static")


class Web(root.Web):
    def __init__(self, **kwargs):
        self.runner = None
        self.port = None
        self.running = asyncio.Event()
        self.ready = asyncio.Event()
        self.client_data = {}
        self.app = web.Application()
        self.proxypasser = None

        # Dashboard auth state
        self._dash_sessions: dict[str, float] = {}
        self._dash_csrf_tokens: dict[str, float] = {}
        self._dash_auth_attempts: dict[str, list[float]] = {}
        self._dash_start_time: float = 0
        self._web_creds: typing.Optional[WebCredentials] = None

        aiohttp_jinja2.setup(
            self.app,
            filters={"getdoc": inspect.getdoc, "ascii": ascii},
            loader=jinja2.FileSystemLoader("web-resources"),
        )
        self.app["static_root_url"] = "/static"

        super().__init__(**kwargs)
        self.app.router.add_get("/favicon.ico", self.favicon)
        self.app.router.add_static("/static/", _STATIC_DIR)

        # Dashboard routes
        self.app.router.add_get("/dashboard", self._dash_page)
        self.app.router.add_get("/login", self._dash_login_page)
        self.app.router.add_post("/api/login", self._api_login)
        self.app.router.add_post("/api/auth_key", self._api_auth_key)
        self.app.router.add_get("/api/auth_key_login/{token}", self._api_auth_key_login)
        self.app.router.add_post("/api/logout", self._api_logout)
        self.app.router.add_get("/api/csrf", self._api_csrf)
        self.app.router.add_get("/api/dashboard", self._api_dashboard)
        self.app.router.add_get("/api/modules", self._api_modules)
        self.app.router.add_post("/api/modules/toggle", self._api_modules_toggle)
        self.app.router.add_get("/api/modules/config/{name}", self._api_modules_config_get)
        self.app.router.add_post("/api/modules/config/{name}", self._api_modules_config_save)
        self.app.router.add_post("/api/terminal/exec", self._api_terminal_exec)

    async def start_if_ready(
        self,
        total_count: int,
        port: int,
        proxy_pass: bool = False,
    ):
        if total_count <= len(self.client_data):
            if not self.running.is_set():
                await self.start(port, proxy_pass=proxy_pass)

            self.ready.set()

    async def get_url(self, proxy_pass: bool) -> str:
        url = None

        if all(option in os.environ for option in {"LAVHOST", "USER", "SERVER"}):
            return f"https://{os.environ['USER']}.{os.environ['SERVER']}.lavhost.ml"

        if proxy_pass:
            with contextlib.suppress(Exception):
                url = await self.proxypasser.get_url(timeout=10)

        if not url:
            ip = (
                "127.0.0.1"
                if "DOCKER" not in os.environ
                else subprocess.run(
                    ["hostname", "-i"],
                    stdout=subprocess.PIPE,
                    check=True,
                    timeout=5,
                    stderr=subprocess.PIPE,
                )
                .stdout.decode("utf-8")
                .strip()
            )

            ip = os.environ.get("HEROKU_IP", ip)

            url = f"http://{ip}:{self.port}"

        self.url = url
        return url

    async def start(self, port: int, proxy_pass: bool = False):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.port = os.environ.get("PORT", port)
        site = web.TCPSite(self.runner, None, self.port)
        self.proxypasser = proxypass.ProxyPasser(port=self.port)
        await site.start()

        await self.get_url(proxy_pass)

        # Initialize dashboard credentials
        data_root = os.environ.get("DATA_ROOT", _PROJECT_ROOT)
        self._web_creds = WebCredentials(data_root)
        self._dash_start_time = time.time()
        self._web_creds.log_credentials(self.port)

        self.running.set()
        print(f"Heroku Userbot Web Interface running on {self.port}")

    async def stop(self):
        await self.runner.shutdown()
        await self.runner.cleanup()
        self.running.clear()
        self.ready.clear()

    async def add_loader(
        self,
        client: CustomTelegramClient,
        loader: Modules,
        db: Database,
    ):
        self.client_data[client.tg_id] = (loader, client, db)

    @staticmethod
    async def favicon(_):
        return web.Response(
            status=301,
            headers={"Location": "https://i.imgur.com/IRAiWBo.jpeg"},
        )

    # ── Dashboard helpers ──────────────────────────────────────

    def _cleanup_expired(self):
        now = time.time()
        self._dash_sessions = {
            k: v for k, v in self._dash_sessions.items() if now - v < SESSION_TTL
        }
        self._dash_csrf_tokens = {
            k: v for k, v in self._dash_csrf_tokens.items() if now - v < 600
        }

    def _is_dash_authenticated(self, request: web.Request) -> bool:
        session = request.cookies.get("dash_session", "")
        if not session:
            return False
        created = self._dash_sessions.get(session, 0)
        if time.time() - created > SESSION_TTL:
            self._dash_sessions.pop(session, None)
            return False
        return True

    def _check_rate_limit(self, ip: str) -> bool:
        now = time.time()
        attempts = self._dash_auth_attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < AUTH_LOCKOUT_SECONDS]
        self._dash_auth_attempts[ip] = attempts
        return len(attempts) >= MAX_AUTH_ATTEMPTS

    def _record_attempt(self, ip: str):
        if ip not in self._dash_auth_attempts:
            self._dash_auth_attempts[ip] = []
        self._dash_auth_attempts[ip].append(time.time())

    def _create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self._dash_sessions[token] = time.time()
        return token

    def _validate_csrf(self, body: dict) -> bool:
        token = body.get("csrf_token", "")
        if not token or token not in self._dash_csrf_tokens:
            return False
        if time.time() - self._dash_csrf_tokens[token] > 600:
            del self._dash_csrf_tokens[token]
            return False
        del self._dash_csrf_tokens[token]
        return True

    # ── Dashboard pages ────────────────────────────────────────

    async def _dash_page(self, request: web.Request):
        if not self._is_dash_authenticated(request):
            raise web.HTTPFound("/login")
        with open(os.path.join(_STATIC_DIR, "dashboard.html"), "r", encoding="utf-8") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html")

    async def _dash_login_page(self, request: web.Request):
        if self._is_dash_authenticated(request):
            raise web.HTTPFound("/dashboard")
        with open(os.path.join(_STATIC_DIR, "login.html"), "r", encoding="utf-8") as f:
            html = f.read()
        return web.Response(text=html, content_type="text/html")

    # ── Auth API ───────────────────────────────────────────────

    async def _api_csrf(self, request: web.Request):
        self._cleanup_expired()
        token = secrets.token_urlsafe(24)
        self._dash_csrf_tokens[token] = time.time()
        return web.json_response({"csrf_token": token})

    async def _api_login(self, request: web.Request):
        ip = request.remote
        if self._check_rate_limit(ip):
            return web.json_response(
                {"error": "Too many attempts. Try again later."}, status=429
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid request"}, status=400)

        if not self._validate_csrf(body):
            return web.json_response({"error": "Invalid CSRF token"}, status=403)

        if not self._web_creds:
            return web.json_response({"error": "Server not ready"}, status=503)

        username = body.get("username", "")
        password = body.get("password", "")

        if not hmac.compare_digest(username, self._web_creds.username) or \
           not hmac.compare_digest(password, self._web_creds.password):
            self._record_attempt(ip)
            return web.json_response({"error": "Invalid credentials"}, status=401)

        session = self._create_session()
        resp = web.json_response({"success": True})
        resp.set_cookie(
            "dash_session", session,
            max_age=SESSION_TTL,
            httponly=True,
            samesite="Strict",
        )
        return resp

    async def _api_auth_key(self, request: web.Request):
        ip = request.remote
        if self._check_rate_limit(ip):
            return web.json_response(
                {"error": "Too many attempts. Try again later."}, status=429
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid request"}, status=400)

        if not self._validate_csrf(body):
            return web.json_response({"error": "Invalid CSRF token"}, status=403)

        if not self._web_creds:
            return web.json_response({"error": "Server not ready"}, status=503)

        auth_key = body.get("auth_key", "")
        if not hmac.compare_digest(auth_key, self._web_creds.auth_key):
            self._record_attempt(ip)
            return web.json_response({"error": "Invalid auth key"}, status=401)

        session = self._create_session()
        resp = web.json_response({"success": True})
        resp.set_cookie(
            "dash_session", session,
            max_age=SESSION_TTL,
            httponly=True,
            samesite="Strict",
        )
        return resp

    async def _api_auth_key_login(self, request: web.Request):
        token = request.match_info.get("token", "")
        if not self._web_creds or not hmac.compare_digest(token, self._web_creds.auth_key):
            return web.json_response({"error": "Invalid token"}, status=401)

        session = self._create_session()
        resp = web.HTTPFound("/dashboard")
        resp.set_cookie(
            "dash_session", session,
            max_age=SESSION_TTL,
            httponly=True,
            samesite="Strict",
        )
        return resp

    async def _api_logout(self, request: web.Request):
        session = request.cookies.get("dash_session", "")
        self._dash_sessions.pop(session, None)
        resp = web.json_response({"success": True})
        resp.del_cookie("dash_session")
        return resp

    # ── Dashboard API ─────────────────────────────────────────

    async def _api_dashboard(self, request: web.Request):
        if not self._is_dash_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        accounts_list = []
        for tg_id, (loader, client, db) in self.client_data.items():
            try:
                me = await client.get_me()
                accounts_list.append({
                    "id": me.id,
                    "name": getattr(me, "first_name", "Unknown"),
                    "username": getattr(me, "username", ""),
                    "phone": getattr(me, "phone", "Hidden"),
                    "online": client.is_connected(),
                    "modules": len(loader.modules) if hasattr(loader, "modules") else 0,
                })
            except Exception:
                accounts_list.append({
                    "id": tg_id,
                    "name": "Unknown",
                    "username": "",
                    "phone": "Hidden",
                    "online": False,
                    "modules": 0,
                })

        total_modules = 0
        for tg_id, (loader, client, db) in self.client_data.items():
            if hasattr(loader, "modules"):
                total_modules += len(loader.modules)

        uptime = time.time() - self._dash_start_time if self._dash_start_time else 0

        return web.json_response({
            "accounts": len(self.client_data),
            "modules": total_modules,
            "uptime": int(uptime),
            "sessions": len(self._dash_sessions),
            "accounts_list": accounts_list,
        })

    async def _api_modules(self, request: web.Request):
        if not self._is_dash_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        modules_list = []
        for tg_id, (loader, client, db) in self.client_data.items():
            if not hasattr(loader, "modules"):
                continue
            for name, mod in loader.modules.items():
                modules_list.append({
                    "name": name,
                    "description": inspect.getdoc(mod) or "",
                    "enabled": not getattr(mod, "disabled", False),
                    "core": getattr(mod, "core", False),
                    "commands": [
                        {"name": cmd.name, "description": cmd.doc or ""}
                        for cmd in getattr(mod, "commands", [])
                    ] if hasattr(mod, "commands") else [],
                })

        return web.json_response({"modules": modules_list})

    async def _api_modules_toggle(self, request: web.Request):
        if not self._is_dash_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid request"}, status=400)

        if not self._validate_csrf(body):
            return web.json_response({"error": "Invalid CSRF token"}, status=403)

        module_name = body.get("module", "")
        enabled = body.get("enabled", True)

        for tg_id, (loader, client, db) in self.client_data.items():
            if not hasattr(loader, "modules"):
                continue
            if module_name in loader.modules:
                mod = loader.modules[module_name]
                if getattr(mod, "core", False):
                    return web.json_response(
                        {"error": "Core modules cannot be disabled"}, status=403
                    )
                if enabled:
                    mod.disabled = False
                    if hasattr(mod, "on_enable"):
                        await mod.on_enable()
                else:
                    mod.disabled = True
                    if hasattr(mod, "on_disable"):
                        await mod.on_disable()
                return web.json_response({"success": True})

        return web.json_response({"error": "Module not found"}, status=404)

    async def _api_modules_config_get(self, request: web.Request):
        if not self._is_dash_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        name = request.match_info.get("name", "")

        for tg_id, (loader, client, db) in self.client_data.items():
            if not hasattr(loader, "modules"):
                continue
            if name in loader.modules:
                mod = loader.modules[name]
                config = {}
                if hasattr(mod, "_db"):
                    try:
                        config = mod._db.get(mod.__class__.__name__, "config") or {}
                    except Exception:
                        pass
                commands = [
                    {"name": cmd.name, "description": cmd.doc or ""}
                    for cmd in getattr(mod, "commands", [])
                ] if hasattr(mod, "commands") else []
                return web.json_response({
                    "name": name,
                    "config": config,
                    "commands": commands,
                })

        return web.json_response({"error": "Module not found"}, status=404)

    async def _api_modules_config_save(self, request: web.Request):
        if not self._is_dash_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid request"}, status=400)

        if not self._validate_csrf(body):
            return web.json_response({"error": "Invalid CSRF token"}, status=403)

        name = request.match_info.get("name", "")
        key = body.get("key", "")
        value = body.get("value")

        for tg_id, (loader, client, db) in self.client_data.items():
            if not hasattr(loader, "modules"):
                continue
            if name in loader.modules:
                mod = loader.modules[name]
                if hasattr(mod, "_db"):
                    try:
                        config = mod._db.get(mod.__class__.__name__, "config") or {}
                        config[key] = value
                        mod._db.set(mod.__class__.__name__, "config", config)
                        return web.json_response({"success": True})
                    except Exception as e:
                        return web.json_response({"error": str(e)}, status=500)
                return web.json_response(
                    {"error": "Module has no database"}, status=400
                )

        return web.json_response({"error": "Module not found"}, status=404)

    async def _api_terminal_exec(self, request: web.Request):
        if not self._is_dash_authenticated(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid request"}, status=400)

        if not self._validate_csrf(body):
            return web.json_response({"error": "Invalid CSRF token"}, status=403)

        command = body.get("command", "").strip()
        if not command:
            return web.json_response({"error": "Empty command"}, status=400)

        dangerous = [
            "rm -rf", "mkfs", "dd if=", ":(){ :|:&", "> /dev/sd",
            "shutdown", "reboot", "poweroff", "halt",
            "curl.*\\|.*sh", "wget.*\\|.*sh",
            "format ", "del /", "rd /s",
        ]
        import re
        for pattern in dangerous:
            if re.search(pattern, command, re.IGNORECASE):
                return web.json_response(
                    {"error": "Command blocked for security"}, status=403
                )

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode("utf-8", errors="replace")
            if stderr:
                output += "\n" + stderr.decode("utf-8", errors="replace")
            return web.json_response({"output": output or "(no output)"})
        except asyncio.TimeoutError:
            return web.json_response({"error": "Command timed out (10s)"}, status=408)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
