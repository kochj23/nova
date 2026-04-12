# Security Policy

## Scope

NovaControl binds exclusively to `127.0.0.1` (loopback). It is not exposed to the network and provides read-only access to local app data files.

## Reporting a Vulnerability

Report security issues to: kochj@digitalnoise.net

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact

Do not open a public GitHub issue for security vulnerabilities.

## Security Features

- Loopback-only API (127.0.0.1:37400)
- Read-only file access — never writes to app data
- No credentials stored or transmitted
- No network requests made (all data is local)
