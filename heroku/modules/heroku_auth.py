# ©️ Heroku Userbot
# 🌐 https://github.com/coddrago/Heroku
# You can redistribute it and/or modify it under the terms of the GNU AGPLv3
# 🔑 https://www.gnu.org/licenses/agpl-3.0.html

import logging

from herokutl.tl.custom import Message

from .. import loader, utils

logger = logging.getLogger(__name__)


@loader.tds
class HerokuAuthMod(loader.Module):
    """Web dashboard authentication via Telegram"""

    strings = {
        "name": "HerokuAuth",
        "auth_success": (
            "🪐 <b>Web Dashboard authenticated!</b>\n\n"
            "<b>Session created.</b> You can now access the dashboard in your browser.\n"
            "<i>Session expires in 1 hour.</i>"
        ),
        "auth_fail": "🚫 <b>Invalid auth key.</b>",
        "auth_usage": (
            "🪐 <b>Heroku Web Dashboard Auth</b>\n\n"
            "<b>Usage:</b> <code>{prefix}auth &lt;key&gt;</code>\n\n"
            "The auth key is shown in bot logs on every startup.\n"
            "Use it to create a web session without login/password."
        ),
        "no_web": "🚫 <b>Web dashboard is not running.</b>",
        "credentials_info": (
            "🪐 <b>Web Dashboard Credentials</b>\n\n"
            "<b>Username:</b> <code>{username}</code>\n"
            "<b>Password:</b> <tg-spoiler>{password}</tg-spoiler>\n"
            "<b>Auth Key:</b> <tg-spoiler>{auth_key}</tg-spoiler>\n\n"
            "<b>URL:</b> <code>http://localhost:{port}</code>\n\n"
            "<i>⚠️ This message will self-destruct in 60 seconds</i>"
        ),
    }

    strings_ru = {
        "auth_success": (
            "🪐 <b>Веб-панель авторизована!</b>\n\n"
            "<b>Сессия создана.</b> Теперь вы можете открыть дашборд в браузере.\n"
            "<i>Сессия истекает через 1 час.</i>"
        ),
        "auth_fail": "🚫 <b>Неверный ключ авторизации.</b>",
        "auth_usage": (
            "🪐 <b>Авторизация веб-панели Heroku</b>\n\n"
            "<b>Использование:</b> <code>{prefix}auth &lt;ключ&gt;</code>\n\n"
            "Ключ авторизации показывается в логах бота при каждом запуске.\n"
            "Используйте его для создания веб-сессии без логина/пароля."
        ),
        "no_web": "🚫 <b>Веб-панель не запущена.</b>",
        "credentials_info": (
            "🪐 <b>Данные для входа в веб-панель</b>\n\n"
            "<b>Логин:</b> <code>{username}</code>\n"
            "<b>Пароль:</b> <tg-spoiler>{password}</tg-spoiler>\n"
            "<b>Ключ:</b> <tg-spoiler>{auth_key}</tg-spoiler>\n\n"
            "<b>URL:</b> <code>http://localhost:{port}</code>\n\n"
            "<i>⚠️ Сообщение самоуничтожится через 60 секунд</i>"
        ),
    }

    @loader.command()
    async def auth(self, message: Message):
        """<key> - Authenticate web dashboard session"""
        from ..web.core import WebDashboard

        web = getattr(self._client, "_heroku_web", None)
        if not web or not isinstance(web, WebDashboard):
            await utils.answer(message, self.strings("no_web"))
            return

        args = utils.get_args_raw(message)
        if not args:
            await utils.answer(
                message,
                self.strings("auth_usage").format(prefix=self.get_prefix()),
            )
            return

        import hmac

        if hmac.compare_digest(args.strip(), web.credentials.auth_key):
            session_token = web._create_session()
            import os
            port = int(os.environ.get("PORT", web.port))
            await utils.answer(
                message,
                self.strings("auth_success")
                + f"\n\n<b>🔗 One-click login:</b> <code>http://localhost:{port}/api/auth_key_login/{session_token}</code>"
                + f"\n<i>⚠️ This link expires in 1 hour. Do not share it!</i>",
            )
        else:
            await utils.answer(message, self.strings("auth_fail"))

        await message.delete()

    @loader.command()
    async def webcreds(self, message: Message):
        """Show web dashboard credentials (self-destructing)"""
        import asyncio

        from ..web.core import WebDashboard

        web = getattr(self._client, "_heroku_web", None)
        if not web or not isinstance(web, WebDashboard):
            await utils.answer(message, self.strings("no_web"))
            return

        import os

        port = int(os.environ.get("PORT", web.port))

        msg = await utils.answer(
            message,
            self.strings("credentials_info").format(
                username=utils.escape_html(web.credentials.username),
                password=web.credentials.password,
                auth_key=web.credentials.auth_key,
                port=port,
            ),
        )

        await asyncio.sleep(60)
        try:
            await msg.delete()
        except Exception:
            pass
