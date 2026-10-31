"""Fence a shared Discord token at gateway admission and outgoing HTTP effects.

discord.py parses commands and persistent buttons before dispatch('interaction'),
so on_interaction is too late. Wrap the parser entrypoints instead. No timer or
gateway heartbeat touches PostgreSQL; only actual user events/outgoing effects do.
"""
from __future__ import annotations

import asyncio
import logging

from discord.webhook.async_ import AsyncWebhookAdapter, async_context

from shared_state import SharedStateError

log = logging.getLogger("scrimbot.shared")


class OwnedWebhookAdapter(AsyncWebhookAdapter):
    def __init__(self, backend):
        super().__init__()
        self.backend = backend

    async def request(self, route, session, **kwargs):
        if route.method != "GET":
            await asyncio.to_thread(self.backend.check_owner)
        return await super().request(route, session, **kwargs)


def install_discord_guard(bot, backend):
    """Install before cogs/gateway tasks are started so they inherit the adapter."""
    original_request = bot.http.request

    async def owned_request(route, **kwargs):
        if route.method != "GET":
            await asyncio.to_thread(backend.check_owner)
        return await original_request(route, **kwargs)

    bot.http.request = owned_request
    # Interaction responses and followups use a separate HTTP adapter.
    async_context.set(OwnedWebhookAdapter(backend))
    bot._shared_event_tasks = set()
    admission = asyncio.Lock()

    def guarded_parser(event, original):
        async def admit(data):
            async with admission:
                try:
                    if event == "MESSAGE_UPDATE":
                        revision = data.get("edited_timestamp")
                        if revision is None:
                            await asyncio.to_thread(backend.check_owner)
                            original(data)  # embed-only update; no new user edit
                            return
                        key = f"{event}:{data['id']}:{revision}"
                    else:
                        key = f"{event}:{data['id']}"
                    if await asyncio.to_thread(backend.claim_event, key):
                        original(data)
                except SharedStateError:
                    # The backend schedules shutdown on ownership/state loss. Do not
                    # emit an error response from an instance that no longer owns it.
                    log.warning("Discord event rejected: shared ownership unavailable")

        def parse(data):
            if event in ("MESSAGE_CREATE", "MESSAGE_UPDATE") and data.get("author", {}).get("bot"):
                original(data)  # cache bot messages; cogs already ignore them
                return
            task = asyncio.create_task(admit(data))
            bot._shared_event_tasks.add(task)
            task.add_done_callback(bot._shared_event_tasks.discard)
        return parse

    for event in ("INTERACTION_CREATE", "MESSAGE_CREATE", "MESSAGE_UPDATE", "MESSAGE_DELETE"):
        bot._connection.parsers[event] = guarded_parser(event, bot._connection.parsers[event])
