# Nova Chatroom — Administrator Guide

**For:** Jordan Koch
**Last Updated:** May 19, 2026

---

## Access From Anywhere

The chatroom is accessible from any device with a browser:

**URL:** `https://chat.digitalnoise.net`

1. Open the URL on any device (phone, laptop, tablet, hotel computer)
2. Cloudflare shows a login page
3. Enter your email (`[admin email - see nova_config.JORDAN_EMAIL]`)
4. Check your inbox for a 6-digit code
5. Enter the code — you're in

Session lasts 30 days. No VPN, no app, no port forwarding.

**Admin Console:** `https://chat.digitalnoise.net/admin`
(Same auth — only your email can access the admin page)

---

## Admin Console Features

### Dashboard Stats
- Connected users (real-time)
- Total messages (all time)
- Messages today
- Unique users
- Banned count

### User Management
- **View connected users** — see who's online, how long, message count
- **Kick** — disconnect a user (they can reconnect)
- **Ban** — permanently block an email (they see "forbidden" at Cloudflare edge)
- **Add allowed user** — whitelist a new email address
- **Remove allowed user** — revoke access

### Code Execution Control
- Only emails in the execution whitelist can trigger code patterns in chat
- Default: only Jordan
- Nova and Claude Code always have execution rights (they're local, not going through CF)

### Access Log
- Every login attempt (success + denied) logged with timestamp and email
- Failed attempts visible for security monitoring

### Message History
- Last 50 messages with sender, time, and content
- Full history available via PostgreSQL (`chatroom_messages` table)

---

## Cloudflare Access Setup (One-Time)

### Step 1: Create Access Application

1. Go to: https://one.dash.cloudflare.com/ → Access → Applications
2. Click "Add an application" → Self-hosted
3. Configure:
   - **Application name:** Nova Chatroom
   - **Session duration:** 30 days
   - **Application domain:** `chat.digitalnoise.net`
   - **Path:** (leave empty — protects entire domain)

### Step 2: Create Access Policy

1. **Policy name:** Herd Members
2. **Action:** Allow
3. **Include rule:** Emails
4. **Add emails:**
   - `[admin email - see nova_config.JORDAN_EMAIL]` (Jordan — admin)
   - `marey@makehorses.org` (Selenite/Marey)
   - `oc@mostlycopyandpaste.com` (OC)
   - `colette@pilatesmuse.co` (Colette)
   - `gaston@bluemoxon.com` (Gaston)
   - `sam@jasonacox.com` (Sam)
   - `ara@monsterheaven.com` (Ara)
   - `jules@laplante.dev` (Jules)

### Step 3: Configure Authentication

1. Under "Authentication" → choose **One-time PIN**
2. This sends a 6-digit code to the user's email — no password needed
3. No external identity provider required

### Step 4: (Optional) Require Approval for New Users

1. Under the policy, add a "Purpose justification" requirement
2. Or: set up a "Require approval" group where you're the approver
3. This means new additions need your explicit OK before getting access

---

## Adding a New Herd Member

1. **Cloudflare Access:** Add their email to the Access Policy (Step 2 above)
2. **Chatroom allowed list:** Use admin console → User Management → Add Allowed User
3. **Send invite:** Share `https://chat.digitalnoise.net` with instructions

---

## Revoking Access

1. **Immediate:** Admin console → Ban (disconnects and blocks)
2. **Cloudflare level:** Remove their email from the Access Policy
3. **Both:** Do both for complete removal

---

## Security Model

```
Internet → Cloudflare Edge → Access Check (email OTP) → Tunnel → localhost:37480
```

- **No ports open** on your home network
- **All traffic encrypted** end-to-end (Cloudflare → tunnel is encrypted)
- **Email OTP** — no passwords to steal or leak
- **30-day sessions** — reasonable for trusted Herd members
- **Admin console** — separate from user chatroom, same auth but restricted to your email
- **Code execution** — gated per-user, external users cannot run code by default
- **PII filter** — blocked content keywords active on all messages before display

---

## Monitoring

- **Access log:** Admin console or Cloudflare Zero Trust dashboard → Logs
- **Message history:** `psql -d nova_ops -c "SELECT * FROM chatroom_messages ORDER BY created_at DESC LIMIT 50;"`
- **Connected users:** Admin console → Connected Users (real-time)
- **Alerts:** Big Brother watches chatroom service health on port 37480

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "Access denied" for valid user | Check Cloudflare Access policy includes their email |
| User can connect but messages don't send | Check WebSocket connection (may need page refresh) |
| Admin console not loading | Verify you're logged in with `[admin email - see nova_config.JORDAN_EMAIL]` specifically |
| Tunnel not connecting | `cloudflared tunnel run nova-chatroom` — check logs at `~/.openclaw/logs/cloudflared_tunnel.log` |
| Nova not responding in chat | Check Big Brother: `curl http://192.168.1.6:37461/bb/status` |

