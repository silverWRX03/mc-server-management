"""Posting a server's invite for friends to a Discord channel, through the user's own bot.

A webhook only reaches the one channel it was made for, so mcsm uses a bot the user creates
once (discord.com/developers → New Application → Bot → Reset Token) and adds to their
Discord server. With its token mcsm can list the servers the bot is in and their text
channels, and post there. The bot only needs to see channels and send messages; mcsm never
reads messages, and posts can't mention @everyone or anyone else.
"""

from __future__ import annotations

import re

from . import __version__
from .http import HttpClient, HttpError

API = "https://discord.com/api/v10"
DEVELOPER_PORTAL = "https://discord.com/developers/applications"
# View Channels (1024) + Send Messages (2048) + Embed Links (16384)
PERMISSIONS = 1024 | 2048 | 16384
TEXT_CHANNELS = {0: "text", 5: "announcements"}
TOKEN = re.compile(r"[A-Za-z0-9_.-]{50,120}")
SNOWFLAKE = re.compile(r"\d{15,22}")
MAX_MESSAGE = 1800


class DiscordError(ValueError):
    pass


class Discord:
    def __init__(self, http: HttpClient, token: str):
        self.http = http
        self.token = token

    @property
    def _headers(self) -> dict:
        # Discord asks bots to say who they are in this form.
        return {"Authorization": f"Bot {self.token}",
                "User-Agent": f"DiscordBot (https://github.com/silverWRX03/mc-server-management, {__version__})"}

    def _get(self, path: str):
        try:
            return self.http.get_json(f"{API}{path}", headers=self._headers, cache=False)
        except HttpError as e:
            raise self._explain(e) from None

    @staticmethod
    def _explain(e: HttpError) -> Exception:
        if e.status == 401:
            return DiscordError("Discord didn't accept the bot token; copy it again (Bot → Reset Token)")
        if e.status == 403:
            return DiscordError("the bot isn't allowed to do that there; check it can see the channel and send messages")
        if e.status == 404:
            return DiscordError("Discord couldn't find that server or channel (is the bot still in it?)")
        return e

    def me(self) -> dict:
        """The bot: its id (for the invite link) and name. Also checks the token."""
        u = self._get("/users/@me")
        return {"id": str(u["id"]), "name": str(u.get("username", "bot"))}

    @staticmethod
    def invite_url(bot_id: str) -> str:
        """The page where the user adds the bot to one of their Discord servers."""
        return f"https://discord.com/oauth2/authorize?client_id={bot_id}&scope=bot&permissions={PERMISSIONS}"

    def guilds(self) -> list[dict]:
        return sorted(({"id": str(g["id"]), "name": str(g.get("name", ""))} for g in self._get("/users/@me/guilds")),
                      key=lambda g: g["name"].lower())

    def channels(self, guild_id: str) -> list[dict]:
        if not SNOWFLAKE.fullmatch(guild_id):
            raise DiscordError("that isn't a Discord server")
        items = self._get(f"/guilds/{guild_id}/channels")
        categories = {str(c["id"]): str(c.get("name", "")) for c in items if c.get("type") == 4}
        out = [{"id": str(c["id"]), "name": str(c.get("name", "")), "kind": TEXT_CHANNELS[c["type"]],
                "category": categories.get(str(c.get("parent_id")), ""), "position": int(c.get("position", 0))}
               for c in items if c.get("type") in TEXT_CHANNELS]
        return sorted(out, key=lambda c: (c["category"].lower(), c["position"]))

    def post(self, channel_id: str, content: str, embed: dict | None = None) -> dict:
        if not SNOWFLAKE.fullmatch(channel_id):
            raise DiscordError("that isn't a Discord channel")
        body = {"content": content[:2000], "allowed_mentions": {"parse": []}}
        if embed:
            body["embeds"] = [embed]
        try:
            msg = self.http.post_json(f"{API}/channels/{channel_id}/messages", body, headers=self._headers)
        except HttpError as e:
            raise self._explain(e) from None
        return {"id": str(msg.get("id", "")), "channel": channel_id}


def check_token(token: str) -> str:
    token = token.strip()
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    if not TOKEN.fullmatch(token):
        raise DiscordError("that doesn't look like a bot token (Bot → Reset Token → Copy)")
    return token


def invite_message(text: str, name: str, minecraft: str, links: dict[str, str]) -> tuple[str, dict]:
    """The message: the user's text, and a card with the server and its download links."""
    text = text.strip()[:MAX_MESSAGE]
    lines = []
    if links.get("internet"):
        lines.append(f"**Download:** {links['internet']}")
    if links.get("local"):
        lines.append(f"**On the same Wi-Fi/network:** {links['local']}")
    embed = {"title": name[:200], "description": "\n".join(lines)[:3500],
             "footer": {"text": f"Minecraft {minecraft} · open the link, run the download, and it sets up your game"},
             "color": 0x3BA55C}
    return text, embed
