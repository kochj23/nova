"""
nova_gateway.context — GatewayContext dataclass holding all shared state.

Replaces module-level globals. Passed as `ctx` to all functions.

Written by Jordan Koch.
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class GatewayContext:
    """Shared state for the entire gateway process."""

    pg_pool: Any = None  # asyncpg.Pool
    http: Any = None  # httpx.AsyncClient
    router: Any = None  # ModelRouter
    tokens: dict = field(default_factory=dict)
    shutdown: asyncio.Event = field(default_factory=asyncio.Event)
    sessions: dict = field(default_factory=lambda: defaultdict(list))  # {session_id: [messages]}
    channel_locks: dict = field(default_factory=lambda: defaultdict(asyncio.Lock))  # {channel: asyncio.Lock}
    typing_tasks: dict = field(default_factory=dict)  # {channel_key: asyncio.Task}
    agent_crash_counts: dict = field(default_factory=lambda: defaultdict(int))
    agent_last_crash: dict = field(default_factory=dict)
    agent_disabled_until: dict = field(default_factory=dict)
    claude_active_task: Optional[str] = None
    claude_editing_files: list = field(default_factory=list)
    redis_conn: Any = None
    start_time: float = field(default_factory=time.time)
    last_reload: float = 0.0
    startup_time: float = field(default_factory=time.time)
