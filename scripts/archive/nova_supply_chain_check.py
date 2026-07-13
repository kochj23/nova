#!/usr/bin/env python3
"""
nova_supply_chain_check.py — Supply chain attack prevention.

Scans for malicious dependencies related to known attacks:
- NullBulge (ComfyUI_LLMVISION, BeamNG mods, axios)
- Suspicious postinstall scripts
- Obfuscated code
- Discord webhook patterns

Runs as cron job daily.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime

WORKSPACE = Path.home() / ".openclaw/workspace"
SCRIPTS = Path.home() / ".openclaw/scripts"
LOG_FILE = Path.home() / ".openclaw/logs/supply_chain_check.log"

# Malicious patterns from known attacks
MALICIOUS_PATTERNS = {
    "nullbulge": [
        "plain-crypto-js",  # axios attack
        "ComfyUI_LLMVISION",
        "AppleBotzz",
        "SillyTavern",
    ],
    "infostealer": [
        "Fadmino", "admin.py", "cadmino",  # Data exfiltrators
        "chrome_cookie", "firefox_password",
        "discord_webhook",
    ],
    "obfuscated": [
        "eval(", "exec(", "base64", "obfuscat",
    ],
    "suspicious_urls": [
        "pixeldrain", "modsfire", "pastebin",
    ]
}

def log(msg: str):
    """Log to file."""
    ts = datetime.now().isoformat()
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def scan_directory(path: Path) -> dict:
    """Scan a directory for malicious dependencies."""
    results = {
        "path": str(path),
        "issues": [],
        "warnings": [],
    }
    
    # Check package.json
    pkg_json = path / "package.json"
    if pkg_json.exists():
        try:
            with open(pkg_json) as f:
                pkg = json.load(f)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            
            for dep_name in deps:
                # Check for known malicious packages
                for category, patterns in MALICIOUS_PATTERNS.items():
                    for pattern in patterns:
                        if pattern.lower() in dep_name.lower():
                            results["issues"].append(f"Suspicious dependency: {dep_name} (matches {category}:{pattern})")
        except Exception as e:
            results["warnings"].append(f"Error reading package.json: {e}")
    
    # Check requirements.txt
    req_txt = path / "requirements.txt"
    if req_txt.exists():
        try:
            with open(req_txt) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    
                    # Check for malicious packages
                    for category, patterns in MALICIOUS_PATTERNS.items():
                        for pattern in patterns:
                            if pattern.lower() in line.lower():
                                results["issues"].append(f"Suspicious Python dependency: {line} (matches {category}:{pattern})")
                    
                    # Check for obfuscated installs
                    if any(x in line for x in ["http://", "https://", "git+", "file://"]):
                        if not any(x in line for x in ["github.com", "pypi.org", "docs"]):
                            results["warnings"].append(f"Non-standard package source: {line}")
        except Exception as e:
            results["warnings"].append(f"Error reading requirements.txt: {e}")
    
    # Check for postinstall scripts
    pkg_json = path / "package.json"
    if pkg_json.exists():
        try:
            with open(pkg_json) as f:
                pkg = json.load(f)
            scripts = pkg.get("scripts", {})
            
            for name, script in scripts.items():
                if "postinstall" in name.lower() or "install" in name.lower():
                    # Check for obfuscated/suspicious code
                    if any(x in script for x in ["eval(", "exec(", "require(Buffer"]):
                        results["issues"].append(f"Suspicious postinstall script: {name}")
        except:
            pass
    
    return results

def scan_installed_packages() -> dict:
    """Scan installed npm and pip packages."""
    results = {
        "npm": {"issues": [], "warnings": []},
        "pip": {"issues": [], "warnings": []},
    }
    
    # Check npm global packages
    try:
        output = subprocess.run(["npm", "list", "-g", "--json"], 
                              capture_output=True, text=True, timeout=10)
        if output.returncode == 0:
            packages = json.loads(output.stdout)
            deps = packages.get("dependencies", {})
            
            for name in deps:
                for category, patterns in MALICIOUS_PATTERNS.items():
                    for pattern in patterns:
                        if pattern.lower() in name.lower():
                            results["npm"]["issues"].append(f"Suspicious global npm: {name}")
    except Exception as e:
        results["npm"]["warnings"].append(f"npm scan error: {e}")
    
    # Check pip packages (if applicable)
    try:
        output = subprocess.run(["python3", "-m", "pip", "list", "--format=json"],
                              capture_output=True, text=True, timeout=10)
        if output.returncode == 0:
            packages = json.loads(output.stdout)
            for pkg in packages:
                name = pkg.get("name", "")
                for category, patterns in MALICIOUS_PATTERNS.items():
                    for pattern in patterns:
                        if pattern.lower() in name.lower():
                            results["pip"]["issues"].append(f"Suspicious pip package: {name}")
    except Exception as e:
        results["pip"]["warnings"].append(f"pip scan error: {e}")
    
    return results

def main():
    log("=== Supply Chain Security Check ===")
    
    all_issues = []
    all_warnings = []
    
    # Scan project directories
    projects_to_scan = [
        Path.home() / "code",
        Path.home() / "projects",
        Path.home() / ".openclaw/workspace",
    ]
    
    for proj_path in projects_to_scan:
        if proj_path.exists():
            log(f"Scanning {proj_path}...")
            result = scan_directory(proj_path)
            all_issues.extend(result["issues"])
            all_warnings.extend(result["warnings"])
    
    # Scan installed packages
    log("Checking installed packages...")
    installed = scan_installed_packages()
    all_issues.extend(installed["npm"]["issues"])
    all_issues.extend(installed["pip"]["issues"])
    all_warnings.extend(installed["npm"]["warnings"])
    all_warnings.extend(installed["pip"]["warnings"])
    
    # Report
    if all_issues:
        log(f"\n⚠️  FOUND {len(all_issues)} ISSUES:")
        for issue in all_issues:
            log(f"  - {issue}")
    else:
        log("✅ No malicious dependencies detected")
    
    if all_warnings:
        log(f"\n📝 {len(all_warnings)} WARNINGS:")
        for warning in all_warnings:
            log(f"  - {warning}")
    
    # Post to Slack if issues found
    if all_issues:
        try:
            slack_msg = f"🚨 *Supply Chain Alert*\n{len(all_issues)} suspicious dependencies found:\n"
            slack_msg += "\n".join([f"  • {issue}" for issue in all_issues[:5]])
            if len(all_issues) > 5:
                slack_msg += f"\n  ... and {len(all_issues)-5} more"
            
            subprocess.run([
                sys.executable, str(SCRIPTS / "nova_slack_notify.py"),
                slack_msg
            ], timeout=10)
        except:
            pass
    
    log("Scan complete.")
    return 1 if all_issues else 0

if __name__ == "__main__":
    sys.exit(main())
