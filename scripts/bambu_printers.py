# bambu_printers.py — Bambu Lab X1C registry (LAN/MQTT).
# Access codes are NOT here — they live in macOS Keychain as nova-bambu-<serial> (account: kochj).
# Discovered via SSDP on 2026-06-23. Both speak local MQTT/TLS on 8883, FTPS on 990, camera on 6000.

# WARNING (2026-08-28): P1's IP 192.168.1.40 is STALE — DHCP reassigned it to a UniFi
# camera while P1 was powered off, so the daemon can no longer reach P1 there (it will
# correctly report OFFLINE). Neither printer has a DHCP reservation, so their addresses
# float. When P1 is next powered on, re-confirm its IP (SSDP by serial, or the UniFi
# client list) and set DHCP reservations for BOTH so this can't recur. The serial is the
# stable identity (MQTT topics + Keychain), not the IP.
PRINTERS = {
    "P1": {"name": "Printer 1", "ip": "192.168.1.40",  "serial": "00M09A362800690"},
    "P2": {"name": "Printer 2", "ip": "192.168.1.166", "serial": "00M09C422901426"},
}
