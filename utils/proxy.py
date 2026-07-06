"""
Proxy management with full per-account assignment support.

Extends the original skeleton with:
- Round-robin + random + least-recently-used selection policies.
- URL parsing that extracts username/password/host for Playwright's
  ``proxy={server, username, password}`` form.
- Optional lightweight liveness probe (HEAD via requests through the proxy)
  so we avoid handing out a known-dead proxy during account creation.
- Per-account assignment + sticky-binding so an account always uses the same
  proxy (preserves IP-reputation / session coherence).
- Env-var + config-file driven bootstrap (``ZAI_PROXIES``).
"""

from __future__ import annotations

import os
import random
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, unquote

from rich.console import Console

import config

console = Console()


def parse_proxy_url(proxy: str) -> Dict[str, str]:
    """
    Parse a proxy URL into Playwright's proxy dict form.

    Accepts:
      - ``http://user:pass@host:port``
      - ``http://host:port``
      - ``socks5://user:pass@host:port``
      - Bare ``host:port`` (treated as HTTP).
    Returns ``{"server": "...", "username": "...", "password": "..."}`` with
    the latter two omitted when not present.
    """
    if not proxy:
        return {}
    proxy = proxy.strip()
    if "://" not in proxy:
        proxy = f"http://{proxy}"

    parsed = urlparse(proxy)
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"

    out: Dict[str, str] = {"server": server}
    if parsed.username:
        out["username"] = unquote(parsed.username)
    if parsed.password:
        out["password"] = unquote(parsed.password)
    return out


