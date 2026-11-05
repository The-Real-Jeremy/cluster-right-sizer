"""The viewer's own personal access token, held only in their browser, for the Clusters and Events APIs.

Why a PAT at all: Databricks Apps has no user-authorization (on-behalf-of) scope for the Clusters API, so the
forwarded user token cannot call it. Each viewer enters their own PAT once. The back end checks it belongs to
the signed-in viewer, seals it, and returns it as an HttpOnly, Secure, SameSite=Strict cookie on the app's path.
Page JavaScript cannot read it. Nothing about it is kept in the container, the environment, logs or tables:
on each request that needs it, the cookie is unsealed in memory and used only for clusters/list, clusters/get,
clusters/events, list-node-types and SCIM Me. SQL never uses it (that runs on the on-behalf-of token).

Sealing uses the standard library only (no `cryptography` wheel, so the app stays air-gap friendly):
encrypt-then-MAC with HMAC-SHA256 as both the keystream PRF (counter mode) and the 256-bit tag. The keys are
derived from the app's own client secret, which never leaves the container, so a copied cookie is useless
outside this app. Standalone, a saved token survives a redeploy but not an app Stop and Start; inside
App-ception the key is per process (see key_mode).
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from . import config, dbapi

COOKIE = "crs_pat"
MAX_AGE_S = 30 * 24 * 3600      # a forgotten browser does not hold a PAT for ever
CHECK_TTL_S = 300               # re-check a PAT with Databricks at most every 5 minutes
_VERSION = b"crs-pat-v1"
_keys = None
_checked = {}                   # sha256(pat) -> (time confirmed, user name). Never the PAT itself.


def _child_name():
    """Set by App-ception for every child process (both the new and the old variable names)."""
    return os.environ.get("APPCEPTION_CHILD") or os.environ.get("CATALOG_CHILD") or ""


def key_mode():
    """memory  : a random key that only this process holds; saved tokens must be re-entered after a restart.
       derived : derived from the app's own client secret: a cookie survives a redeploy, not an app Stop and Start.
    As an App-ception child every sibling inherits the SAME client secret and app name, so a derived key would let any
    sibling child unseal this cookie. Children therefore always use 'memory'. Standalone defaults to 'derived'
    (the secret belongs to this app alone); CRS_PAT_KEY=memory forces 'memory' there too."""
    if _child_name():
        return "memory"
    return "memory" if os.environ.get("CRS_PAT_KEY", "").strip().lower() == "memory" else "derived"


def _derive():
    global _keys
    if _keys is None:
        secret = (config.CLIENT_SECRET or "").encode()
        if key_mode() == "memory" or not secret:
            secret = secrets.token_bytes(32)        # never written anywhere; gone when the process exits
        info = (os.environ.get("DATABRICKS_APP_NAME") or "cluster-right-sizer").encode()
        prk = hmac.new(_VERSION, secret, hashlib.sha256).digest()           # HKDF-extract
        enc = hmac.new(prk, info + b"|enc\x01", hashlib.sha256).digest()    # HKDF-expand, one block each
        mac = hmac.new(prk, info + b"|mac\x01", hashlib.sha256).digest()
        _keys = (enc, mac)
    return _keys


def _stream(key, nonce, n):
    out, ctr = bytearray(), 0
    while len(out) < n:
        out += hmac.new(key, nonce + ctr.to_bytes(8, "big"), hashlib.sha256).digest()
        ctr += 1
    return bytes(out[:n])


def seal(pat, email):
    enc, mac = _derive()
    nonce = secrets.token_bytes(16)
    blob = json.dumps({"t": pat, "e": (email or "").lower(), "iat": int(time.time())}).encode()
    ct = bytes(a ^ b for a, b in zip(blob, _stream(enc, nonce, len(blob))))
    tag = hmac.new(mac, _VERSION + nonce + ct, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + ct + tag).decode().rstrip("=")


def unseal(value):
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
        enc, mac = _derive()
        if not hmac.compare_digest(tag, hmac.new(mac, _VERSION + nonce + ct, hashlib.sha256).digest()):
            return None
        data = json.loads(bytes(a ^ b for a, b in zip(ct, _stream(enc, nonce, len(ct)))))
        if time.time() - data.get("iat", 0) > MAX_AGE_S:
            return None
        return data
    except Exception:   # tampered, from another app, or sealed under a key that has since changed
        return None


def cookie_path():
    """Standalone the app owns the origin ("/"). As a child, scope the cookie to the child's own prefix ourselves:
    App-ception rewrites a Set-Cookie Path only in prefix mount mode, so under a root mount Path=/ would reach every child."""
    base = (os.environ.get("APPCEPTION_BASE_PATH") or os.environ.get("CATALOG_BASE_PATH") or "").rstrip("/")
    return (base + "/") if _child_name() and base.startswith("/") else "/"


def cookie_header(value, max_age):
    # SameSite=Strict: never sent on a cross-site request. HttpOnly: page scripts (ours or a sibling's) cannot read it.
    return f"{COOKIE}={value}; Max-Age={max_age}; Path={cookie_path()}; HttpOnly; Secure; SameSite=Strict"


def clear_header():
    return cookie_header("", 0)


def read_cookie(cookie_header_value):
    for part in (cookie_header_value or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return None


def pat_user(pat):
    """The PAT's own user name, or raises ApiError (401/403 when it is bad)."""
    me = dbapi.Client(lambda: pat, label="personal access token").get("/api/2.0/preview/scim/v2/Me", timeout=20)
    return (me.get("userName") or "").lower()


