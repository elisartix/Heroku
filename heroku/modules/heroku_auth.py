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
import logging

from .. import main, utils
from ..loader import Loader

logger = logging.getLogger(__name__)


@Loader.mod(name="heroku_auth", author="Heroku")
class HerokuAuthMod(Loader):
    """Authenticate web dashboard via Telegram"""

    strings = {
        "name": "heroku_auth",
        "auth_success": (
            "🪐 <b>Web Dashboard Auth</b>\n\n"
            "✅ Successfully authenticated!\n\n"
            "🔗 <a href=\"{url}\">Click here to open dashboard</a>\n\n"
            "<i>This link will auto-login you in the browser</i>"
        ),
        "auth_invalid": "🪐 <b>Web Dashboard Auth</b>\n\n❌ Invalid auth key",
        "auth_no_web": "🪐 <b>Web Dashboard Auth</b>\n\n❌ Web dashboard is not running",
        "webcreds": (
            "🪐 <b>Web Dashboard Credentials</b>\n\n"
            "👤 Username: <code>{username}</code>\n"
            "🔑 Password: <code>{password}</code>\n"
            "🔐 Auth Key: <code>{auth_key}</code>\n\n"
            "🔗 <a href=\"{url}\">Open Dashboard</a>\n\n"
            "<i>This message will self-destruct in 60 seconds</i>"
        ),
    }

    async def client_ready(self, client, db):
        self._client = client

    @Loader.command()
    async def auth(self, message):
        """<key> - Authenticate web dashboard via auth key"""
        args = utils.get_args(message)

        if not args:
            await utils.answer(message, "🪐 Usage: <code>.auth &lt;key&gt;</code>")
            return

        if not main.heroku.web or not main.heroku.web.running.is_set():
            await utils.answer(message, self.strings("auth_no_web"))
            return

        web = main.heroku.web
        if not web._web_creds:
            await utils.answer(message, self.strings("auth_no_web"))
            return

        key = args.strip()
        if key != web._web_creds.auth_key:
            await utils.answer(message, self.strings("auth_invalid"))
            return

        # Build one-click login URL
        base_url = getattr(web, "url", f"http://127.0.0.1:{web.port}")
        login_url = f"{base_url}/api/auth_key_login/{web._web_creds.auth_key}"

        await utils.answer(
            message,
            self.strings("auth_success").format(url=login_url),
        )

    @Loader.command()
    async def webcreds(self, message):
        """Show web dashboard credentials (self-destructing)"""
        if not main.heroku.web or not main.heroku.web.running.is_set():
            await utils.answer(message, "🪐 Web dashboard is not running")
            return

        web = main.heroku.web
        if not web._web_creds:
            await utils.answer(message, "🪐 Web dashboard is not running")
            return

        base_url = getattr(web, "url", f"http://127.0.0.1:{web.port}")

        msg = await utils.answer(
            message,
            self.strings("webcreds").format(
                username=web._web_creds.username,
                password=web._web_creds.password,
                auth_key=web._web_creds.auth_key,
                url=f"{base_url}/login",
            ),
        )

        # Self-destruct after 60 seconds
        await asyncio.sleep(60)
        try:
            await msg.delete()
        except Exception:
            pass
