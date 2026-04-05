#!/usr/bin/env python3
"""
nova_software_inventory.py — Daily software catalog.

Catalogs all installed software on macOS:
- Homebrew packages (brew)
- Global npm packages
- Python packages
- Applications in /Applications
- Command-line tools
- System frameworks

Stores as JSON for easy querying.
Runs daily at 4am.
"""

import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime

WORKSPACE = Path.home() / ".openclaw/workspace"
INVENTORY_DIR = WORKSPACE / "software-inventory"
INVENTORY_DIR.mkdir(exist_ok=True)

TODAY = datetime.now().isoformat().split("T")[0]
INVENTORY_FILE = INVENTORY_DIR / f"inventory-{TODAY}.json"
LATEST_FILE = INVENTORY_DIR / "inventory-latest.json"

def log(msg: str):
    print(f"[{datetime.now().isoformat()}] {msg}")

def run_command(cmd: list, timeout: int = 30) -> str:
    """Run shell command, return output."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception as e:
        log(f"Error running {cmd[0]}: {e}")
        return ""

def get_homebrew_packages() -> dict:
    """Get all Homebrew packages (brew + casks)."""
    result = {"formulae": [], "casks": [], "taps": []}
    
    # Formulae
    output = run_command(["brew", "list", "--versions"])
    for line in output.split("\n"):
        if line.strip():
            parts = line.split()
            if len(parts) >= 2:
                result["formulae"].append({
                    "name": parts[0],
                    "version": parts[1],
                })
    
    # Casks
    output = run_command(["brew", "list", "--cask", "--versions"])
    for line in output.split("\n"):
        if line.strip():
            parts = line.split()
            if len(parts) >= 2:
                result["casks"].append({
                    "name": parts[0],
                    "version": parts[1],
                })
    
    # Taps
    output = run_command(["brew", "tap"])
    for line in output.split("\n"):
        if line.strip():
            result["taps"].append(line.strip())
    
    return result

def get_npm_packages() -> list:
    """Get globally installed npm packages."""
    result = []
    output = run_command(["npm", "list", "-g", "--depth=0", "--json"])
    
    if output:
        try:
            data = json.loads(output)
            deps = data.get("dependencies", {})
            for name, info in deps.items():
                result.append({
                    "name": name,
                    "version": info.get("version", "unknown"),
                })
        except:
            pass
    
    return result

def get_applications() -> list:
    """Get all applications in /Applications."""
    result = []
    app_dir = Path("/Applications")
    
    if app_dir.exists():
        for app in app_dir.glob("*.app"):
            info_plist = app / "Contents" / "Info.plist"
            version = "unknown"
            
            if info_plist.exists():
                # Try to extract version from plist
                try:
                    output = run_command(["defaults", "read", str(info_plist), "CFBundleShortVersionString"])
                    if output:
                        version = output
                except:
                    pass
            
            result.append({
                "name": app.stem,
                "path": str(app),
                "version": version,
            })
    
    return result

def get_python_packages() -> list:
    """Get installed Python packages."""
    result = []
    output = run_command(["python3", "-m", "pip", "list", "--format=json"])
    
    if output:
        try:
            data = json.loads(output)
            for pkg in data:
                result.append({
                    "name": pkg.get("name"),
                    "version": pkg.get("version"),
                })
        except:
            pass
    
    return result

def get_cli_tools() -> list:
    """Get installed command-line tools."""
    result = []
    common_tools = [
        "git", "python3", "node", "npm", "docker", "kubectl", "terraform",
        "aws", "gcloud", "az", "ruby", "go", "rust", "java", "gcc",
        "clang", "openssl", "ssh", "curl", "wget", "vim", "neovim",
    ]
    
    for tool in common_tools:
        output = run_command(["which", tool])
        if output:
            version_output = run_command([tool, "--version"])
            version = version_output.split("\n")[0] if version_output else "installed"
            
            result.append({
                "name": tool,
                "path": output,
                "version": version[:100],  # Truncate long version strings
            })
    
    return result

def get_system_info() -> dict:
    """Get macOS system info."""
    result = {}
    
    # OS version
    output = run_command(["sw_vers"])
    for line in output.split("\n"):
        if "ProductVersion:" in line:
            result["os_version"] = line.split(":")[1].strip()
        if "BuildVersion:" in line:
            result["build_version"] = line.split(":")[1].strip()
    
    # Architecture
    output = run_command(["uname", "-m"])
    if output:
        result["architecture"] = output
    
    # Kernel version
    output = run_command(["uname", "-r"])
    if output:
        result["kernel"] = output
    
    return result

def main():
    log("=== Software Inventory Scan ===")
    
    inventory = {
        "timestamp": datetime.now().isoformat(),
        "system": get_system_info(),
        "homebrew": get_homebrew_packages(),
        "npm": get_npm_packages(),
        "python": get_python_packages(),
        "applications": get_applications(),
        "cli_tools": get_cli_tools(),
    }
    
    # Write today's inventory
    with open(INVENTORY_FILE, "w") as f:
        json.dump(inventory, f, indent=2)
    log(f"✓ Inventory saved: {INVENTORY_FILE}")
    
    # Write latest symlink
    with open(LATEST_FILE, "w") as f:
        json.dump(inventory, f, indent=2)
    log(f"✓ Latest inventory updated: {LATEST_FILE}")
    
    # Print summary
    stats = {
        "homebrew_formulae": len(inventory["homebrew"]["formulae"]),
        "homebrew_casks": len(inventory["homebrew"]["casks"]),
        "npm_packages": len(inventory["npm"]),
        "python_packages": len(inventory["python"]),
        "applications": len(inventory["applications"]),
        "cli_tools": len(inventory["cli_tools"]),
    }
    
    log("\n📦 INVENTORY SUMMARY:")
    for key, count in stats.items():
        log(f"  {key}: {count}")
    
    # Create a human-readable report
    report_file = INVENTORY_DIR / f"report-{TODAY}.txt"
    with open(report_file, "w") as f:
        f.write(f"Software Inventory Report - {TODAY}\n")
        f.write("=" * 60 + "\n\n")
        
        f.write(f"System: macOS {inventory['system'].get('os_version', 'unknown')} ({inventory['system'].get('architecture', 'unknown')})\n")
        f.write(f"Kernel: {inventory['system'].get('kernel', 'unknown')}\n\n")
        
        f.write(f"Homebrew Packages: {stats['homebrew_formulae']}\n")
        for pkg in sorted(inventory["homebrew"]["formulae"], key=lambda x: x["name"])[:20]:
            f.write(f"  • {pkg['name']} ({pkg['version']})\n")
        if stats['homebrew_formulae'] > 20:
            f.write(f"  ... and {stats['homebrew_formulae'] - 20} more\n")
        
        f.write(f"\nHomebrew Casks: {stats['homebrew_casks']}\n")
        for pkg in sorted(inventory["homebrew"]["casks"], key=lambda x: x["name"])[:10]:
            f.write(f"  • {pkg['name']} ({pkg['version']})\n")
        if stats['homebrew_casks'] > 10:
            f.write(f"  ... and {stats['homebrew_casks'] - 10} more\n")
        
        f.write(f"\nApplications: {stats['applications']}\n")
        for app in sorted(inventory["applications"], key=lambda x: x["name"])[:15]:
            f.write(f"  • {app['name']} ({app['version']})\n")
        if stats['applications'] > 15:
            f.write(f"  ... and {stats['applications'] - 15} more\n")
        
        f.write(f"\nCLI Tools: {stats['cli_tools']}\n")
        for tool in sorted(inventory["cli_tools"], key=lambda x: x["name"]):
            f.write(f"  • {tool['name']}: {tool['version'][:50]}\n")
    
    log(f"✓ Human-readable report: {report_file}")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