class PatState:
    """What the request knows about the viewer's PAT: state is none | ok | wrong_user | expired | error."""
    def __init__(self, state, message="", pat=None, user=None, clear=False):
        self.state, self.message, self.pat, self.user, self.clear = state, message, pat, user, clear

    def public(self):
        return {"state": self.state, "message": self.message, "user": self.user,
                "max_age_days": MAX_AGE_S // 86400, "key_mode": key_mode()}


def from_request(headers):
    viewer = (headers.get("x-forwarded-email") or "").lower()
    if headers.get("x-crs-as-app") == "1" and config.SP_DIAGNOSTICS:
        return PatState("ok", "", pat=headers.get("x-forwarded-access-token"), user=viewer)
    sealed = read_cookie(headers.get("cookie"))
    if not sealed and config.DEV_TOKEN and not config.CLIENT_ID and os.environ.get("CRS_DEV_TOKEN_AS_PAT") == "1":
        return PatState("ok", "", pat=config.DEV_TOKEN, user="(laptop dev token)")   # laptop only: never set in an app
    if not sealed:
        return PatState("none", "Enter your personal access token to see your clusters and their events.")
    data = unseal(sealed)
    if not data or not data.get("t"):
        why = ("The app restarted, and saved tokens are sealed with a key that lives only in its memory (by design). Enter it again."
               if key_mode() == "memory" else "Your saved token could not be read: the app was stopped and started since you saved it. Enter it again.")
        return PatState("none", why, clear=True)
    if viewer and data.get("e") != viewer:
        return PatState("wrong_user", "The saved token belongs to another user. Enter your own personal access token.", clear=True)
    pat = data["t"]
    h = hashlib.sha256(pat.encode()).hexdigest()
    hit = _checked.get(h)
    if not hit or time.time() - hit[0] > CHECK_TTL_S:
        try:
            user = pat_user(pat)
        except dbapi.ApiError as e:
            _checked.pop(h, None)
            if e.status in (401, 403):
                return PatState("expired", "Your personal access token has expired or been revoked. Enter a new one.", clear=True)
            return PatState("error", f"Could not check your token with the workspace ({e.kind()}). Try again.")
        _checked[h] = (time.time(), user)
        hit = _checked[h]
    return PatState("ok", "", pat=pat, user=hit[1])


def save(headers, pat):
    """Validate and seal. Returns (http status, body, set-cookie header or None)."""
    viewer = (headers.get("x-forwarded-email") or "").lower()
    pat = (pat or "").strip()
    if not pat:
        return 400, {"error": "Paste a personal access token."}, None
    if len(pat) > 4096 or any(c.isspace() for c in pat):
        return 400, {"error": "That does not look like a personal access token."}, None
    try:
        owner = pat_user(pat)
    except dbapi.ApiError as e:
        if e.status in (401, 403):
            return 400, {"error": "Databricks rejected that token: it is mistyped, expired or revoked."}, None
        return 502, {"error": "Could not reach the workspace to check the token. Try again."}, None
    if viewer and owner != viewer:
        return 403, {"error": f"That token belongs to {owner}, but you are signed in as {viewer}. "
                              "Use a token you created yourself (Settings → Developer → Access tokens)."}, None
    _checked[hashlib.sha256(pat.encode()).hexdigest()] = (time.time(), owner)
    return 200, {"ok": True, "user": owner, "max_age_days": MAX_AGE_S // 86400}, cookie_header(seal(pat, owner), MAX_AGE_S)
