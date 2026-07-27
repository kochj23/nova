# Network Device Integration Guide

> Authoritative reference for all controllable devices on the Koch network.
> Last updated: 2026-06-09

---

## Quick-Reference Table

| Device | IP | Protocol | Port | Auth | One-Liner |
|--------|-----|----------|------|------|-----------|
| Bose Soundbar 900 (Living) | 192.168.1.25 | UPnP/SOAP | 8091 | None | `curl -X POST http://192.168.1.25:8091/RenderingControl -d @vol.xml` |
| Bose Soundbar 900 (Bedroom) | 192.168.1.82 | UPnP/SOAP | 8091 | None | `curl -X POST http://192.168.1.82:8091/RenderingControl -d @vol.xml` |
| Bose Soundbar 900 (Office) | 192.168.1.197 | UPnP/SOAP | 8091 | None | `curl -X POST http://192.168.1.197:8091/RenderingControl -d @vol.xml` |
| Onkyo TX-NR696 | 192.168.1.98 | eISCP | 60128 | None | `echo "ISCP...!1PWR01" \| nc 192.168.1.98 60128` |
| Onkyo TX-NR5100 | 192.168.1.145 | eISCP | 60128 | None | `echo "ISCP...!1PWR01" \| nc 192.168.1.145 60128` |
| Ambient Weather Pro | 192.168.1.33 | HTTP POST (push) | 80 | None | Read-only; station pushes every 16s |
| Koogeek KH01 (Switch A) | 192.168.1.45 | HAP/HomeKit | 80 | Paired | `curl http://localhost:37432/switch/192.168.1.45/on` |
| Koogeek KH01 (Switch B) | 192.168.1.48 | HAP/HomeKit | 80 | Paired | `curl http://localhost:37432/switch/192.168.1.48/on` |
| Koogeek KH02 (2-Gang) | 192.168.1.79 | HAP/HomeKit | 80 | Paired | `curl http://localhost:37432/switch/192.168.1.79/on` |
| UniFi Controller | 192.168.1.1 | REST/HTTPS | 443 | Session cookie | `curl -k -X POST https://192.168.1.1/api/auth/login -d '...'` |
| Eve Energy Strips | Thread/BLE | HAP/HomeKit | -- | Paired | `curl http://localhost:37432/outlet/<id>/off` |

---

## 1. Bose Smart Soundbar 900 (x3)

### Hardware

| Property | Value |
|----------|-------|
| Make/Model | Bose Smart Soundbar 900 |
| Firmware | 15.0.40 |
| IP Addresses | 192.168.1.25, 192.168.1.82, 192.168.1.197 |
| Protocol | UPnP/DLNA (SOAP over HTTP) |
| Port | 8091 |
| Authentication | None |

### Available Commands/Actions

| Action | SOAP Service | Method |
|--------|-------------|--------|
| Set Volume | RenderingControl | SetVolume |
| Get Volume | RenderingControl | GetVolume |
| Mute/Unmute | RenderingControl | SetMute |
| Play | AVTransport | Play |
| Pause | AVTransport | Pause |
| Stop | AVTransport | Stop |
| Set Media URI | AVTransport | SetAVTransportURI |
| Get Position | AVTransport | GetPositionInfo |
| Get Transport State | AVTransport | GetTransportInfo |

### Python Code Examples

```python
# Set volume to 30 on Living Room soundbar
import requests
body = '''<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:SetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1"><InstanceID>0</InstanceID><Channel>Master</Channel><DesiredVolume>30</DesiredVolume></u:SetVolume></s:Body></s:Envelope>'''
requests.post("http://192.168.1.25:8091/RenderingControl", data=body, headers={"Content-Type": "text/xml", "SOAPAction": '"urn:schemas-upnp-org:service:RenderingControl:1#SetVolume"'})
```

```python
# Play a media URI
body = '''<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID><CurrentURI>http://stream.example.com/audio.mp3</CurrentURI><CurrentURIMetaData></CurrentURIMetaData></u:SetAVTransportURI></s:Body></s:Envelope>'''
requests.post("http://192.168.1.25:8091/AVTransport", data=body, headers={"Content-Type": "text/xml", "SOAPAction": '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"'})
```

