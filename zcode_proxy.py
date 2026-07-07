#!/usr/bin/env python3
"""
ZCode Multi-Account Local Proxy Server
======================================

This lightweight proxy server intercepts requests from ZCode, handles the header
translation (from x-api-key to Authorization Bearer), performs smart account
rotation in real-time, and retries requests automatically if an account is 
rate-limited or unauthorized.

Usage:
1. Run this script in a terminal:
   python zcode_proxy.py

2. Configure ZCode settings:
   - Connection mode: API key
   - Base URL: http://127.0.0.1:1337
   - API key: dummy (any string)
"""

import http.server
import socketserver
import json
import requests
import sys
from pathlib import Path
from rich.console import Console

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Force UTF-8 encoding on standard streams to prevent Windows console encoding crashes
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

import account_router
import config
from utils.logging_config import get_logger

log = get_logger("zai.proxy")
console = Console()
PORT = 1337

class ZCodeProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        # Suppress standard logging to console to keep console clean for our prints
        pass

    def do_GET(self):
        """Simple health check or metadata endpoint."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({
            "status": "healthy",
            "service": "ZCode Multi-Account Proxy",
            "active_accounts_in_pool": len(account_router.load_accounts().get("accounts", []))
        }).encode("utf-8"))

    def do_OPTIONS(self):
        """Handle CORS pre-flight requests."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_POST(self):
        """Proxy post requests to Z.ai with token insertion and automatic failover."""
        content_length = int(self.headers.get('Content-Length', 0))
        req_body = self.rfile.read(content_length)

        max_retries = 5
        for attempt in range(max_retries):
            # 1. Fetch next healthy session
            session = account_router.get_next_session(deep_check=False)
            if not session:
                console.print("[bold red][Proxy Error] No healthy accounts available in the pool![/bold red]")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "No healthy accounts available in the pool"}).encode("utf-8"))
                return

            email = session["email"]
            token = None
            for cookie in session.get("cookies", []):
                if cookie.get("name") == "token":
                    token = cookie.get("value")
                    break

            if not token:
                console.print(f"[bold yellow][!] Account {email} has no token cookie. Marking invalid.[/bold yellow]")
                account_router.mark_invalid(email, "Missing token cookie")
                continue

            # 2. Forward request to Z.ai Billing Gateway
            target_url = f"https://zcode.z.ai/api/v1/zcode-plan/anthropic{self.path}"
            
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": self.headers.get("Content-Type", "application/json"),
                "Accept": self.headers.get("Accept", "*/*"),
                "User-Agent": "ZCode/2.0.0"
            }

            console.print(f"[cyan][*] Forwarding request via: {email} (Attempt {attempt + 1}/{max_retries})[/cyan]")

            try:
                resp = requests.post(
                    target_url,
                    data=req_body,
                    headers=headers,
                    stream=True,
                    timeout=45
                )

                # 3. Handle auth and limit signals
                if resp.status_code == 401:
                    console.print(f"[bold red][!] Auth failed for {email} (401). Discarding account...[/bold red]")
                    account_router.mark_invalid(email, "401 Unauthorized from gateway")
                    continue
                elif resp.status_code == 429:
                    console.print(f"[bold yellow][!] Quota limit reached for {email} (429). Rotating...[/bold yellow]")
                    account_router.mark_exhausted(email, reason="429 Limit from gateway")
                    continue
                elif resp.status_code >= 400:
                    console.print(f"[bold red][!] Server error {resp.status_code} from Z.ai. Passing response back.[/bold red]")
                    self.send_response(resp.status_code)
                    for k, v in resp.headers.items():
                        if k.lower() not in ('content-encoding', 'transfer-encoding', 'content-length'):
                            self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(resp.content)
                    return

                # 4. Stream response back to ZCode
                self.send_response(200)
                for k, v in resp.headers.items():
                    if k.lower() not in ('content-encoding', 'transfer-encoding', 'content-length'):
                        self.send_header(k, v)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                try:
                    for chunk in resp.iter_content(chunk_size=512):
                        if chunk:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    console.print(f"[bold green][OK] Request successfully completed using: {email}[/bold green]")
                except Exception as stream_err:
                    console.print(f"[bold red][!] Streaming connection closed: {stream_err}[/bold red]")
                return

            except requests.RequestException as conn_err:
                console.print(f"[bold red][!] Connection failure to Z.ai: {conn_err}[/bold red]")
                # Retry on connection/timeout issues as well
                continue

        # If all retries failed
        self.send_response(502)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": "Failed to complete request after rotating accounts."}).encode("utf-8"))


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    # Allows multiple concurrent streaming connections from ZCode
    daemon_threads = True


if __name__ == "__main__":
    console.print("====================================================", style="bold blue")
    console.print("      ZCode Multi-Account Local Proxy Server        ", style="bold white on blue")
    console.print("====================================================", style="bold blue")
    console.print(f"[*] Running local proxy on: [bold green]http://127.0.0.1:{PORT}[/bold green]")
    console.print("[*] Please configure ZCode Model Settings:")
    console.print("    - Connection mode: [bold yellow]API key[/bold yellow]")
    console.print(f"    - Base URL: [bold yellow]http://127.0.0.1:{PORT}[/bold yellow]")
    console.print("    - API key: [bold yellow]dummy[/bold yellow] (any value)")
    console.print("====================================================", style="bold blue")

    server = ThreadingHTTPServer(('127.0.0.1', PORT), ZCodeProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[bold yellow][*] Shutting down proxy server.[/bold yellow]")
        server.server_close()
