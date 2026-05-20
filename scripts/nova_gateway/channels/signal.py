"""
nova_gateway.channels.signal — Signal listener via TCP JSON-RPC streaming.

Written by Jordan Koch.
"""

import asyncio
import json
import logging

from nova_gateway.config import (
    SIGNAL_URL, SIGNAL_TCP_HOST, SIGNAL_TCP_PORT,
    JORDAN_SIGNAL,
)
from nova_gateway.context import GatewayContext
from nova_gateway.agent import run_agent, session_id, gen_trace_id

log = logging.getLogger("nova_gateway_v2")


def _split_message(text: str, max_len: int = 1000) -> list[str]:
    """Split a long message at sentence boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Find last sentence end before max_len
        cut = text.rfind(". ", 0, max_len)
        if cut == -1:
            cut = text.rfind(" ", 0, max_len)
        if cut == -1:
            cut = max_len
        else:
            cut += 1  # include the period
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    return chunks


async def signal_rpc(ctx: GatewayContext, method: str, params: dict = None) -> dict:
    """Call signal-cli JSON-RPC API."""
    payload = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params:
        payload["params"] = params
    resp = await ctx.http.post(
        f"{SIGNAL_URL}/api/v1/rpc",
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


async def send_signal(ctx: GatewayContext, recipient: str, text: str):
    for chunk in _split_message(text, 1000):
        try:
            result = await signal_rpc(ctx, "send", {
                "recipient": recipient,
                "message":   chunk,
            })
            if "error" in result:
                log.error(f"Signal send error: {result['error']}")
        except Exception as e:
            log.error(f"Signal send failed: {e}")


async def run_signal(ctx: GatewayContext):
    """Signal listener via TCP JSON-RPC streaming.

    signal-cli daemon runs with --tcp 127.0.0.1:7583 for streaming receive
    and --http 127.0.0.1:8080 for outbound sends.

    TCP streaming: open connection -> subscribeReceive -> listen for pushed messages.
    Much more efficient than HTTP polling, no "already being received" conflict.
    """
    log.info("Signal adapter starting (TCP streaming mode)...")

    ALLOWED = {JORDAN_SIGNAL}
    last_timestamp: dict[str, int] = {}

    while not ctx.shutdown.is_set():
        reader = writer = None
        try:
            reader, writer = await asyncio.open_connection(SIGNAL_TCP_HOST, SIGNAL_TCP_PORT)
            log.info("Signal: TCP connection established")

            # Subscribe to receive messages
            sub_req = json.dumps({"jsonrpc": "2.0", "method": "subscribeReceive", "id": 1}) + "\n"
            writer.write(sub_req.encode())
            await writer.drain()

            # Read first response (subscription confirmation)
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            resp = json.loads(line)
            if "error" in resp:
                log.error(f"Signal subscribeReceive error: {resp['error']}")
                await asyncio.sleep(5)
                continue

            sub_id = resp.get("result", 0)
            log.info(f"Signal: subscribed (id={sub_id}) — listening for messages")

            # Stream incoming messages
            while not ctx.shutdown.is_set():
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=30)
                    if not line:
                        break

                    msg = json.loads(line)
                    # Incoming messages arrive as JSON-RPC notifications (no id)
                    params = msg.get("params", {})
                    envelope = params.get("envelope", {})
                    data_msg = envelope.get("dataMessage", {})
                    sender   = envelope.get("sourceNumber", "")
                    text     = data_msg.get("message", "").strip()
                    ts       = envelope.get("timestamp", 0)

                    if not text or not sender:
                        continue
                    if sender not in ALLOWED:
                        continue
                    if last_timestamp.get(sender, 0) >= ts:
                        continue
                    last_timestamp[sender] = ts

                    trace_id = gen_trace_id()
                    log.info(f"[{trace_id}] Signal: message from {sender}: {text[:50]}")
                    sid = session_id("signal", sender)
                    channel_key = f"signal:{sender}"

                    async def handle_signal(t=text, s=sid, sndr=sender, ck=channel_key, tid=trace_id):
                        async with ctx.channel_locks[ck]:
                            try:
                                response = await run_agent(ctx, t, s, "chat", trace_id=tid)
                                await send_signal(ctx, sndr, response)
                            except Exception as e:
                                log.error(f"Signal agent error: {e}", exc_info=True)
                                await send_signal(ctx, sndr, "Something went wrong on my end.")

                    asyncio.create_task(handle_signal())

                except asyncio.TimeoutError:
                    # Send keepalive ping
                    ping = json.dumps({"jsonrpc": "2.0", "method": "version", "id": 99}) + "\n"
                    writer.write(ping.encode())
                    await writer.drain()
                except json.JSONDecodeError:
                    continue

        except asyncio.CancelledError:
            break
        except ConnectionRefusedError:
            log.warning("Signal: TCP connection refused — signal-cli not ready, retrying in 10s")
            await asyncio.sleep(10)
        except Exception as e:
            log.error(f"Signal TCP error: {e}")
            await asyncio.sleep(5)
        finally:
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