```python
# Subscribe to volume/mute change events (SUBSCRIBE callback)
requests.request("SUBSCRIBE", "http://192.168.1.25:8091/RenderingControl/Event", headers={"CALLBACK": "<http://192.168.1.100:9090/bose/events>", "NT": "upnp:event", "TIMEOUT": "Second-300"})
```

### Event Subscription

Subscribe to `/RenderingControl/Event` and `/AVTransport/Event` for push notifications on state changes. The soundbar sends HTTP NOTIFY to your callback URL with XML event data.

### Known Quirks/Limitations

- TLS ports 8082-8085 require Bose cloud mTLS certificates; not usable for local control
- Volume range is 0-100 but the bar clips audio above ~85
- SOAP responses use HTTP 200 even on logical errors; check the XML body
- The bar may take 2-3 seconds to respond if waking from network standby
- Group/multi-room sync requires the Bose SoundTouch protocol (proprietary, ports 8082+)
- No local discovery of current source/preset without polling GetTransportInfo

### Integration Status: **Implemented**

---

## 2. Onkyo TX-NR696 + TX-NR5100

### Hardware

| Property | TX-NR696 | TX-NR5100 |
|----------|----------|-----------|
| IP Address | 192.168.1.98 | 192.168.1.145 |
| Protocol | eISCP (Integra Serial over IP) | eISCP |
| Port | 60128 | 60128 |
| Authentication | None | None |
| Zones | Main + Zone 2 | Main only |
| Additional | Chromecast (8008/8009), AirPlay 2 (7000), Web UI (80) | Chromecast (8008/8009), AirPlay 2 (7000), Web UI (80) |
| Web UI Creds | admin / admin | admin / admin |

### eISCP Protocol

Messages are wrapped in the ISCP framing:
```
Header: "ISCP" (4 bytes)
Header size: 16 (4 bytes, big-endian)
Data size: N (4 bytes, big-endian)
Version: 0x01
Reserved: 0x00 0x00 0x00
Data: "!1<CMD><PARAM>\r"
```

### Available Commands/Actions

| Action | Command | Values |
|--------|---------|--------|
| Power On | PWR | 01 |
| Power Off (Standby) | PWR | 00 |
| Power Query | PWR | QSTN |
| Master Volume Set | MVL | 00-64 (hex, 0-100) |
| Master Volume Up | MVL | UP |
| Master Volume Down | MVL | DOWN |
| Mute Toggle | AMT | TG |
| Mute On | AMT | 01 |
| Mute Off | AMT | 00 |
| Input Select | SLI | see table below |
| Listening Mode | LMD | see table below |
| Zone 2 Power (696 only) | ZPW | 01/00 |
| Zone 2 Volume (696 only) | ZVL | 00-64 (hex) |
| Zone 2 Input (696 only) | SLZ | same as SLI |

### Input Codes (SLI)

| Code | Input |
|------|-------|
| 00 | VCR/DVR |
| 01 | CBL/SAT |
| 02 | GAME |
| 03 | AUX |
| 10 | BD/DVD |
| 22 | PHONO |
| 23 | CD |
| 24 | FM |
| 25 | AM |
| 29 | USB |
| 2B | Network |
| 2E | Bluetooth |
| 55 | HDMI 5 |
| 56 | HDMI 6 |

### Listening Mode Codes (LMD)

| Code | Mode |
|------|------|
| 00 | Stereo |
| 01 | Direct |
| 02 | Surround |
| 03 | Film |
| 04 | THX |
| 08 | Orchestra |
| 09 | Unplugged |
| 0C | All Ch Stereo |
| 80 | Pure Audio |

### Python Code Examples

```python
# Power on the TX-NR696 using the onkyo-eiscp library
import eiscp
with eiscp.eISCP("192.168.1.98") as receiver:
    receiver.command("system-power=on")
```

