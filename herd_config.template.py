"""
herd_config.template.py — Template for Nova's herd of AI peers.

Copy to herd_config.py and fill in your own herd's email addresses.
herd_config.py is gitignored and never committed.
"""

HERD = [
    {"name": "Sam",     "email": "sam@example.com",     "profile": "sam.md"},
    {"name": "O.C.",    "email": "oc@example.com",      "profile": "oc.md"},
    {"name": "Gaston",  "email": "gaston@example.com",  "profile": "gaston.md"},
    {"name": "Marey",   "email": "marey@example.com",   "profile": "marey.md"},
    {"name": "Colette", "email": "colette@example.com", "profile": "colette.md"},
    {"name": "Rockbot", "email": "rockbot@example.com", "profile": "rockbot.md"},
]

HERD_EMAILS = {m["email"] for m in HERD}
