"""The bot's own receive-only inbox, via the mail.tm API.

The user cannot create or share a mailbox, so the bot owns one: an account on
mail.tm (free, API-only, ~40MB quota) created on first use and stored -
address plus encrypted password - in the local database. The user adds one
Outlook rule forwarding library emails to this address, and the bot polls it
after each booking to pluck out the check-in code and cancellation link.

Everything here degrades gracefully: if mail.tm is down or the account was
purged, calls return None/[] and the paste-the-email fallback still works.
"""

from __future__ import annotations

import logging
import random
import re
import string

import httpx

from .. import storage
from . import credstore

log = logging.getLogger(__name__)

API = "https://api.mail.tm"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API, timeout=20,
                             headers={"Accept": "application/json"})


def _members(payload) -> list:
    # mail.tm returns a bare list for application/json and a hydra-wrapped
    # object for application/ld+json; accept either.
    if isinstance(payload, list):
        return payload
    return payload.get("hydra:member", [])


async def ensure_inbox() -> str | None:
    """Returns the bot's inbox address, creating the account on first call."""
    saved = storage.durable_get("botmail_account")
    if saved and saved.get("address"):
        return saved["address"]
    try:
        async with _client() as client:
            resp = await client.get("/domains")
            resp.raise_for_status()
            domain = _members(resp.json())[0]["domain"]
            local = "ntulib" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
            address = f"{local}@{domain}"
            password = "".join(random.choices(string.ascii_letters + string.digits, k=24))
            resp = await client.post("/accounts", json={"address": address, "password": password})
            resp.raise_for_status()
        storage.durable_set("botmail_account", {
            "address": address,
            "password": credstore.encrypt(password).hex(),
        })
        log.info("Created bot inbox %s", address)
        return address
    except Exception as e:
        log.warning("Could not create bot inbox: %s", e)
        return None


async def _token(client: httpx.AsyncClient) -> str | None:
    saved = storage.durable_get("botmail_account")
    if not saved:
        return None
    try:
        password = credstore.decrypt(bytes.fromhex(saved["password"]))
        resp = await client.post("/token", json={"address": saved["address"], "password": password})
        resp.raise_for_status()
        return resp.json().get("token")
    except Exception as e:
        log.warning("Bot inbox login failed: %s", e)
        return None


async def fetch_messages(limit: int = 10) -> list[dict]:
    """Newest-first [{subject, sender, text}] from the bot inbox."""
    out: list[dict] = []
    try:
        async with _client() as client:
            token = await _token(client)
            if not token:
                return []
            auth = {"Authorization": f"Bearer {token}"}
            resp = await client.get("/messages", headers=auth)
            resp.raise_for_status()
            for meta in _members(resp.json())[:limit]:
                detail = await client.get(f"/messages/{meta['id']}", headers=auth)
                if detail.status_code != 200:
                    continue
                data = detail.json()
                html = data.get("html")
                text = data.get("text") or ""
                if not text and html:
                    joined = "".join(html) if isinstance(html, list) else str(html)
                    text = re.sub(r"<[^>]+>", " ", joined)
                out.append({
                    "subject": data.get("subject", ""),
                    "sender": (data.get("from") or {}).get("address", ""),
                    "text": text,
                })
    except Exception as e:
        log.warning("Bot inbox fetch failed: %s", e)
    return out