```python
# Set volume to 45 on TX-NR5100 (raw socket)
import socket, struct
cmd = "!1MVL2D\r"  # 0x2D = 45 decimal
data = b"ISCP" + struct.pack(">IIBBBB", 16, len(cmd), 1, 0, 0, 0) + cmd.encode()
s = socket.create_connection(("192.168.1.145", 60128)); s.sendall(data); s.close()
```

```python
# Switch TX-NR696 to GAME input and set Surround mode
import eiscp
with eiscp.eISCP("192.168.1.98") as r:
    r.command("input-selector=game"); r.command("listening-mode=surround")
```

### Known Quirks/Limitations

- Connection is persistent TCP; the receiver pushes state changes back
- Only one TCP connection allowed at a time (second connection drops the first)
- The TX-NR696 can take up to 10s to respond after power-on (capacitor warm-up)
- Zone 2 is only available on the TX-NR696
- Volume is hex-encoded (MVL2D = volume 45, MVL64 = volume 100)
- The web UI on port 80 is fragile; prefer eISCP for automation
- Chromecast integration (ports 8008/8009) is Google-managed; can cast audio but no direct volume control through Cast
- AirPlay 2 (port 7000) works but cannot be controlled programmatically without Apple frameworks

### Integration Status: **Implemented**

---

## 3. Ambient Weather Pro Station

### Hardware

| Property | Value |
|----------|-------|
| Make/Model | Ambient Weather WS-5000 (Pro Station) |
| IP Address | 192.168.1.33 |
| Protocol | Ecowitt push protocol (HTTP POST) |
| Port | 80 (outbound push to configured server) |
| Authentication | None |
| Push Interval | Every 16 seconds |
| Direction | Read-only (station pushes data to receiver) |

### Data Fields Received

| Field | Key | Unit |
|-------|-----|------|
| Outdoor Temperature | `tempf` | Fahrenheit |
| Outdoor Humidity | `humidity` | % |
| Indoor Temperature | `tempinf` | Fahrenheit |
| Indoor Humidity | `humidityin` | % |
| Barometric Pressure (relative) | `baromrelin` | inHg |
| Barometric Pressure (absolute) | `baromabsin` | inHg |
| Wind Speed | `windspeedmph` | mph |
| Wind Gust | `windgustmph` | mph |
| Wind Direction | `winddir` | degrees |
| Rain Rate | `rainratein` | in/hr |
| Daily Rain | `dailyrainin` | in |
| UV Index | `uv` | index |
| Solar Radiation | `solarradiation` | W/m2 |
| PM2.5 | `pm25_ch1` | ug/m3 |
| PM2.5 (24h avg) | `pm25_avg_24h_ch1` | ug/m3 |

### Configuration

The station's push target is configured via HTTP POST:

```
POST http://192.168.1.33/set_ws_settings
Content-Type: application/x-www-form-urlencoded

server_ip=192.168.1.100&server_port=8080&protocol=ecowitt&interval=16
```

### Python Code Examples

```python
# Receive weather data (Flask endpoint)
from flask import Flask, request
app = Flask(__name__)
@app.route("/weather", methods=["POST"])
def weather():
    data = request.form; print(f"Temp: {data['tempf']}F, Humidity: {data['humidity']}%"); return "OK"
```

```python
# Parse the latest push and extract key metrics
def parse_ecowitt(form_data: dict) -> dict:
    return {"temp_f": float(form_data.get("tempf", 0)), "humidity": int(form_data.get("humidity", 0)), "wind_mph": float(form_data.get("windspeedmph", 0)), "pm25": float(form_data.get("pm25_ch1", 0))}
```

```python
# Reconfigure push target to point at Nova
import requests
requests.post("http://192.168.1.33/set_ws_settings", data={"server_ip": "192.168.1.100", "server_port": "8080", "protocol": "ecowitt", "interval": "16"})
```

### Known Quirks/Limitations

- Completely read-only; no way to trigger an on-demand reading
- Push interval is configurable (16-300s) but shorter intervals increase WiFi traffic
- The station drops WiFi during firmware updates and resumes automatically
- PM2.5 sensor readings lag ~2 minutes behind actual conditions
- Solar radiation sensor saturates at ~1200 W/m2
- If the push target is unreachable, data is lost (no local buffer)
- Configuration endpoint requires the station to be on the same subnet

