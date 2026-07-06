"""
Temporary email integration with multi-provider fallback.

Backends are pluggable. Each implements the ``TempMailProvider`` interface:

    class TempMailProvider:
        name: str
        def create_account(self) -> str: ...        # returns email address
        def wait_for_verification_link(self) -> str: ...  # returns https URL
        def cleanup(self) -> None: ...

The public ``TempMailManager`` retains the original API for backward
compatibility (``create_account`` / ``wait_for_verification_link`` /
``cleanup``), but now orchestrates a list of providers with automatic
fallback: if a provider fails to create a mailbox OR to retrieve a
verification link, the next provider is tried.

Backends ship out of the box:

- ``mail.tm``      - REST API (https://api.mail.tm), token-authenticated polling.
- ``guerrillamail`` - JSON API (api.guerrillamail.com), session-based.
- ``1secmail``     - JSON API (www.1secmail.com), stateless polling.

Detection of the Z.ai verification link is shared and improved:

- Accepts plain text + HTML bodies (strips tags before regex).
- Recognises a wide range of sender/subject hints (z.ai / verify / confirm /
  magic / registration / welcome / activate / signin).
- Filters candidate URLs to those whose host or path strongly suggests a
  verification action, then falls back to any ``chat.z.ai`` URL.
- Trailing punctuation / query artifacts are sanitised.

Verification-link heuristics are centralised so all providers benefit from
fixes.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, Dict, List, Optional, Set

import requests
from rich.console import Console

import config

console = Console()

# ------------------------------------------------------------
# Shared verification-link extraction
# ------------------------------------------------------------

# Senders / subjects that are considered relevant to Z.ai signup.
RELEVANT_SENDER_HINTS = (
    "z.ai", "zai", "zhipu", "chat.z.ai",
    "noreply", "no-reply", "verify", "verification",
    "confirm", "registration", "welcome", "activate", "signin", "sign-in",
    "magic", "auth",
)

# URL substrings that strongly indicate a verification action.
VERIFY_URL_HINTS = (
    "verify", "confirm", "verification", "auth", "token",
    "magic", "callback", "activate", "registration", "signin", "sign-in",
    "chat.z.ai",
)

# Words found inside the email body that hint the link is the verification CTA.
VERIFY_BODY_HINTS = (
    "verify your", "confirm your", "activate your", "click here",
    "verify the email", "verification link", "magic link", "sign in",
    "complete your registration", "click the link", "click below",
)


def _strip_html(html: str) -> str:
    """Crude HTML -> text: drop tags + decode a few common entities."""
    if not html:
        return ""
    # Pull href targets before stripping so we keep all links.
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    # Anchorize: keep href visible
    text = re.sub(r'(?is)<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                  r" \1 \2 ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (text
            .replace("&amp;", "&")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&nbsp;", " "))
    return text


def _normalise_link(link: str) -> str:
    """Trim trailing punctuation / stray angle brackets from a scraped URL."""
    link = link.strip().rstrip(".,;:!)]}>\"'")
    # Balance trailing paren if the URL clearly contained an open one.
    if link.count("(") > link.count(")"):
        link = link.rsplit("(", 1)[0].strip()
    return link


def _extract_links(body: str) -> List[str]:
    """Return all http(s) links found in a text/html body."""
    if not body:
        return []
    import html
    body = html.unescape(body)
    # Catch URLs that may include query params + fragments.
    raw = re.findall(r'https?://[^\s<>"\']+', body)
    return [_normalise_link(l) for l in raw if l]


def is_relevant_message(from_addr: str, subject: str, body: str) -> bool:
    """Heuristic: does this email look like the Z.ai verification mail?"""
    from_addr = (from_addr or "").lower()
    subject = (subject or "").lower()
    body_lower = (body or "").lower()

    if any(h in from_addr for h in RELEVANT_SENDER_HINTS):
        return True
    if any(h in subject for h in RELEVANT_SENDER_HINTS):
        return True
    if any(h in body_lower for h in VERIFY_BODY_HINTS):
        return True
    # A chat.z.ai link in the body is a strong relevance signal.
    if "chat.z.ai" in body_lower:
        return True
    return False


def pick_verification_link(body: str) -> Optional[str]:
    """
    Pick the best verification link from an email body.

    Strategy:
      1. Find links whose URL contains a verify-style hint.
      2. Among those, prefer chat.z.ai links.
      3. If none, fall back to any chat.z.ai link.
      4. Otherwise fall back to the first link in the email.
    """
    links = _extract_links(body)
    if not links:
        return None

    def hint_score(link: str) -> int:
        low = link.lower()
        score = 0
        if "chat.z.ai" in low:
            score += 5
        if any(h in low for h in VERIFY_URL_HINTS):
            score += 3
        return score

    scored = sorted(links, key=hint_score, reverse=True)
    if scored and hint_score(scored[0]) > 0:
        return scored[0]
    # Last resort: first link in the email.
    return scored[0] if scored else None


# ------------------------------------------------------------
# Provider interface
# ------------------------------------------------------------

class TempMailProvider:
    """Base class. Subclasses implement the three core methods."""

    name: str = "base"

    def __init__(self, timeout: int, poll_interval: int):
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.address: Optional[str] = None
        self._session = requests.Session()
        # generous default; providers can override per-call.
        self._session.headers.update({"User-Agent": "zai-multi-account/1.0"})

    def create_account(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def wait_for_verification_link(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def cleanup(self) -> None:
        pass

    # Shared polling helper used by every provider.
    def _poll_for_link(self, fetch_messages) -> Optional[str]:
        """
        ``fetch_messages`` returns a list of dicts:
            {id, from, subject, body}
        ``body`` may be plain text or HTML.
        """
        deadline = time.time() + self.timeout
        seen: Set[str] = set()
        while time.time() < deadline:
            try:
                for msg in fetch_messages() or []:
                    mid = msg.get("id")
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)

                    if not is_relevant_message(
                        msg.get("from", ""), msg.get("subject", ""), msg.get("body", "")
                    ):
                        continue

                    link = pick_verification_link(msg.get("body", ""))
                    if link:
                        return link
            except Exception as exc:
                console.print(f"[yellow][{self.name}] poll error (retry): {exc}[/yellow]")
            time.sleep(self.poll_interval)
        return None


# ------------------------------------------------------------
# mail.tm
# ------------------------------------------------------------

class MailTmProvider(TempMailProvider):
    name = "mail.tm"
    base = "https://api.mail.tm"

    def __init__(self, timeout: int, poll_interval: int):
        super().__init__(timeout, poll_interval)
        self.password: Optional[str] = None
        self.token: Optional[str] = None
        self._account_id: Optional[str] = None

    def _req(self, method: str, endpoint: str, **kwargs) -> dict:
        url = f"{self.base}{endpoint}"
        resp = self._session.request(method, url, timeout=15, **kwargs)
        if resp.status_code == 204:
            return {}
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def create_account(self) -> str:
        domains = self._req("GET", "/domains").get("hydra:member", [])
        if not domains:
            raise RuntimeError("mail.tm returned no domains")
        domain = domains[0]["domain"]
        username = f"zai{random.randint(100000, 999999)}"
        self.address = f"{username}@{domain}"
        self.password = f"ZaiTemp!{random.randint(10000, 99999)}"

        created = self._req(
            "POST", "/accounts",
            json={"address": self.address, "password": self.password},
            headers={"Content-Type": "application/json"},
        )
        self._account_id = created.get("id") or self.address
        token_resp = self._req(
            "POST", "/token",
            json={"address": self.address, "password": self.password},
            headers={"Content-Type": "application/json"},
        )
        self.token = token_resp.get("token")
        if not self.token:
            raise RuntimeError("mail.tm: failed to obtain auth token")
        return self.address

    def _fetch_messages(self) -> List[Dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        msgs_resp = self._req("GET", "/messages", headers=headers)
        if isinstance(msgs_resp, list):
            messages = msgs_resp
        else:
            messages = (msgs_resp or {}).get("hydra:member", []) if isinstance(msgs_resp, dict) else []

        out: List[Dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            mid = msg.get("id")
            from_addr = ""
            from_data = msg.get("from") or {}
            if isinstance(from_data, dict):
                from_addr = (from_data.get("address") or "").lower()
            # Fetch full body.
            try:
                full = self._req("GET", f"/messages/{mid}", headers=headers)
                if isinstance(full, list):
                    full = full[0] if full else {}
                if not isinstance(full, dict):
                    full = {}
            except Exception:
                full = {}

            html = full.get("html")
            if html:
                body = html[0] if isinstance(html, list) else html
            else:
                body = full.get("text", "") or ""
            out.append({
                "id": mid,
                "from": from_addr,
                "subject": msg.get("subject", ""),
                "body": body,
            })
        return out

    def wait_for_verification_link(self) -> str:
        if not self.token:
            raise RuntimeError("mail.tm: no token (create_account first)")
        link = self._poll_for_link(self._fetch_messages)
        if not link:
            raise TimeoutError(f"{self.name}: no verification link within {self.timeout}s")
        return link

    def cleanup(self) -> None:
        if self._account_id and self.token:
            try:
                self._req("DELETE", f"/accounts/{self._account_id}",
                          headers={"Authorization": f"Bearer {self.token}"})
            except Exception:
                pass
        self.address = self.password = self.token = self._account_id = None


# ------------------------------------------------------------
# 1secmail  (stateless, simple)
# ------------------------------------------------------------

class OneSecMailProvider(TempMailProvider):
    name = "1secmail"
    base = "https://www.1secmail.com/api/v1"

    def __init__(self, timeout: int, poll_interval: int):
        super().__init__(timeout, poll_interval)

    def create_account(self) -> str:
        # 1secmail does not require explicit signup; pick a random mailbox.
        domains = self._session.get(f"{self.base}/?action=getDomainList", timeout=15).json()
        if not domains:
            raise RuntimeError("1secmail returned no domains")
        domain = random.choice(domains)
        login = f"zai{random.randint(100000, 999999)}"
        self.address = f"{login}@{domain}"
        return self.address

    def _fetch_messages(self) -> List[Dict[str, Any]]:
        if not self.address:
            return []
        login, _, domain = self.address.partition("@")
        msgs = self._session.get(
            f"{self.base}/?action=getMessages",
            params={"login": login, "domain": domain},
            timeout=15,
        ).json()
        if not isinstance(msgs, list):
            return []

        out: List[Dict[str, Any]] = []
        for msg in msgs:
            mid = msg.get("id")
            from_addr = (msg.get("from") or "").lower()
            try:
                full = self._session.get(
                    f"{self.base}/?action=readMessage",
                    params={"login": login, "domain": domain, "id": mid},
                    timeout=15,
                ).json()
            except Exception:
                full = {}
            body = full.get("htmlBody") or full.get("textBody") or ""
            out.append({
                "id": f"{domain}:{mid}",
                "from": from_addr,
                "subject": msg.get("subject", ""),
                "body": body,
            })
        return out

    def wait_for_verification_link(self) -> str:
        if not self.address:
            raise RuntimeError("1secmail: no mailbox (create_account first)")
        link = self._poll_for_link(self._fetch_messages)
        if not link:
            raise TimeoutError(f"{self.name}: no verification link within {self.timeout}s")
        return link


# ------------------------------------------------------------
# Guerrilla Mail  (session-based via sid_token)
# ------------------------------------------------------------

class GuerrillaMailProvider(TempMailProvider):
    name = "guerrillamail"
    base = "https://api.guerrillamail.com/ajax.php"

    def __init__(self, timeout: int, poll_interval: int):
        super().__init__(timeout, poll_interval)
        self.sid_token: Optional[str] = None
        self._seq: int = 0

    def _api(self, action: str, extra: Optional[Dict[str, Any]] = None) -> dict:
        params: Dict[str, Any] = {"f": action}
        if self.sid_token:
            params["ip"] = "127.0.0.1"
            params["sid_token"] = self.sid_token
        if extra:
            params.update(extra)
        resp = self._session.get(self.base, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def create_account(self) -> str:
        # Pick a random alias to lower address reuse risk.
        rand_user = f"zai{random.randint(100000, 999999)}"
        data = self._api("get_email_address", {"lang": "en"})
        self.sid_token = data.get("sid_token") or self.sid_token
        # Try to set custom username; if it fails, fall back to default.
        try:
            data = self._api("set_email_user", {"email_user": rand_user})
            self.sid_token = data.get("sid_token") or self.sid_token
        except Exception:
            pass
        self.address = data.get("email_addr") or ""
        if not self.address:
            raise RuntimeError("guerrillamail: failed to obtain mailbox")
        return self.address

    def _fetch_messages(self) -> List[Dict[str, Any]]:
        if not self.sid_token:
            return []
        data = self._api("check_email", {"seq": self._seq})
        self._seq = int(data.get("seq", self._seq))
        out: List[Dict[str, Any]] = []
        for msg in data.get("list", []) or []:
            mid = msg.get("mail_id")
            from_addr = (msg.get("mail_from") or "").lower()
            # Body lives in 'mail_body' once fetched individually.
            body = msg.get("mail_excerpt") or ""
            try:
                full = self._api("fetch_email", {"email_uid": mid})
                body = full.get("mail_body") or body
            except Exception:
                pass
            out.append({
                "id": f"{self.sid_token}:{mid}",
                "from": from_addr,
                "subject": msg.get("mail_subject", ""),
                "body": body,
            })
        return out

    def wait_for_verification_link(self) -> str:
        if not self.sid_token or not self.address:
            raise RuntimeError("guerrillamail: no mailbox (create_account first)")
        link = self._poll_for_link(self._fetch_messages)
        if not link:
            raise TimeoutError(f"{self.name}: no verification link within {self.timeout}s")
        return link

    def cleanup(self) -> None:
        if self.sid_token:
            try:
                self._api("forget_me", {"email_addr": self.address or ""})
            except Exception:
                pass
        self.address = self.sid_token = None


# ------------------------------------------------------------
# Provider registry
# ------------------------------------------------------------

_PROVIDERS = {
    "mail.tm": MailTmProvider,
    "1secmail": OneSecMailProvider,
    "guerrillamail": GuerrillaMailProvider,
}


def get_provider_class(name: str):
    key = (name or "").strip().lower()
    return _PROVIDERS.get(key)


def list_available_providers() -> List[str]:
    return list(_PROVIDERS.keys())


# ------------------------------------------------------------
# Backward-compatible manager with fallback orchestration
# ------------------------------------------------------------

class TempMailManager:
    """
    Backward-compatible temp-mail manager.

    Maintains the original public surface (``create_account``,
    ``wait_for_verification_link``, ``cleanup``) but internally tries
    multiple providers in order, switching on failure.

    Failures handled:
      - ``create_account`` raises / returns None -> try next provider.
      - ``wait_for_verification_link`` raises TimeoutError -> try next provider
        (re-creating the mailbox because the previous address is now useless).
      - All providers exhausted -> raise.

    The current active provider is exposed as ``.active_provider`` and the
    raw provider object as ``.provider`` for callers that need provider-
    specific behaviour.
    """

    def __init__(
        self,
        timeout: Optional[int] = None,
        poll_interval: Optional[int] = None,
        providers: Optional[List[str]] = None,
        max_retries_per_provider: Optional[int] = None,
    ):
        self.timeout = int(timeout if timeout is not None
                           else config.get("temp_mail_timeout_sec", 240))
        self.poll_interval = int(poll_interval if poll_interval is not None
                                 else config.get("temp_mail_poll_interval_sec", 5))
        self.provider_order: List[str] = list(
            providers or config.get("temp_mail_providers", ["mail.tm"])
        )
        self.max_retries_per_provider = int(
            max_retries_per_provider if max_retries_per_provider is not None
            else config.get("temp_mail_max_retries_per_provider", 2)
        )
        self.provider: Optional[TempMailProvider] = None
        self.active_provider: Optional[str] = None
        self.address: Optional[str] = None
        # History of attempts - useful for debugging / logging.
        self.attempts: List[Dict[str, str]] = []

    # The original implementation exposed ``.address`` etc. Keep parity.
    @property
    def token(self) -> Optional[str]:
        return getattr(self.provider, "token", None)

    @property
    def password(self) -> Optional[str]:
        return getattr(self.provider, "password", None)

    def _make_provider(self, name: str) -> TempMailProvider:
        cls = get_provider_class(name)
        if not cls:
            raise RuntimeError(f"Unknown temp-mail provider: {name}")
        return cls(timeout=self.timeout, poll_interval=self.poll_interval)

    def create_account(self) -> str:
        """
        Create a mailbox on the first provider that succeeds.
        Stores the active provider + address and returns the email address.
        """
        last_exc: Optional[Exception] = None
        for name in self.provider_order:
            for attempt in range(1, self.max_retries_per_provider + 1):
                try:
                    console.print(f"[cyan]Temp mail: trying provider '{name}' (attempt {attempt})[/cyan]")
                    prov = self._make_provider(name)
                    addr = prov.create_account()
                    self.provider = prov
                    self.active_provider = name
                    self.address = addr
                    console.print(f"[green][OK] Mailbox ready on {name}: {addr}[/green]")
                    return addr
                except Exception as exc:
                    last_exc = exc
                    self.attempts.append({"provider": name, "phase": "create", "error": str(exc)})
                    console.print(f"[yellow]Provider '{name}' create failed: {exc}[/yellow]")
                    break  # move to next provider rather than hammering the same one
        raise RuntimeError(
            f"All temp-mail providers failed to create a mailbox. "
            f"Last error: {last_exc}"
        )

    def wait_for_verification_link(self) -> str:
        """
        Poll the active provider for a verification link.
        On timeout, restart with the next provider (new mailbox).
        """
        if not self.provider:
            raise RuntimeError("No active provider (call create_account first)")

        providers_to_try = [self.active_provider] + [
            p for p in self.provider_order if p != self.active_provider
        ]

        last_exc: Optional[Exception] = None
        for idx, name in enumerate(providers_to_try):
            try:
                if idx == 0:
                    prov = self.provider
                else:
                    console.print(f"[cyan]Falling back to provider '{name}' (new mailbox)[/cyan]")
                    prov = self._make_provider(name)
                    self.address = prov.create_account()
                    self.provider = prov
                    self.active_provider = name
                    # IMPORTANT: caller must re-submit signup with this new address.
                    # We surface it via the NewMailboxAfterFallback exception below
                    # so the orchestrator can re-drive the signup form.
                    raise NewMailboxAfterFallback(self.address)

                link = prov.wait_for_verification_link()
                console.print(f"[green][OK] Verification link found via {name}[/green]")
                return link

            except NewMailboxAfterFallback:
                raise  # propagate to caller (signup needs new email)

            except TimeoutError as exc:
                last_exc = exc
                self.attempts.append({"provider": name, "phase": "poll", "error": str(exc)})
                console.print(f"[yellow]Provider '{name}' timed out: {exc}[/yellow]")
                continue
            except Exception as exc:
                last_exc = exc
                self.attempts.append({"provider": name, "phase": "poll", "error": str(exc)})
                console.print(f"[yellow]Provider '{name}' poll failed: {exc}[/yellow]")
                continue

        raise TimeoutError(
            f"No verification link from any provider within timeout. Last error: {last_exc}"
        )

    def cleanup(self) -> None:
        if self.provider:
            try:
                self.provider.cleanup()
            except Exception:
                pass
        self.provider = self.active_provider = self.address = None


class NewMailboxAfterFallback(RuntimeError):
    """
    Raised by ``TempMailManager.wait_for_verification_link`` when a fallback
    provider created a *new* mailbox address. The signup flow must re-submit
    the registration form using ``manager.address`` before we can wait again.
    """

    def __init__(self, new_address: str):
        super().__init__(f"New mailbox obtained after fallback: {new_address}")
        self.new_address = new_address


__all__ = [
    "TempMailManager",
    "TempMailProvider",
    "MailTmProvider",
    "OneSecMailProvider",
    "GuerrillaMailProvider",
    "NewMailboxAfterFallback",
    "get_provider_class",
    "list_available_providers",
    "is_relevant_message",
    "pick_verification_link",
]
