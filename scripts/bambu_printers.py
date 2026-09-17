# bambu_printers.py — Bambu Lab X1C registry (LAN/MQTT).
# Access codes are NOT here — they live in macOS Keychain as nova-bambu-<serial> (account: kochj).
# Discovered via SSDP on 2026-06-23. Both speak local MQTT/TLS on 8883, FTPS on 990, camera on 6000.

# IPs corrected 2026-09-16: the old .40/.166 had drifted (DHCP reassigned .40 to a patio
# camera while the printers were off). Re-identified by MQTT serial match against the UniFi
# client list — P1=00M09A…→.179, P2=00M09C…→.119 — and both verified CONNECTED/IDLE. The
# addresses still FLOAT (no DHCP reservation), so this can recur; set reservations in UniFi
# for both to make it permanent. The serial is the stable identity (MQTT topics + Keychain),
# not the IP — identify by serial if they drift again.
PRINTERS = {
    "P1": {"name": "Printer 1", "ip": "192.168.1.179",  "serial": "00M09A362800690"},
    "P2": {"name": "Printer 2", "ip": "192.168.1.119", "serial": "00M09C422901426"},
}
