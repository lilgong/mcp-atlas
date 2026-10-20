"""Bounded author conversations: one live tool environment, at most one repair.

Local control-plane only. Sessions are runtime state, never benchmark answers.
Ordinary evaluation/rollout requests never enter this registry.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from .runtime_log import write_runtime_event


@dataclass
class AuthorSession:
    identity: tuple
    client: Any
    initial_messages: list
    control: Any
    state: dict = field(default_factory=dict)
    outputs: list = field(default_factory=list)
    busy: bool = True
    repairs: int = 0
    touched: float = field(default_factory=time.monotonic)
    closing: Any = None


class AuthorSessions:
    def __init__(self, idle_seconds=1800):
        self.entries: dict[str, AuthorSession] = {}
        self.idle_seconds = idle_seconds

    @staticmethod
    def identity(body):
        # No changing model/tools/thinking during resume. The original controller
        # is retained by the server; new feedback and a repair budget are separate.
        return (body.task_id, body.model, tuple(sorted(body.enabled_tools)), body.extra_body)

    async def begin(self, body, factory):
        key = body.author_session_id
        identity = self.identity(body)
        if body.author_session_action == "start":
            if key in self.entries:
                raise HTTPException(409, "author session already exists")
            entry = AuthorSession(identity, factory(), list(body.messages), body.synthesis_control)
            self.entries[key] = entry  # Reserve before the first await.
            try:
                await entry.client.__aenter__()
            except BaseException:
                await self.discard(key)
                raise
        else:
            entry = self.entries.get(key)
            if entry is None:
                raise HTTPException(410, "author session expired; fresh exploration required")
            if not entry.busy and time.monotonic() - entry.touched > self.idle_seconds:
                await self.discard(key)
                raise HTTPException(410, "author session expired; fresh exploration required")
            if entry.busy or entry.closing or entry.repairs >= 1 or entry.identity != identity:
                raise HTTPException(409, "author session busy, repaired, or configuration changed")
            if any(m.role != "user" for m in body.messages) or not body.messages:
                raise HTTPException(422, "resume accepts user feedback only")
            entry.busy = True
            entry.repairs += 1
            entry.state["conversation"].extend(body.messages)
            entry.outputs.extend({"type": "message", "data": m.model_dump()} for m in body.messages)
        entry.touched = time.monotonic()
        write_runtime_event("service", "author_session_" + body.author_session_action,
                            task_id=body.task_id, session_id=key)
        return entry

    def idle(self, entry):
        entry.busy = False
        entry.touched = time.monotonic()

    async def discard(self, key):
        entry = self.entries.get(key)
        if entry is not None:
            # The same cancellation-safe cleanup as ordinary isolated clients.
            if entry.closing is None:
                entry.closing = asyncio.create_task(entry.client.__aexit__(None, None, None))
            try:
                await asyncio.shield(entry.closing)
            except asyncio.CancelledError:
                await entry.closing
                raise
            finally:
                if self.entries.get(key) is entry:
                    self.entries.pop(key)
                    write_runtime_event("service", "author_session_closed",
                                        task_id=entry.identity[0], session_id=key)

    async def close(self, key):
        entry = self.entries.get(key)
        if entry is not None and entry.busy:
            raise HTTPException(409, "author session is still executing")
        await self.discard(key)

    async def reap(self):
        now = time.monotonic()
        for key, entry in list(self.entries.items()):
            if not entry.busy and now - entry.touched > self.idle_seconds:
                await self.discard(key)

    async def sweep(self):
        while True:
            await asyncio.sleep(60)
            try:
                await self.reap()
            except Exception as exc:
                write_runtime_event("service", "author_session_reap_failed", error=type(exc).__name__)

    async def close_all(self):
        await asyncio.gather(*(self.discard(key) for key in list(self.entries)), return_exceptions=True)


AUTHOR_SESSIONS = AuthorSessions()
