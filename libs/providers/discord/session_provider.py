from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from libs.core.redaction import redact_string
from libs.providers.discord.provider import DISCORD_API_BASE, DiscordAPIError


@dataclass(frozen=True)
class DiscordSessionAuth:
    """Approved local Discord Web session material for read-only sync.

    The caller owns how this material is captured/stored. This provider only
    sends Discord Web-compatible GET requests and never exposes outbound write
    methods.
    """

    cookie_header: str
    user_agent: str | None = None
    x_super_properties: str | None = None
    authorization: str | None = None
    locale: str | None = "en-US"

    @classmethod
    def from_material(cls, material: dict[str, Any]) -> "DiscordSessionAuth":
        if material.get("kind") != "session:web":
            raise ValueError("Stored Discord credential is not session:web material")
        return cls(
            cookie_header=str(material.get("cookie_header") or ""),
            user_agent=material.get("user_agent"),
            x_super_properties=material.get("x_super_properties"),
            authorization=material.get("authorization"),
            locale=material.get("locale") or "en-US",
        )

    @classmethod
    def from_sources(
        cls,
        *,
        cookie_header: str | None = None,
        session_state_path: str | None = None,
        authorization: str | None = None,
        user_agent: str | None = None,
        x_super_properties: str | None = None,
        locale: str | None = "en-US",
    ) -> "DiscordSessionAuth":
        resolved_cookie = (cookie_header or "").strip()
        if not resolved_cookie and session_state_path:
            resolved_cookie = _cookie_header_from_storage_state(session_state_path)
        if not resolved_cookie:
            raise ValueError("Provide cookie_header or session_state_path for an approved Discord Web session")
        return cls(
            cookie_header=resolved_cookie,
            user_agent=user_agent,
            x_super_properties=x_super_properties,
            authorization=authorization,
            locale=locale,
        )

    def to_material(self) -> dict[str, Any]:
        return {
            "kind": "session:web",
            "cookie_header": self.cookie_header,
            "user_agent": self.user_agent,
            "x_super_properties": self.x_super_properties,
            "authorization": self.authorization,
            "locale": self.locale,
        }

    def headers(self) -> dict[str, str]:
        headers = {
            "Cookie": self.cookie_header,
            "Accept": "application/json",
            "Referer": "https://discord.com/channels/@me",
        }
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        if self.x_super_properties:
            headers["X-Super-Properties"] = self.x_super_properties
        if self.authorization:
            # Some approved local web sessions expose the web authorization
            # value alongside cookies. Treat it as session material, not a bot.
            headers["Authorization"] = self.authorization
        if self.locale:
            headers["Accept-Language"] = self.locale
        return headers


def _cookie_header_from_storage_state(session_state_path: str) -> str:
    path = Path(session_state_path).expanduser()
    payload = json.loads(path.read_text())
    cookies = payload.get("cookies")
    if not isinstance(cookies, list):
        raise ValueError("session_state_path must point to a browser storage-state JSON with cookies[]")
    parts: list[str] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "")
        if "discord.com" not in domain:
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if name and value is not None:
            parts.append(f"{name}={value}")
    if not parts:
        raise ValueError("session_state_path did not contain discord.com cookies")
    return "; ".join(parts)


class DiscordSessionProvider:
    """Read-only Discord Web session client.

    Uses only endpoints the logged-in user can already read through Discord Web.
    It intentionally has no send/reaction/join/delete/moderation methods.
    """

    def __init__(
        self,
        *,
        auth: DiscordSessionAuth,
        client: httpx.Client | None = None,
        api_base: str = DISCORD_API_BASE,
    ):
        if not auth.cookie_header.strip():
            raise ValueError("Discord session cookie_header must be non-empty")
        self.auth = auth
        self.api_base = api_base.rstrip("/")
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get_current_user(self) -> dict[str, Any]:
        data = self._get("/users/@me", route="GET /users/@me (session:web)")
        if not isinstance(data, dict):
            raise DiscordAPIError(502, "Discord returned a non-object user payload", route="GET /users/@me (session:web)")
        return data

    def list_user_guilds(self) -> list[dict[str, Any]]:
        data = self._get("/users/@me/guilds", route="GET /users/@me/guilds (session:web)")
        if not isinstance(data, list):
            raise DiscordAPIError(502, "Discord returned a non-list guild payload", route="GET /users/@me/guilds (session:web)")
        return data

    def list_guild_channels(self, guild_id: str) -> list[dict[str, Any]]:
        data = self._get(f"/guilds/{guild_id}/channels", route=f"GET /guilds/{guild_id}/channels (session:web)")
        if not isinstance(data, list):
            raise DiscordAPIError(502, "Discord returned a non-list channel payload", route=f"GET /guilds/{guild_id}/channels (session:web)")
        return data

    def list_channel_messages(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        before: str | None = None,
        after: str | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        params: dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        data = self._get(f"/channels/{channel_id}/messages", params=params, route=f"GET /channels/{channel_id}/messages (session:web)")
        if not isinstance(data, list):
            raise DiscordAPIError(502, "Discord returned a non-list message payload", route=f"GET /channels/{channel_id}/messages (session:web)")
        return data

    def _get(self, path: str, *, route: str, params: dict[str, Any] | None = None) -> Any:
        response = self._client.get(f"{self.api_base}{path}", params=params, headers=self.auth.headers())
        if response.status_code < 400:
            return response.json()
        try:
            payload = response.json()
            detail = payload.get("message") or payload.get("error_description") or payload.get("error") or response.text
        except Exception:
            detail = response.text
        raise DiscordAPIError(response.status_code, redact_string(str(detail)), route=route)
