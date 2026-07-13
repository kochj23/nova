# bambu_printers.py — Bambu Lab X1C registry (LAN/MQTT).
# Access codes are NOT here — they live in macOS Keychain as nova-bambu-<serial> (account: kochj).
# Discovered via SSDP on 2026-06-23. Both speak local MQTT/TLS on 8883, FTPS on 990, camera on 6000.

PRINTERS = {
    "P1": {"name": "Printer 1", "ip": "192.168.1.40",  "serial": "00M09A362800690"},
    "P2": {"name": "Printer 2", "ip": "192.168.1.166", "serial": "00M09C422901426"},
}