### Integration Status: **Implemented**

---

## 4. Koogeek Smart Switches (x3)

### Hardware

| Property | Value |
|----------|-------|
| Models | KH01 (1-gang, x2), KH02 (2-gang, x1) |
| IP Addresses | 192.168.1.45 (KH01), 192.168.1.48 (KH01), 192.168.1.79 (KH02) |
| Protocol | HAP (HomeKit Accessory Protocol) |
| Port | 80 |
| Authentication | HomeKit pairing (already paired to Apple Home) |
| Control Methods | Shortcuts CLI proxy (port 37432), aiohomekit |

### Available Commands/Actions

| Action | Target |
|--------|--------|
| Turn On | Relay (per gang) |
| Turn Off | Relay (per gang) |
| Toggle | Relay (per gang) |
| Get State | on/off boolean |
| Get Power (KH02) | Watts (if supported by firmware) |

### Python Code Examples

```python
# Toggle switch via Shortcuts CLI proxy
import requests
requests.post("http://localhost:37432/switch/192.168.1.45/toggle")
```

```python
# Control KH02 2-gang (each gang independently)
import requests
requests.post("http://localhost:37432/switch/192.168.1.79/gang/1/on")
requests.post("http://localhost:37432/switch/192.168.1.79/gang/2/off")
```

```python
# Using aiohomekit directly (requires stored pairing data)
import asyncio, aiohomekit
async def toggle():
    ctrl = aiohomekit.Controller(); pairing = ctrl.load_pairing("koogeek_45")
    chars = await pairing.get_characteristics([(1, 10)]); await pairing.put_characteristics([(1, 10, not chars[(1,10)]["value"])])
asyncio.run(toggle())
```

### Known Quirks/Limitations

- HAP requires existing pairing; cannot pair programmatically without physical button press
- The Shortcuts CLI proxy (port 37432) is the preferred integration path
- KH02 2-gang exposes each gang as a separate HomeKit service
- Firmware updates can only be pushed via the Koogeek app (no OTA endpoint)
- Switches occasionally drop off WiFi; they reconnect within 30-60s
- Power monitoring accuracy is +/- 5W on KH02
- No dimming capability; these are relay-only switches

### Integration Status: **Implemented** (via Shortcuts proxy)

---

## 5. UniFi Network Controller

### Hardware

| Property | Value |
|----------|-------|
| Make/Model | Ubiquiti UniFi Dream Machine Pro |
| IP Address | 192.168.1.1 |
| Protocol | REST API over HTTPS |
| Port | 443 |
| Authentication | Session cookie from /api/auth/login |
| API Base | https://192.168.1.1/proxy/network/api/ |

### Authentication Flow

```
POST https://192.168.1.1/api/auth/login
Content-Type: application/json

{"username": "admin", "password": "<from-keychain>"}

Response: Set-Cookie: TOKEN=...; unifises=...
```

### Available Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/proxy/network/api/s/default/stat/sta` | GET | All connected clients |
| `/proxy/network/api/s/default/stat/device` | GET | All network devices (APs, switches) |
| `/proxy/network/api/s/default/stat/health` | GET | Network health summary |
| `/proxy/network/api/s/default/rest/user` | GET | User/client list |
| `/proxy/network/api/s/default/cmd/stamgr` | POST | Client management (block/unblock/reconnect) |
| `/proxy/network/api/s/default/cmd/devmgr` | POST | Device management (restart, adopt) |
| `/proxy/network/api/s/default/stat/report/hourly.site` | GET | Hourly bandwidth stats |

### Python Code Examples

```python
# Authenticate and get all connected clients
import requests
s = requests.Session(); s.verify = False
s.post("https://192.168.1.1/api/auth/login", json={"username": "admin", "password": pw})
clients = s.get("https://192.168.1.1/proxy/network/api/s/default/stat/sta").json()["data"]
```

