"""Heroku Web Dashboard - Core server"""

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
import typing

from aiohttp import web

from .credentials import WebCredentials

logger = logging.getLogger(__name__)

SESSION_TTL = 3600  # 1 hour
MAX_AUTH_ATTEMPTS = 5
AUTH_LOCKOUT_SECONDS = 300

# Resolve web-resources relative to project root
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_STATIC_DIR = os.path.join(_PROJECT_ROOT, "web-resources", "static")


class WebDashboard:
    def __init__(self, data_root: str, port: int = 8080):
        self.data_root = data_root
        self.port = port
        self.credentials = WebCredentials(data_root)
        self.app = web.Application()
        self.runner: typing.Optional[web.AppRunner] = None
        self.running = asyncio.Event()

        self._sessions: dict[str, float] = {}
        self._csrf_tokens: dict[str, float] = {}
        self._auth_attempts: dict[str, list[float]] = {}
        self._client_data: dict[int, tuple] = {}

        self._setup_routes()
        self.app.middlewares.append(self._security_headers_middleware)

    def _setup_routes(self):
        self.app.router.add_get("/", self._handle_root)
        self.app.router.add_get("/login", self._handle_login_page)
        self.app.router.add_post("/api/login", self._handle_login)
        self.app.router.add_post("/api/auth_key", self._handle_auth_key)
        self.app.router.add_get("/api/dashboard", self._handle_dashboard_data)
        self.app.router.add_get("/api/modules", self._handle_modules_list)
        self.app.router.add_post("/api/modules/toggle", self._handle_module_toggle)
        self.app.router.add_get(
            "/api/modules/config/{name}", self._handle_module_config
        )
        self.app.router.add_post(
            "/api/modules/config/{name}", self._handle_module_config_save
        )
        self.app.router.add_post("/api/terminal/exec", self._handle_terminal_exec)
        self.app.router.add_post("/api/logout", self._handle_logout)
        self.app.router.add_get("/api/csrf", self._handle_csrf)
        self.app.router.add_get(
            "/api/auth_key_login/{token}", self._handle_auth_key_login
        )
        self.app.router.add_static("/static/", _STATIC_DIR)

    @web.middleware
    async def _security_headers_middleware(self, request: web.Request, handler):
        response = await handler(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def _get_client_ip(self, request: web.Request) -> str:
        return request.remote or "unknown"

    def _is_rate_limited(self, ip: str) -> bool:
        now = time.time()
        attempts = self._auth_attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < AUTH_LOCKOUT_SECONDS]
        self._auth_attempts[ip] = attempts
        return len(attempts) >= MAX_AUTH_ATTEMPTS

    def _record_attempt(self, ip: str):
        now = time.time()
        if ip not in self._auth_attempts:
            self._auth_attempts[ip] = []
        self._auth_attempts[ip].append(now)

    def _create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time()
        return token

    def _validate_session(self, request: web.Request) -> bool:
        token = request.cookies.get("heroku_session")
        if not token or token not in self._sessions:
            return False
        created = self._sessions[token]
        if time.time() - created > SESSION_TTL:
            del self._sessions[token]
            return False
        return True

    def _create_csrf_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self._csrf_tokens[token] = time.time()
        return token

    def _validate_csrf(self, token: str) -> bool:
        if not token or token not in self._csrf_tokens:
            return False
        created = self._csrf_tokens[token]
        if time.time() - created > 600:
            del self._csrf_tokens[token]
            return False
        del self._csrf_tokens[token]
        return True

    def _cleanup_expired(self):
        now = time.time()
        self._sessions = {
            k: v for k, v in self._sessions.items() if now - v < SESSION_TTL
        }
        self._csrf_tokens = {
            k: v for k, v in self._csrf_tokens.items() if now - v < 600
        }

    def _get_primary_client(self):
        """Return the first available client data tuple"""
        for tg_id, data in self._client_data.items():
            return data
        return None

    # ── Page Routes ──────────────────────────────────────────

    async def _handle_root(self, request: web.Request) -> web.Response:
        if self._validate_session(request):
            raise web.HTTPFound("/static/dashboard.html")
        raise web.HTTPFound("/login")

    async def _handle_login_page(self, request: web.Request) -> web.Response:
        if self._validate_session(request):
            raise web.HTTPFound("/static/dashboard.html")
        return web.FileResponse(os.path.join(_STATIC_DIR, "login.html"))

    async def _handle_csrf(self, request: web.Request) -> web.Response:
        token = self._create_csrf_token()
        return web.json_response({"csrf_token": token})

    # ── Auth Routes ──────────────────────────────────────────

    async def _handle_login(self, request: web.Request) -> web.Response:
        ip = self._get_client_ip(request)
        if self._is_rate_limited(ip):
            return web.json_response(
                {"error": "Too many attempts. Try again later."},
                status=429,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid request"}, status=400)

        username = data.get("username", "")
        password = data.get("password", "")
        csrf = data.get("csrf_token", "")

        if not self._validate_csrf(csrf):
            return web.json_response({"error": "Invalid CSRF token"}, status=403)

        if not (
            hmac.compare_digest(username, self.credentials.username)
            and hmac.compare_digest(password, self.credentials.password)
        ):
            self._record_attempt(ip)
            return web.json_response(
                {"error": "Invalid credentials"},
                status=401,
            )

        session_token = self._create_session()
        response = web.json_response({"status": "ok"})
        response.set_cookie(
            "heroku_session",
            session_token,
            httponly=True,
            samesite="Strict",
            max_age=SESSION_TTL,
        )
        return response

    async def _handle_auth_key(self, request: web.Request) -> web.Response:
        ip = self._get_client_ip(request)
        if self._is_rate_limited(ip):
            return web.json_response(
                {"error": "Too many attempts. Try again later."},
                status=429,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid request"}, status=400)

        key = data.get("auth_key", "")

        if not hmac.compare_digest(key, self.credentials.auth_key):
            self._record_attempt(ip)
            return web.json_response(
                {"error": "Invalid auth key"},
                status=401,
            )

        session_token = self._create_session()
        response = web.json_response({"status": "ok"})
        response.set_cookie(
            "heroku_session",
            session_token,
            httponly=True,
            samesite="Strict",
            max_age=SESSION_TTL,
        )
        return response

    async def _handle_logout(self, request: web.Request) -> web.Response:
        token = request.cookies.get("heroku_session")
        if token and token in self._sessions:
            del self._sessions[token]
        response = web.json_response({"status": "ok"})
        response.del_cookie("heroku_session")
        return response

    # ── Dashboard Data ───────────────────────────────────────

    async def _handle_dashboard_data(self, request: web.Request) -> web.Response:
        if not self._validate_session(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        self._cleanup_expired()

        accounts_list = []
        total_modules = 0
        for tg_id, (modules, client, db) in self._client_data.items():
            try:
                me = client.heroku_me
                first_name = getattr(me, "first_name", "Unknown")
                username = getattr(me, "username", None)
                phone = getattr(me, "phone", None)
                mod_count = len(modules.modules) if modules else 0
                total_modules += mod_count
                accounts_list.append({
                    "id": tg_id,
                    "name": first_name,
                    "username": username,
                    "phone": phone,
                    "online": client.is_connected(),
                    "modules": mod_count,
                })
            except Exception:
                accounts_list.append({
                    "id": tg_id,
                    "name": "Unknown",
                    "online": False,
                })

        uptime = (
            int(time.time() - self._start_time)
            if hasattr(self, "_start_time")
            else 0
        )

        return web.json_response({
            "accounts": len(accounts_list),
            "accounts_list": accounts_list,
            "modules": total_modules,
            "uptime": uptime,
            "sessions": len(self._sessions),
        })

    # ── Modules API ──────────────────────────────────────────

    async def _handle_modules_list(self, request: web.Request) -> web.Response:
        if not self._validate_session(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        modules_list = []
        client_data = self._get_primary_client()
        if not client_data:
            return web.json_response({"modules": []})

        modules_obj, client, db = client_data

        for name, mod in modules_obj.modules.items():
            try:
                is_core = getattr(mod, "core", False)
                is_enabled = not getattr(mod, "disabled", False)
                description = getattr(mod, "strings", {}).get(
                    "name", ""
                ) or getattr(mod, "__doc__", "") or ""
                commands = []
                for cmd_name in getattr(mod, "commands", {}):
                    cmd = mod.commands[cmd_name]
                    cmd_desc = getattr(cmd, "__doc__", "") or ""
                    commands.append({"name": cmd_name, "description": cmd_desc})

                modules_list.append({
                    "name": name,
                    "description": description.strip().split("\n")[0],
                    "core": is_core,
                    "enabled": is_enabled,
                    "commands": commands,
                })
            except Exception:
                modules_list.append({
                    "name": name,
                    "description": "",
                    "core": False,
                    "enabled": True,
                    "commands": [],
                })

        return web.json_response({"modules": modules_list})

    async def _handle_module_toggle(self, request: web.Request) -> web.Response:
        if not self._validate_session(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid request"}, status=400)

        csrf = data.get("csrf_token", "")
        if not self._validate_csrf(csrf):
            return web.json_response({"error": "Invalid CSRF token"}, status=403)

        module_name = data.get("module", "")
        enabled = data.get("enabled", True)

        client_data = self._get_primary_client()
        if not client_data:
            return web.json_response({"error": "No client"}, status=400)

        modules_obj, client, db = client_data

        mod = modules_obj.modules.get(module_name)
        if not mod:
            return web.json_response({"error": "Module not found"}, status=404)

        if getattr(mod, "core", False):
            return web.json_response(
                {"error": "Core modules cannot be disabled"}, status=403
            )

        try:
            if enabled:
                await modules_obj.load_module(module_name, client)
            else:
                await modules_obj.unload_module(module_name, client)
        except Exception as e:
            return web.json_response(
                {"error": f"Failed to toggle: {e}"}, status=500
            )

        return web.json_response({"success": True})

    async def _handle_module_config(
        self, request: web.Request
    ) -> web.Response:
        if not self._validate_session(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        module_name = request.match_info["name"]

        client_data = self._get_primary_client()
        if not client_data:
            return web.json_response({"error": "No client"}, status=400)

        modules_obj, client, db = client_data

        mod = modules_obj.modules.get(module_name)
        if not mod:
            return web.json_response({"error": "Module not found"}, status=404)

        config = {}
        try:
            if hasattr(mod, "config"):
                for key, val in mod.config.items():
                    config[key] = val.get() if callable(getattr(val, "get", None)) else val
        except Exception:
            pass

        commands = []
        try:
            for cmd_name in getattr(mod, "commands", {}):
                cmd = mod.commands[cmd_name]
                cmd_desc = getattr(cmd, "__doc__", "") or ""
                commands.append({"name": cmd_name, "description": cmd_desc})
        except Exception:
            pass

        return web.json_response({
            "config": config,
            "commands": commands,
        })

    async def _handle_module_config_save(
        self, request: web.Request
    ) -> web.Response:
        if not self._validate_session(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        module_name = request.match_info["name"]

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid request"}, status=400)

        csrf = data.get("csrf_token", "")
        if not self._validate_csrf(csrf):
            return web.json_response({"error": "Invalid CSRF token"}, status=403)

        key = data.get("key", "")
        value = data.get("value", "")

        client_data = self._get_primary_client()
        if not client_data:
            return web.json_response({"error": "No client"}, status=400)

        modules_obj, client, db = client_data

        mod = modules_obj.modules.get(module_name)
        if not mod:
            return web.json_response({"error": "Module not found"}, status=404)

        try:
            if hasattr(mod, "config") and key in mod.config:
                config_val = mod.config[key]
                if hasattr(config_val, "set"):
                    config_val.set(value)
                else:
                    mod.config[key] = value
            else:
                return web.json_response(
                    {"error": "Config key not found"}, status=404
                )
        except Exception as e:
            return web.json_response(
                {"error": f"Failed to save: {e}"}, status=500
            )

        return web.json_response({"success": True})

    # ── Terminal API ─────────────────────────────────────────

    async def _handle_terminal_exec(self, request: web.Request) -> web.Response:
        if not self._validate_session(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid request"}, status=400)

        csrf = data.get("csrf_token", "")
        if not self._validate_csrf(csrf):
            return web.json_response({"error": "Invalid CSRF token"}, status=403)

        command = data.get("command", "").strip()
        if not command:
            return web.json_response({"error": "Empty command"}, status=400)

        # Block obviously dangerous commands
        dangerous = ["rm -rf", "mkfs", "dd if=", ":(){ :|:&", "shutdown", "reboot"]
        cmd_lower = command.lower()
        for d in dangerous:
            if d in cmd_lower:
                return web.json_response(
                    {"error": f"Command blocked: dangerous pattern"}, status=403
                )

        client_data = self._get_primary_client()
        if not client_data:
            return web.json_response({"error": "No client"}, status=400)

        modules_obj, client, db = client_data

        try:
            # Try to dispatch command through the module loader
            prefix = db.get("heroku.main", "command_prefix", ".")
            if not command.startswith(prefix):
                command = prefix + command

            # Find matching module command
            parts = command[len(prefix):].split(" ", 1)
            cmd_name = parts[0]
            args = parts[1] if len(parts) > 1 else ""

            # Search through modules for the command
            for mod_name, mod in modules_obj.modules.items():
                if hasattr(mod, "commands") and cmd_name in mod.commands:
                    # Create a fake message for command execution
                    from herokutl.tl.custom import Message
                    from .. import utils

                    # Execute via terminal module if available
                    terminal_mod = modules_obj.modules.get("Terminal")
                    if terminal_mod and hasattr(terminal_mod, "executecmd"):
                        result = await asyncio.create_subprocess_shell(
                            command,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, stderr = await asyncio.wait_for(
                            result.communicate(), timeout=30
                        )
                        output = stdout.decode("utf-8", errors="replace")
                        err = stderr.decode("utf-8", errors="replace")
                        if err and not output:
                            return web.json_response({"error": err})
                        return web.json_response({"output": output or "(no output)"})

                    break

            # Fallback: execute as shell command
            result = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                result.communicate(), timeout=30
            )
            output = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")

            if err and not output:
                return web.json_response({"error": err})
            return web.json_response({"output": output or "(no output)"})

        except asyncio.TimeoutError:
            return web.json_response(
                {"error": "Command timed out (30s)"}, status=408
            )
        except Exception as e:
            return web.json_response(
                {"error": f"Execution error: {e}"}, status=500
            )

    async def _handle_auth_key_login(
        self, request: web.Request
    ) -> web.Response:
        """One-click login via session token from .auth command"""
        token = request.match_info.get("token", "")
        if not token or token not in self._sessions:
            return web.json_response(
                {"error": "Invalid or expired login link"}, status=401
            )

        # Token is valid — set cookie and redirect to dashboard
        response = web.HTTPFound("/static/dashboard.html")
        response.set_cookie(
            "heroku_session",
            token,
            httponly=True,
            samesite="Strict",
            max_age=SESSION_TTL,
        )
        return response

    # ── Lifecycle ────────────────────────────────────────────

    def add_client(self, tg_id: int, modules, client, db):
        self._client_data[tg_id] = (modules, client, db)

    async def start(self):
        self._start_time = time.time()
        self.credentials.log_credentials()

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        port = int(os.environ.get("PORT", self.port))
        site = web.TCPSite(self.runner, "0.0.0.0", port)
        await site.start()

        self.running.set()
        logger.info("Heroku Web Dashboard running on port %d", port)

    async def stop(self):
        if self.runner:
            await self.runner.shutdown()
            await self.runner.cleanup()
        self.running.clear()
