from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from libs.core.redaction import redact_string

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DEFAULT_SCOPES = ("identify", "guilds")


class DiscordAPIError(RuntimeError):
    """Safe Discord upstream error; message is redacted before storage/logging."""

    def __init__(self, status_code: int, message: str, *, route: str | None = None):
        self.status_code = status_code
        self.route = route
        super().__init__(redact_string(message))


@dataclass(frozen=True)
class DiscordOAuthConfig:
    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None
    bot_token: str | None = None

    @classmethod
    def from_env(cls) -> "DiscordOAuthConfig":
        return cls(
            client_id=_env("DISCORD_SYNC_CLIENT_ID"),
            client_secret=_env("DISCORD_SYNC_CLIENT_SECRET"),
            redirect_uri=_env("DISCORD_SYNC_REDIRECT_URI"),
            bot_token=_env("DISCORD_SYNC_BOT_TOKEN"),
        )

    def missing_oauth(self) -> list[str]:
        missing: list[str] = []
        if not self.client_id:
            missing.append("DISCORD_SYNC_CLIENT_ID")
        if not self.client_secret:
            missing.append("DISCORD_SYNC_CLIENT_SECRET")
        if not self.redirect_uri:
            missing.append("DISCORD_SYNC_REDIRECT_URI")
        return missing

    def require_oauth(self) -> None:
        missing = self.missing_oauth()
        if missing:
            raise RuntimeError("Missing Discord OAuth config: " + ", ".join(missing))

    def require_bot_token(self) -> str:
        if not self.bot_token:
            raise RuntimeError("Missing Discord bot token credential_ref/env: DISCORD_SYNC_BOT_TOKEN")
        return self.bot_token

    def authorization_url(self, *, state: str, scopes: list[str] | None = None) -> str:
        self.require_oauth()
        scope = " ".join(scopes or list(DEFAULT_SCOPES))
        query = urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "scope": scope,
            "state": state,
            "redirect_uri": self.redirect_uri,
            "prompt": "consent",
        })
        return f"{DISCORD_AUTHORIZE_URL}?{query}"


def _env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def token_expires_at(token_payload: dict[str, Any], *, now: datetime | None = None) -> str | None:
    expires_in = token_payload.get("expires_in")
    if expires_in is None:
        return None
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(seconds=int(expires_in))).isoformat()


class DiscordProvider:
    """Official Discord OAuth/API client.

    Uses OAuth Bearer tokens only for the consented user identity/guild list,
    and Bot tokens only for app-authorized guild channel/message reads.
    No raw user tokens, passwords, cookies, selfbot, or browser-session scraping.
    """

    def __init__(self, *, config: DiscordOAuthConfig | None = None, client: httpx.Client | None = None):
        self.config = config or DiscordOAuthConfig.from_env()
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def exchange_code(self, code: str) -> dict[str, Any]:
        self.config.require_oauth()
        response = self._client.post(
            f"{DISCORD_API_BASE}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.redirect_uri,
            },
            auth=(self.config.client_id or "", self.config.client_secret or ""),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._json_or_error(response, route="POST /oauth2/token")

    def get_current_user(self, access_token: str) -> dict[str, Any]:
        return self._get_bearer("/users/@me", access_token, route="GET /users/@me")

    def list_user_guilds(self, access_token: str) -> list[dict[str, Any]]:
        data = self._get_bearer("/users/@me/guilds", access_token, route="GET /users/@me/guilds")
        if not isinstance(data, list):
            raise DiscordAPIError(502, "Discord returned a non-list guild payload", route="GET /users/@me/guilds")
        return data

    def list_guild_channels(self, guild_id: str, bot_token: str | None = None) -> list[dict[str, Any]]:
        token = bot_token or self.config.require_bot_token()
        data = self._get_bot(f"/guilds/{guild_id}/channels", token, route=f"GET /guilds/{guild_id}/channels")
        if not isinstance(data, list):
            raise DiscordAPIError(502, "Discord returned a non-list channel payload", route=f"GET /guilds/{guild_id}/channels")
        return data

    def list_channel_messages(
        self,
        channel_id: str,
        *,
        bot_token: str | None = None,
        limit: int = 50,
        before: str | None = None,
        after: str | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        token = bot_token or self.config.require_bot_token()
        params: dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        data = self._get_bot(
            f"/channels/{channel_id}/messages",
            token,
            params=params,
            route=f"GET /channels/{channel_id}/messages",
        )
        if not isinstance(data, list):
            raise DiscordAPIError(502, "Discord returned a non-list message payload", route=f"GET /channels/{channel_id}/messages")
        return data

    def _get_bearer(self, path: str, access_token: str, *, route: str) -> Any:
        response = self._client.get(f"{DISCORD_API_BASE}{path}", headers={"Authorization": f"Bearer {access_token}"})
        return self._json_or_error(response, route=route)

    def _get_bot(self, path: str, bot_token: str, *, route: str, params: dict[str, Any] | None = None) -> Any:
        response = self._client.get(f"{DISCORD_API_BASE}{path}", params=params, headers={"Authorization": f"Bot {bot_token}"})
        return self._json_or_error(response, route=route)

    def _json_or_error(self, response: httpx.Response, *, route: str) -> Any:
        if response.status_code < 400:
            return response.json()
        try:
            payload = response.json()
            detail = payload.get("message") or payload.get("error_description") or payload.get("error") or response.text
        except Exception:
            detail = response.text
        raise DiscordAPIError(response.status_code, detail, route=route)