```python
# Get bandwidth usage for a specific client by MAC
clients = s.get("https://192.168.1.1/proxy/network/api/s/default/stat/sta").json()["data"]
target = next((c for c in clients if c["mac"] == CLIENT_MAC), None)
print(f"TX: {target['tx_bytes']/(1024**3):.1f} GB, RX: {target['rx_bytes']/(1024**3):.1f} GB") if target else None
```

```python
# Block a client device
s.post("https://192.168.1.1/proxy/network/api/s/default/cmd/stamgr", json={"cmd": "block-sta", "mac": CLIENT_MAC})
```

### Known Quirks/Limitations

- Self-signed TLS certificate; must use `verify=False` or add to trust store
- Session cookies expire after ~30 minutes of inactivity
- The `/proxy/network/` prefix is specific to UniFi OS (Dream Machine); standalone controllers use `/api/` directly
- Rate limiting applies after ~100 requests/minute
- Client stats reset on device reconnection
- Some endpoints return stale data for up to 30s after a topology change
- 2FA must be disabled on the API user account, or use a local-only account

### Integration Status: **Implemented**

---

## 6. Eve Energy Strips

### Hardware

| Property | Value |
|----------|-------|
| Make/Model | Eve Energy Strip (3rd gen) |
| Connectivity | HomeKit over Thread (with BLE fallback) |
| IP Address | N/A (Thread mesh, no direct IP) |
| Protocol | HAP (HomeKit Accessory Protocol) |
| Authentication | HomeKit pairing (Apple Home) |
| Control Methods | Shortcuts CLI proxy (port 37432), aiohomekit |

### Available Commands/Actions

| Action | Description |
|--------|-------------|
| Relay On/Off | Per-outlet control (3 outlets per strip) |
| Get Power (W) | Real-time wattage per outlet |
| Get Energy (kWh) | Cumulative energy consumption |
| Get Voltage (V) | Line voltage |
| Get Current (A) | Current draw per outlet |
| Reset Energy | Reset the kWh counter |

### Python Code Examples

```python
# Turn off outlet 2 on an Eve Energy Strip via Shortcuts proxy
import requests
requests.post("http://localhost:37432/outlet/eve_strip_1/2/off")
```

```python
# Read power consumption for all outlets
import requests
data = requests.get("http://localhost:37432/outlet/eve_strip_1/power").json()
for outlet in data["outlets"]:
    print(f"Outlet {outlet['id']}: {outlet['watts']}W, {outlet['kwh']} kWh total")
```

```python
# Monitor and alert if any outlet exceeds threshold
import requests
data = requests.get("http://localhost:37432/outlet/eve_strip_1/power").json()
alerts = [o for o in data["outlets"] if o["watts"] > 500]
if alerts: print(f"HIGH POWER: {alerts}")
```

### Known Quirks/Limitations

- Thread devices have no direct IP; must go through a Thread Border Router (HomePod/Apple TV)
- BLE fallback is slow (~2s per command vs ~200ms over Thread)
- Power readings update every ~10 seconds
- Historical energy data is stored only in the Eve app (not exposed via HAP)
- Voltage/current readings require Eve firmware 5.3+
- Cannot be controlled if all Thread Border Routers are offline
- The Shortcuts proxy must run on a Mac with Home.app signed in

### Integration Status: **Implemented** (via Shortcuts proxy)

---

## 7. Unknown/Identified Devices

Devices discovered during network probes that have been identified but not yet fully integrated.

### 192.168.1.144

| Property | Value |
|----------|-------|
| MAC Vendor | Espressif (ESP32) |
| Open Ports | 80 (HTTP), 443 |
| Hostname | ESP_A4CF12 |
| Notes | Likely an IoT sensor or smart plug with ESP32 chipset. HTTP port returns basic status JSON. |
| Integration Status | **Planned** - needs identification |

### 192.168.1.172

| Property | Value |
|----------|-------|
| MAC Vendor | Apple Inc. |
| Open Ports | 7000 (AirPlay), 3689 (DAAP) |
| Hostname | Apple-TV-Office |
| Notes | Apple TV 4K, acts as Thread Border Router. Controllable via pyatv. |
| Integration Status | **Planned** |

