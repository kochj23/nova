"""
nova_gateway — Nova's custom Python gateway package.

Replaces OpenClaw node.js binary. Channels: Slack, Discord, Signal, Claude Code.
Multi-backend LLM routing with automatic failover.

Written by Jordan Koch.
"""

__version__ = "2.4.0"

from nova_gateway.main import main

__all__ = ["main"]
