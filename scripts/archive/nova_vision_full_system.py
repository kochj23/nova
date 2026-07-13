#!/usr/bin/env python3
"""
MASTER ORCHESTRATOR: Motion + Claude + HomeKit
Runs all three tracks in parallel background processes.
"""

import subprocess
import os
from pathlib import Path
from datetime import datetime
import signal
import sys
import time
import json

SCRIPTS_DIR = Path.home() / ".openclaw/scripts"
WORKSPACE = Path.home() / ".openclaw/workspace"
PID_FILE = WORKSPACE / ".vision_system_pids.json"

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)

def start_track(name, script, args=[]):
    """Start a background track script."""
    try:
        script_path = SCRIPTS_DIR / script
        
        if not script_path.exists():
            log(f"✗ Script not found: {script}")
            return None
        
        # Make executable
        os.chmod(script_path, 0o755)
        
        # Start process
        cmd = ["python3", str(script_path)] + args
        
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        
        log(f"✓ Started {name} (PID {proc.pid})")
        return proc
    
    except Exception as e:
        log(f"✗ Failed to start {name}: {e}")
        return None

def save_pids(processes):
    """Save process IDs for later management."""
    pid_data = {
        "timestamp": datetime.now().isoformat(),
        "processes": {
            name: proc.pid for name, proc in processes.items() if proc
        }
    }
    
    try:
        PID_FILE.write_text(json.dumps(pid_data, indent=2))
        log(f"Saved PIDs: {PID_FILE}")
    except Exception as e:
        log(f"Failed to save PIDs: {e}")

def load_pids():
    """Load previously saved process IDs."""
    try:
        if PID_FILE.exists():
            return json.loads(PID_FILE.read_text())
    except:
        pass
    return None

def check_processes(processes):
    """Monitor running processes."""
    for name, proc in processes.items():
        if proc is None:
            continue
        
        if proc.poll() is not None:  # Process has exited
            log(f"⚠️  {name} exited (PID {proc.pid})")
            return False
    
    return True

def stop_all(processes):
    """Stop all running processes gracefully."""
    log("Stopping all vision tracks...")
    
    for name, proc in processes.items():
        if proc is None:
            continue
        
        try:
            proc.terminate()
            proc.wait(timeout=5)
            log(f"✓ Stopped {name}")
        except subprocess.TimeoutExpired:
            proc.kill()
            log(f"✓ Killed {name}")
        except Exception as e:
            log(f"Error stopping {name}: {e}")

def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully."""
    log("\nReceived stop signal, shutting down...")
    sys.exit(0)

def main():
    import sys
    
    if len(sys.argv) > 1:
        command = sys.argv[1]
        
        if command == "start":
            log("Starting full vision system...")
            
            # Start all three tracks
            processes = {
                "motion_detector": start_track(
                    "Motion Detector",
                    "nova_motion_detector_live.py"
                ),
                "homekit_occupancy": start_track(
                    "HomeKit Occupancy",
                    "nova_homekit_occupancy.py"
                ),
                "claude_analyzer": start_track(
                    "Claude Analyzer",
                    "nova_claude_vision_analyzer.py"
                )
            }
            
            # Save PIDs
            save_pids(processes)
            
            # Monitor processes
            log("\n" + "="*60)
            log("VISION SYSTEM RUNNING")
            log("="*60)
            log("Tracks:")
            log("  1. Motion detector (every 30s)")
            log("  2. HomeKit occupancy (every 5m)")
            log("  3. Claude analyzer (on-demand)")
            log("\nPress Ctrl+C to stop")
            log("="*60 + "\n")
            
            signal.signal(signal.SIGINT, signal_handler)
            
            try:
                while True:
                    if not check_processes(processes):
                        log("One or more processes died, restarting...")
                        # Could restart here, or exit
                        break
                    
                    time.sleep(10)
            
            except KeyboardInterrupt:
                pass
            finally:
                stop_all(processes)
                log("Vision system stopped")
        
        elif command == "stop":
            log("Stopping vision system...")
            
            pid_data = load_pids()
            if pid_data and "processes" in pid_data:
                for name, pid in pid_data["processes"].items():
                    try:
                        os.kill(pid, signal.SIGTERM)
                        log(f"✓ Stopped {name} (PID {pid})")
                    except ProcessLookupError:
                        log(f"✓ {name} not running")
                    except Exception as e:
                        log(f"✗ Error stopping {name}: {e}")
            else:
                log("No running processes found")
        
        elif command == "status":
            pid_data = load_pids()
            if pid_data:
                print(json.dumps(pid_data, indent=2))
            else:
                print("No vision system running")
        
        elif command == "daily-report":
            log("Generating daily vision report...")
            subprocess.run([
                "python3",
                str(SCRIPTS_DIR / "nova_claude_vision_analyzer.py"),
                "daily"
            ])
        
        elif command == "threat-report":
            log("Generating threat profile...")
            subprocess.run([
                "python3",
                str(SCRIPTS_DIR / "nova_claude_vision_analyzer.py"),
                "threat"
            ])
        
        elif command == "occupancy":
            log("Getting occupancy status...")
            result = subprocess.run([
                "python3",
                str(SCRIPTS_DIR / "nova_homekit_occupancy.py"),
                "status"
            ], capture_output=True, text=True)
            print(result.stdout)
        
        else:
            print("Usage:")
            print("  nova_vision_full_system.py start     — Start all tracks")
            print("  nova_vision_full_system.py stop      — Stop all tracks")
            print("  nova_vision_full_system.py status    — Show running processes")
            print("  nova_vision_full_system.py daily-report   — Generate daily report")
            print("  nova_vision_full_system.py threat-report  — Generate threat profile")
            print("  nova_vision_full_system.py occupancy      — Get current occupancy")
    
    else:
        print("Usage: nova_vision_full_system.py [start|stop|status|daily-report|threat-report|occupancy]")

if __name__ == "__main__":
    main()