### 192.168.1.199

| Property | Value |
|----------|-------|
| MAC Vendor | Synology Inc. |
| Open Ports | 5000 (HTTP), 5001 (HTTPS), 6690 (Synology Drive), 139/445 (SMB) |
| Hostname | NAS |
| Notes | Synology NAS. REST API available at port 5001. Already mounted via SMB for file access. |
| Integration Status | **Planned** (API integration for health/status monitoring) |

### 192.168.1.128

| Property | Value |
|----------|-------|
| MAC Vendor | Raspberry Pi Foundation |
| Open Ports | 22 (SSH), 8080 (HTTP), 1883 (MQTT) |
| Hostname | rpi-homebridge |
| Notes | Raspberry Pi running Homebridge. MQTT broker on 1883 could be leveraged for device bridging. |
| Integration Status | **Planned** (MQTT bridge integration) |

### 192.168.1.136

| Property | Value |
|----------|-------|
| MAC Vendor | Amazon Technologies |
| Open Ports | 8008, 55443 |
| Hostname | echo-dot-office |
| Notes | Amazon Echo Dot. Limited local control; Alexa APIs are cloud-only. Port 8008 is for local media streaming. |
| Integration Status | **Not planned** (no useful local API) |

---

## Appendix A: Common Protocol Patterns

### UPnP/SOAP (Bose)

```python
def soap_request(ip, service, action, body_xml):
    """Generic UPnP SOAP request helper."""
    import requests
    url = f"http://{ip}:8091/{service}"
    headers = {"Content-Type": "text/xml", "SOAPAction": f'"urn:schemas-upnp-org:service:{service}:1#{action}"'}
    envelope = f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>{body_xml}</s:Body></s:Envelope>'
    return requests.post(url, data=envelope, headers=headers, timeout=5)
```

### eISCP (Onkyo)

```python
def eiscp_command(ip, command, port=60128):
    """Send raw eISCP command to Onkyo receiver."""
    import socket, struct
    msg = f"!1{command}\r"
    header = b"ISCP" + struct.pack(">IIBBBB", 16, len(msg), 1, 0, 0, 0)
    s = socket.create_connection((ip, port), timeout=5)
    s.sendall(header + msg.encode())
    resp = s.recv(1024); s.close()
    return resp.decode(errors="ignore")
```

### Shortcuts CLI Proxy (HomeKit)

```python
def homekit_control(device_ip, action, gang=None):
    """Control HomeKit device via Shortcuts proxy."""
    import requests
    url = f"http://localhost:37432/switch/{device_ip}"
    if gang: url += f"/gang/{gang}"
    url += f"/{action}"
    return requests.post(url, timeout=5)
```

---

## Appendix B: Network Topology Notes

- All devices are on the 192.168.1.0/24 subnet
- UniFi gateway at 192.168.1.1 provides DHCP with static leases for all controlled devices
- Thread mesh runs through Apple TV (192.168.1.172) and HomePod as border routers
- The Shortcuts CLI proxy runs on the Mac at port 37432 and bridges HomeKit commands
- Weather station pushes to a local receiver; configure target via its web interface
- Onkyo receivers maintain persistent TCP connections; only one client at a time

---

## Appendix C: Integration Architecture

```
                    +-------------------+
                    |   Nova Gateway    |
                    |  ws://127.0.0.1   |
                    |     :18789        |
                    +--------+----------+
                             |
              +--------------+--------------+
              |              |              |
     +--------v--+   +------v------+  +----v--------+
     | UPnP/SOAP |   |   eISCP     |  |  REST/HTTP  |
     | (Bose x3) |   | (Onkyo x2) |  | (UniFi,Wx)  |
     +-----------+   +-------------+  +-------------+
              |
     +--------v---------+
     | Shortcuts Proxy   |
     | :37432            |
     | (Koogeek, Eve)   |
     +-------------------+
```

All device integrations funnel through Nova's gateway for unified command dispatch, state tracking, and automation rule evaluation.
