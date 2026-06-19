#!/usr/bin/env python3
"""
Start SAMSEL DJ Engine Pro Web App with public URL (internet access).
Uses Cloudflare Tunnel (cloudflared) or ngrok - no port forwarding needed.

Prereqs: Install one of:
  - cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/
  - ngrok: https://ngrok.com/download
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8000


def find_tunnel_cmd():
    """Return (cmd, args) for cloudflared or ngrok, or None."""
    for name in ("cloudflared", "ngrok"):
        path = shutil.which(name)
        if path:
            if name == "cloudflared":
                return (path, ["tunnel", "--url", f"http://{HOST}:{PORT}", "--protocol", "http2"])
            if name == "ngrok":
                return (path, ["http", str(PORT)])
    return None


def start_uvicorn():
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app:app",
            "--host", "0.0.0.0", f"--port={PORT}",
            "--timeout-keep-alive", "300",
        ],
        cwd=Path(__file__).resolve().parent,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def main():
    tunnel = find_tunnel_cmd()
    if not tunnel:
        print("To get a public URL, install cloudflared or ngrok:")
        print("  cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/")
        print("  ngrok: https://ngrok.com/download")
        print()
        print("Starting server for LAN access only (http://0.0.0.0:8000)...")
        subprocess.run([sys.executable, "-m", "uvicorn", "app:app", "--host", "0.0.0.0", f"--port={PORT}"])
        return

    print("Starting SAMSEL DJ Engine Pro...")
    uvicorn_proc = start_uvicorn()

    tunnel_proc_holder = []

    def cleanup():
        try:
            uvicorn_proc.terminate()
            uvicorn_proc.wait(timeout=3)
        except Exception:
            uvicorn_proc.kill()
        for p in tunnel_proc_holder:
            if p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=2)
                except Exception:
                    p.kill()

    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    time.sleep(2)
    if uvicorn_proc.poll() is not None:
        print("Server failed to start.")
        sys.exit(1)

    cmd, args = tunnel
    is_ngrok = "ngrok" in cmd
    print(f"Starting tunnel ({'ngrok' if is_ngrok else 'cloudflared'})...")
    tunnel_proc = subprocess.Popen(
        [cmd] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    tunnel_proc_holder.append(tunnel_proc)

    def read_tunnel():
        url = None
        for line in iter(tunnel_proc.stdout.readline, ""):
            print(line, end="")
            if url:
                continue
            # cloudflared: "https://xxxx.trycloudflare.com"
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m:
                url = m.group(0)
            # ngrok: "Forwarding https://xxxx.ngrok-free.app -> http://localhost:8000"
            if not url:
                m = re.search(r"https://[a-z0-9-]+\.ngrok[-a-z0-9.]*\.app", line)
                if m:
                    url = m.group(0)
            if url:
                print()
                print("=" * 60)
                print("  PUBLIC URL (share to access from anywhere):")
                print(f"  {url}")
                print("=" * 60)
                print()

    t = threading.Thread(target=read_tunnel, daemon=True)
    t.start()

    try:
        tunnel_proc.wait()
    except KeyboardInterrupt:
        pass
    cleanup()


if __name__ == "__main__":
    main()
