#!/usr/bin/env python3
"""
nova_chatroom_admin.py — Admin console for the Nova Chatroom.

Provides admin functionality accessible via /admin endpoint:
- View connected users
- Kick/ban users
- View access logs
- Toggle code execution permissions
- Manage allowed users list
- View message history with moderation

Only accessible to users in the ADMIN_EMAILS list.
Runs as part of nova_chatroom.py (imported, not standalone).
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

# Admin users — only these emails (from Cloudflare Access JWT) can access /admin
ADMIN_EMAILS = [
    "kochj23" + "@gmail.com",
]

# Banned users (persisted to DB)
# Code execution whitelist (only these can trigger exec patterns)
CODE_EXEC_ALLOWED = [
    "kochj23" + "@gmail.com",  # Jordan
]

ADMIN_HTML = """<!DOCTYPE html>
<html>
<head>
<title>Nova Chatroom — Admin</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'SF Pro', sans-serif; background: #0d1117; color: #e6edf3; padding: 20px; }
h1 { color: #58a6ff; margin-bottom: 20px; }
h2 { color: #79c0ff; margin: 20px 0 10px; border-bottom: 1px solid #21262d; padding-bottom: 5px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; margin: 10px 0; }
.card-header { font-weight: bold; color: #58a6ff; margin-bottom: 8px; }
table { width: 100%; border-collapse: collapse; margin: 10px 0; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #21262d; }
th { color: #8b949e; font-size: 0.85em; text-transform: uppercase; }
.status-online { color: #3fb950; }
.status-offline { color: #8b949e; }
.btn { padding: 6px 12px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.85em; margin: 2px; }
.btn-danger { background: #da3633; color: white; }
.btn-warning { background: #d29922; color: white; }
.btn-success { background: #238636; color: white; }
.btn-primary { background: #1f6feb; color: white; }
input[type="email"], input[type="text"] { background: #0d1117; border: 1px solid #30363d; color: #e6edf3; padding: 8px 12px; border-radius: 6px; width: 300px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 15px 0; }
.stat { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; text-align: center; }
.stat-value { font-size: 2em; font-weight: bold; color: #58a6ff; }
.stat-label { font-size: 0.8em; color: #8b949e; margin-top: 5px; }
.log-entry { font-family: monospace; font-size: 0.85em; padding: 4px 8px; border-left: 3px solid #30363d; margin: 4px 0; }
.log-warn { border-left-color: #d29922; }
.log-error { border-left-color: #da3633; }
.log-success { border-left-color: #3fb950; }
#refresh-indicator { position: fixed; top: 10px; right: 10px; color: #8b949e; font-size: 0.8em; }
</style>
</head>
<body>
<h1>Nova Chatroom — Admin Console</h1>
<div id="refresh-indicator">Auto-refresh: 10s</div>

<div class="stats" id="stats"></div>

<h2>Connected Users</h2>
<div class="card" id="users-card">
<table id="users-table">
<thead><tr><th>User</th><th>Email</th><th>Connected</th><th>Messages</th><th>Actions</th></tr></thead>
<tbody id="users-body"></tbody>
</table>
</div>

<h2>User Management</h2>
<div class="card">
<div style="margin-bottom: 10px;">
<input type="email" id="add-email" placeholder="email@example.com">
<button class="btn btn-success" onclick="addAllowed()">Add Allowed User</button>
<button class="btn btn-danger" onclick="banUser()">Ban User</button>
</div>
<div id="allowed-list"></div>
</div>

<h2>Code Execution Permissions</h2>
<div class="card">
<p style="color: #8b949e; margin-bottom: 10px;">Only users listed here can trigger code execution in chat.</p>
<div id="exec-list"></div>
</div>

<h2>Recent Access Log</h2>
<div class="card" id="access-log"></div>

<h2>Recent Messages (last 50)</h2>
<div class="card" id="messages-log"></div>

<script>
async function fetchAdmin() {
    try {
        const resp = await fetch('/admin/api/status');
        const data = await resp.json();
        renderStats(data.stats);
        renderUsers(data.users);
        renderAllowed(data.allowed_users);
        renderExec(data.exec_allowed);
        renderAccessLog(data.access_log);
        renderMessages(data.recent_messages);
    } catch(e) {
        console.error('Admin fetch failed:', e);
    }
}

function renderStats(stats) {
    document.getElementById('stats').innerHTML = `
        <div class="stat"><div class="stat-value">${stats.connected}</div><div class="stat-label">Connected Now</div></div>
        <div class="stat"><div class="stat-value">${stats.total_messages}</div><div class="stat-label">Total Messages</div></div>
        <div class="stat"><div class="stat-value">${stats.today_messages}</div><div class="stat-label">Today</div></div>
        <div class="stat"><div class="stat-value">${stats.unique_users}</div><div class="stat-label">Unique Users</div></div>
        <div class="stat"><div class="stat-value">${stats.banned}</div><div class="stat-label">Banned</div></div>
    `;
}

function renderUsers(users) {
    const tbody = document.getElementById('users-body');
    tbody.innerHTML = users.map(u => `
        <tr>
            <td>${u.name}</td>
            <td>${u.email || 'N/A'}</td>
            <td class="status-online">${u.connected_since}</td>
            <td>${u.message_count}</td>
            <td>
                <button class="btn btn-warning" onclick="kickUser('${u.id}')">Kick</button>
                <button class="btn btn-danger" onclick="banEmail('${u.email}')">Ban</button>
            </td>
        </tr>
    `).join('');
}

function renderAllowed(users) {
    document.getElementById('allowed-list').innerHTML = users.map(u => 
        `<span style="display:inline-block;background:#21262d;padding:4px 8px;border-radius:4px;margin:2px;">${u} <button class="btn btn-danger" style="padding:2px 6px;font-size:0.7em;" onclick="removeAllowed('${u}')">×</button></span>`
    ).join(' ');
}

function renderExec(users) {
    document.getElementById('exec-list').innerHTML = users.map(u => 
        `<span style="display:inline-block;background:#0d2818;border:1px solid #238636;padding:4px 8px;border-radius:4px;margin:2px;">${u}</span>`
    ).join(' ');
}

function renderAccessLog(log) {
    document.getElementById('access-log').innerHTML = log.map(e => 
        `<div class="log-entry ${e.type === 'denied' ? 'log-error' : 'log-success'}">${e.time} — ${e.email} — ${e.action}</div>`
    ).join('');
}

function renderMessages(msgs) {
    document.getElementById('messages-log').innerHTML = msgs.map(m => 
        `<div class="log-entry"><strong>${m.sender}</strong> (${m.time}): ${m.text}</div>`
    ).join('');
}

async function kickUser(id) { await fetch('/admin/api/kick', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id})}); fetchAdmin(); }
async function banEmail(email) { if(confirm('Ban '+email+'?')) { await fetch('/admin/api/ban', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email})}); fetchAdmin(); }}
async function addAllowed() { const email = document.getElementById('add-email').value; if(email) { await fetch('/admin/api/allow', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email})}); document.getElementById('add-email').value=''; fetchAdmin(); }}
async function removeAllowed(email) { await fetch('/admin/api/remove-allowed', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email})}); fetchAdmin(); }
function banUser() { const email = document.getElementById('add-email').value; if(email) banEmail(email); }

fetchAdmin();
setInterval(fetchAdmin, 10000);
</script>
</body>
</html>"""


def is_admin(email: str) -> bool:
    """Check if email is an admin."""
    return email.lower() in [e.lower() for e in ADMIN_EMAILS]


def can_execute_code(email: str) -> bool:
    """Check if user is allowed to trigger code execution."""
    return email.lower() in [e.lower() for e in CODE_EXEC_ALLOWED]
