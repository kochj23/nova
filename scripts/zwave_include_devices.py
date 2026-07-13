#!/usr/bin/env python3
"""Z-Wave bulk inclusion — loops inclusion mode continuously. Ctrl+C to stop."""

import asyncio
import json
import sys

import websockets

ZWUI_WS = "ws://localhost:8091/socket.io/?EIO=4&transport=websocket"
TIMEOUT_SECS = 90
MAX_DEVICES = 20

_ack_id = 0
_pending_acks = {}


def next_ack_id():
    global _ack_id
    _ack_id += 1
    return _ack_id


async def emit_with_ack(ws, event, data, timeout=10):
    """Send Socket.IO event with acknowledgement (the 42<N> format)."""
    ack_id = next_ack_id()
    frame = f"42{ack_id}" + json.dumps([event, data])
    fut = asyncio.get_event_loop().create_future()
    _pending_acks[ack_id] = fut
    await ws.send(frame)
    try:
        async with asyncio.timeout(timeout):
            return await fut
    except (TimeoutError, asyncio.TimeoutError):
        _pending_acks.pop(ack_id, None)
        return None


async def recv_loop(ws, event_queue):
    """Background task that routes incoming messages."""
    while True:
        try:
            msg = await ws.recv()
        except websockets.exceptions.ConnectionClosed:
            break
        if msg == "2":
            await ws.send("3")  # pong
            continue
        if msg.startswith("43"):
            # Ack response: 43<id>[payload]
            rest = msg[2:]
            # Parse the ack id (digits at start)
            i = 0
            while i < len(rest) and rest[i].isdigit():
                i += 1
            if i > 0:
                ack_id = int(rest[:i])
                payload = json.loads(rest[i:]) if rest[i:] else None
                fut = _pending_acks.pop(ack_id, None)
                if fut and not fut.done():
                    fut.set_result(payload)
        elif msg.startswith("42"):
            data = json.loads(msg[2:])
            if isinstance(data, list) and len(data) >= 1:
                await event_queue.put(data)


async def main():
    print("\n=== Z-Wave Bulk Inclusion ===", flush=True)
    print("Inclusion mode will stay active. Press device buttons now.", flush=True)
    print(f"Timeout per device: {TIMEOUT_SECS}s. Stops after timeout with no response.\n", flush=True)

    async with websockets.connect(ZWUI_WS, max_size=10 * 1024 * 1024) as ws:
        await ws.recv()  # engine.io open
        await ws.send("40")  # socket.io connect
        await ws.recv()  # connect ack

        event_queue = asyncio.Queue()
        recv_task = asyncio.create_task(recv_loop(ws, event_queue))

        # Subscribe to node events
        await emit_with_ack(ws, "SUBSCRIBE", {"channels": ["nodes", "controller"]})
        await asyncio.sleep(0.5)

        # Drain any initial events
        while not event_queue.empty():
            event_queue.get_nowait()

        device_num = 0
        while device_num < MAX_DEVICES:
            device_num += 1
            print(f"{'─' * 50}", flush=True)
            print(f"  Device #{device_num}: Starting inclusion...", flush=True)

            # Start inclusion: strategy 3 = Insecure, options = {name, location}
            # InclusionStrategy: Default=0, SmartStart=1, Insecure=2, Security_S0=3, Security_S2=4
            result = await emit_with_ack(ws, "ZWAVE_API", {
                "api": "startInclusion",
                "args": [2, {"name": "", "location": ""}],
            }, timeout=10)

            if result is not None:
                if isinstance(result, list) and len(result) > 0:
                    result = result[0]
                if isinstance(result, dict) and result.get("success") is False:
                    print(f"  ✗ Error: {result.get('message', 'unknown')}", flush=True)
                    break
                print(f"  ✓ Inclusion mode ACTIVE", flush=True)
            else:
                print(f"  ⚠ No ack (may still be active)", flush=True)

            print(f"  >>> PRESS THE BUTTON ON YOUR DEVICE <<<", flush=True)

            # Wait for NODE_ADDED event
            node_added = None
            try:
                async with asyncio.timeout(TIMEOUT_SECS):
                    while True:
                        data = await event_queue.get()
                        event = data[0]
                        payload = data[1] if len(data) > 1 else {}

                        if event == "NODE_FOUND":
                            print(f"  ... device found! Interviewing...", flush=True)
                        elif event == "NODE_ADDED":
                            node_added = payload
                            break
                        elif event == "GRANT_SECURITY_CLASSES":
                            await emit_with_ack(ws, "ZWAVE_API", {
                                "api": "grantSecurityClasses",
                                "args": [payload.get("requested", {})],
                            })
                        elif event == "VALIDATE_DSK":
                            await emit_with_ack(ws, "ZWAVE_API", {
                                "api": "validateDSK",
                                "args": [payload.get("dsk", "")],
                            })
                        elif event == "INCLUSION_ABORTED":
                            print(f"  ✗ Inclusion aborted", flush=True)
                            break
                        elif event == "CONTROLLER_CMD":
                            status = payload if isinstance(payload, str) else payload.get("status", "")
                            if "started" in str(status).lower():
                                print(f"  ✓ Controller confirmed: inclusion active", flush=True)
            except (TimeoutError, asyncio.TimeoutError):
                print(f"\n  No device responded in {TIMEOUT_SECS}s. Stopping.", flush=True)
                await emit_with_ack(ws, "ZWAVE_API", {"api": "stopInclusion", "args": []}, timeout=5)
                break

            if node_added:
                nid = node_added.get("id", node_added.get("nodeId", "?"))
                prod = node_added.get("productDescription", "")
                mfr = node_added.get("manufacturer", "")
                print(f"\n  ✓ INCLUDED! Node {nid}: {mfr} {prod}", flush=True)
                await asyncio.sleep(3)
                print(f"  Looping for next device...\n", flush=True)
            else:
                break

        # Stop inclusion and clean up
        await emit_with_ack(ws, "ZWAVE_API", {"api": "stopInclusion", "args": []}, timeout=5)
        recv_task.cancel()

        print("\nDone.", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    except Exception as e:
        print(f"\nError: {e}", flush=True)
        sys.exit(1)