class ProxyManager:
    """
    Proxy pool with selection policies + optional liveness checks.

    For production, load from env or config file. Format examples::

        "http://user:pass@host:port"
        "socks5://host:port"
        "host:port"
    """

    def __init__(
        self,
        proxies: Optional[List[str]] = None,
        *,
        policy: str = "round_robin",
        sticky: bool = True,
        probe_on_add: bool = False,
    ):
        self.proxies: List[str] = []
        self.policy = policy
        self.sticky = sticky
        self.probe_on_add = probe_on_add
        self._index = 0
        self._lock = threading.Lock()
        # email/account-key -> proxy string (sticky binding)
        self._bindings: Dict[str, str] = {}
        # proxy string -> last assigned timestamp (LRU)
        self._last_used: Dict[str, float] = defaultdict(float)
        # cache of known-dead proxies (with expiry)
        self._dead: Dict[str, float] = {}
        self._dead_ttl_sec = 300

        for p in proxies or []:
            self.add_proxy(p)

    # ---------------- mutation ----------------
    def add_proxy(self, proxy: str, *, force: bool = False) -> bool:
        if not proxy:
            return False
        proxy = proxy.strip()
        if not force and proxy in self.proxies:
            return False
        if self.probe_on_add and not self._probe(proxy):
            console.print(f"[yellow]Proxy unreachable, skipped: {self._redact(proxy)}[/yellow]")
            return False
        if proxy not in self.proxies:
            self.proxies.append(proxy)
        return True

    def remove_proxy(self, proxy: str) -> None:
        with self._lock:
            self.proxies = [p for p in self.proxies if p != proxy]
            self._dead.pop(proxy, None)
            self._last_used.pop(proxy, None)

    def from_env(self, env_var: Optional[str] = None) -> int:
        """Load comma-separated proxies from env. Returns count added."""
        var_name = env_var or str(config.get("proxy_env_var", "ZAI_PROXIES"))
        raw = os.getenv(var_name, "")
        added = 0
        for chunk in [x.strip() for x in raw.split(",") if x.strip()]:
            if self.add_proxy(chunk):
                added += 1
        return added

    def from_list(self, items: List[str]) -> int:
        added = 0
        for it in items or []:
            if self.add_proxy(it):
                added += 1
        return added

    # ---------------- selection ----------------
    def _alive(self) -> List[str]:
        now = time.time()
        return [p for p in self.proxies
                if now - self._dead.get(p, 0) > self._dead_ttl_sec]

    def get_proxy(self, *, for_key: Optional[str] = None) -> Optional[str]:
        """
        Return next proxy. Honours sticky binding when ``for_key`` is set
        (e.g. account email).
        """
        if not self.proxies:
            return None
        if self.sticky and for_key and for_key in self._bindings:
            bound = self._bindings[for_key]
            if bound in self._alive():
                return bound
            # Binding is dead - fall through and rebind.

        with self._lock:
            alive = self._alive()
            if not alive:
                return None
            if self.policy == "random":
                proxy = random.choice(alive)
            elif self.policy == "lru":
                proxy = min(alive, key=lambda p: self._last_used.get(p, 0.0))
            else:  # round_robin
                proxy = alive[self._index % len(alive)]
                self._index += 1
            self._last_used[proxy] = time.time()

        if self.sticky and for_key:
            self._bindings[for_key] = proxy
        return proxy

    def get_random_proxy(self) -> Optional[str]:
        if not self.proxies:
            return None
        return random.choice(self._alive() or self.proxies)

    def bind(self, key: str, proxy: str) -> None:
        """Explicitly bind a key (e.g. email) to a specific proxy string."""
        self._bindings[key] = proxy

    def get_bound(self, key: str) -> Optional[str]:
        return self._bindings.get(key)

    def mark_dead(self, proxy: str, ttl_sec: Optional[int] = None) -> None:
        with self._lock:
            self._dead[proxy] = time.time()
            self._last_used.pop(proxy, None)
        if ttl_sec is not None:
            self._dead_ttl_sec = int(ttl_sec)

    def is_dead(self, proxy: str) -> bool:
        return (time.time() - self._dead.get(proxy, 0)) <= self._dead_ttl_sec

    # ---------------- playwright helpers ----------------
    def to_playwright_proxy(self, proxy: Optional[str]) -> Optional[Dict[str, str]]:
        """Convert a string proxy into Playwright's proxy dict (or None)."""
        if not proxy:
            return None
        return parse_proxy_url(proxy)

    def get_playwright_proxy(self, for_key: Optional[str] = None) -> Optional[Dict[str, str]]:
        return self.to_playwright_proxy(self.get_proxy(for_key=for_key))

    # ---------------- diagnostics ----------------
    def __len__(self) -> int:
        return len(self.proxies)

    def summary(self) -> Dict[str, Any]:
        return {
            "total": len(self.proxies),
            "alive": len(self._alive()),
            "dead": len(self.proxies) - len(self._alive()),
            "bindings": len(self._bindings),
            "policy": self.policy,
            "sticky": self.sticky,
        }

    # ---------------- internal ----------------
    def _redact(self, proxy: str) -> str:
        parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
        if parsed.username or parsed.password:
            return proxy.replace(parsed.password or "", "***") if parsed.password else proxy
        return proxy

    def _probe(self, proxy: str, timeout: float = 6.0) -> bool:
        """Lightweight HEAD request through the proxy to check liveness."""
        try:
            import requests
            r = requests.head(
                "https://chat.z.ai/",
                proxies={"http": proxy, "https": proxy},
                timeout=timeout,
                allow_redirects=False,
            )
            return r.status_code < 500
        except Exception:
            return False


# ------------------------------------------------------------
# Module-level singleton convenience
# ------------------------------------------------------------

_DEFAULT_MANAGER: Optional[ProxyManager] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_manager() -> ProxyManager:
    """Lazy shared ProxyManager initialised from env (ZAI_PROXIES)."""
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_MANAGER is None:
                mgr = ProxyManager(
                    policy=str(config.get("proxy_policy", "round_robin") or "round_robin"),
                    sticky=bool(config.get("proxy_assign_per_account", True)),
                    probe_on_add=bool(config.get("proxy_probe_on_add", False)),
                )
                mgr.from_env()
                _DEFAULT_MANAGER = mgr
    return _DEFAULT_MANAGER


def reset_default_manager() -> None:
    """Reset the shared manager (used by tests / config reload)."""
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        _DEFAULT_MANAGER = None


__all__ = [
    "ProxyManager",
    "parse_proxy_url",
    "get_default_manager",
    "reset_default_manager",
]
