"""
=============================================================================
FastAPI / Uvicorn Process Supervisor & Watchdog (PM2-Style)
=============================================================================
Features:
  1. Auto-Refresh: Automatically restarts the server every 5 minutes (300s)
     to clear memory leaks, deadlocks, hung event loops, and stale sockets.
  2. Active Watchdog: Pings http://127.0.0.1:8000/api/health every 10s.
     If the server freezes or hangs, it terminates and restarts it instantly.
  3. Crash Recovery: Automatically restarts if uvicorn exits unexpectedly.
  4. Socket Protection: Runs uvicorn with --timeout-keep-alive 30 and --backlog 2048
     to prevent Windows socket exhaustion (ERR_CONNECTION_TIMED_OUT).
  5. Clean Shutdown: Properly kills process trees on Ctrl+C (no orphan ports).
=============================================================================
"""

import os
import sys
import time
import socket
import signal
import argparse
import subprocess
import urllib.request
import urllib.error
from datetime import datetime

# Configuration Defaults
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_REFRESH_INTERVAL = 300  # 5 minutes in seconds
HEALTH_CHECK_INTERVAL = 10      # Ping every 10 seconds
HEALTH_CHECK_TIMEOUT = 5        # Timeout after 5 seconds
MAX_CONSECUTIVE_FAILURES = 2    # Restart if 2 health checks fail in a row

current_process = None
is_shutting_down = False

def get_lan_ip():
    """Detect local LAN IPv4 address (e.g. 192.168.0.x)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        # Connect to a public DNS IP (doesn't send data) to determine outgoing interface
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def kill_process_tree(proc):
    """Cleanly and forcefully terminate a process and all its children on Windows/Unix."""
    if proc is None:
        return
    try:
        if sys.platform == "win32":
            # Force kill process tree using taskkill on Windows
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass

def check_server_health(port: int) -> bool:
    """Ping the /api/health endpoint to verify the server is alive and not hung."""
    url = f"http://127.0.0.1:{port}/api/health"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ServerSupervisor/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=HEALTH_CHECK_TIMEOUT) as response:
            return response.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, Exception):
        return False

def start_server_process(host: str, port: int, workspace_dir: str):
    """Launch the uvicorn server with optimized production flags."""
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "backend.main:app",
        "--host", host,
        "--port", str(port),
        "--timeout-keep-alive", "30",
        "--backlog", "2048",
        "--log-level", "info"
    ]
    
    # Environment variables
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    
    return subprocess.Popen(
        cmd,
        cwd=workspace_dir,
        env=env
    )

def handle_signal(sig, frame):
    """Handle Ctrl+C and SIGTERM gracefully."""
    global is_shutting_down, current_process
    if is_shutting_down:
        return
    is_shutting_down = True
    print("\n\033[93m[SUPERVISOR] Shutting down server gracefully...\033[0m")
    if current_process:
        kill_process_tree(current_process)
    sys.exit(0)

def main():
    global current_process, is_shutting_down

    parser = argparse.ArgumentParser(description="PM2-style FastAPI Process Supervisor")
    parser.add_argument("--host", default=os.getenv("HOST", DEFAULT_HOST), help="Host interface (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", DEFAULT_PORT)), help="Port number (default: 8000)")
    parser.add_argument("--interval", type=int, default=int(os.getenv("REFRESH_INTERVAL", DEFAULT_REFRESH_INTERVAL)),
                        help="Auto-refresh interval in seconds (default: 300 = 5 min)")
    args = parser.parse_args()

    host = args.host
    port = args.port
    refresh_interval = args.interval
    workspace_dir = os.path.dirname(os.path.abspath(__file__))

    # Force UTF-8 encoding on Windows console if supported
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Register signal handlers
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    lan_ip = get_lan_ip()

    print("\033[92m" + "=" * 65 + "\033[0m")
    print(f"\033[1;96m[*] FASTAPI AUTO-REFRESH SUPERVISOR & WATCHDOG (PM2 STYLE)\033[0m")
    print("\033[92m" + "=" * 65 + "\033[0m")
    print(f"  * Local URL:      \033[1mhttp://localhost:{port}\033[0m")
    print(f"  * Network URL:    \033[1;32mhttp://{lan_ip}:{port}\033[0m")
    print(f"  * Auto-Refresh:   \033[1;33mEvery {refresh_interval} seconds ({refresh_interval / 60:.1f} minutes)\033[0m")
    print(f"  * Watchdog:       \033[1;34mHealth check every {HEALTH_CHECK_INTERVAL}s (Auto-recovers if hung)\033[0m")
    print(f"  * Failure Shield: \033[1;35mTimeout / Keep-Alive limits active\033[0m")
    print("\033[92m" + "=" * 65 + "\033[0m\n")

    consecutive_failures = 0
    restart_count = 0

    while not is_shutting_down:
        restart_count += 1
        now_str = datetime.now().strftime("%H:%M:%S")
        print(f"\033[94m[{now_str}] [SUPERVISOR] Starting Uvicorn server (Cycle #{restart_count})...\033[0m")
        
        current_process = start_server_process(host, port, workspace_dir)
        process_start_time = time.time()
        time.sleep(2)  # Allow initial startup

        # Loop until next refresh interval or process failure
        while not is_shutting_down:
            time.sleep(HEALTH_CHECK_INTERVAL)

            # Check if process died unexpectedly
            ret = current_process.poll()
            if ret is not None:
                print(f"\033[91m[SUPERVISOR WARNING] Server process exited with code {ret}. Restarting immediately...\033[0m")
                break

            elapsed = time.time() - process_start_time
            remaining = max(0, int(refresh_interval - elapsed))

            # Health check watchdog
            is_healthy = check_server_health(port)
            if is_healthy:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                print(f"\033[91m[WATCHDOG WARNING] Health check failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})...\033[0m")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"\033[91;1m[WATCHDOG ALERT] Server unresponsive / hung! Triggering emergency restart...\033[0m")
                    consecutive_failures = 0
                    break

            # Check if 5-minute refresh interval reached
            if elapsed >= refresh_interval:
                print(f"\033[93m[{datetime.now().strftime('%H:%M:%S')}] [AUTO-REFRESH] 5-minute interval reached. Refreshing server cleanly...\033[0m")
                break

        # Recycle process
        if current_process and not is_shutting_down:
            kill_process_tree(current_process)
            current_process = None
            time.sleep(1)  # Brief pause to allow OS socket release

if __name__ == "__main__":
    main()
