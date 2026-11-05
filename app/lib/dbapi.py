"""A small Databricks REST client on the standard library (no SDK, no pip: it must run air-gapped).

Two identities:
  viewer  the signed-in user's on-behalf-of token (x-forwarded-access-token). Used for SQL, so each
          viewer sees only the system tables THEY may read.
  app     the app's own service principal (OAuth client credentials). Used for the Clusters and Events
          APIs, because Databricks Apps has no user-authorization scope for them.
"""
import base64
import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


class ApiError(Exception):
    def __init__(self, status, code, message, path=""):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.path = status, code or "", message or "", path

    def kind(self):
        """Name the failure so the UI can say what to do about it."""
        m = (self.message or "").lower()
        if "required scopes" in m or "does not have required scope" in m:
            return "scope"
        if self.status == 401:
            return "unauthenticated"
        if self.status == 403 or "permission" in m or "not authorized" in m or "insufficient" in m:
            return "permission"
        if self.status == 404:
            return "not_found"
        if self.status == 429:
            return "rate_limited"
        return "error"

    def public(self):
        return {"status": self.status, "code": self.code, "kind": self.kind(), "message": self.message[:400]}


class Client:
    def __init__(self, token_fn, host=None, label=""):
        self._token_fn = token_fn
        self.host = (host or config.HOST).rstrip("/")
        self.label = label

    def request(self, method, path, body=None, query=None, timeout=60):
        if not self.host:
            raise ApiError(0, "NO_HOST", "DATABRICKS_HOST is not set", path)
        tok = self._token_fn()
        if not tok:
            raise ApiError(401, "NO_TOKEN", f"no {self.label or 'API'} token available", path)
        url = self.host + path
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                try:
                    j = json.loads(raw)
                except ValueError:
                    j = {"message": raw[:300]}
                err = ApiError(e.code, j.get("error_code"), j.get("message") or j.get("error") or raw[:300], path)
                if e.code in (429, 503) and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise err
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(1.0)
                    continue
                raise ApiError(0, "NETWORK", str(e.reason)[:300], path)
            except (http.client.IncompleteRead, ConnectionError, TimeoutError) as e:
                # a cut-off response: safe to retry a GET; a POST could have run, so only GETs retry
                if method == "GET" and attempt < 2:
                    time.sleep(1.0)
                    continue
                raise ApiError(0, "NETWORK", f"{type(e).__name__}: {str(e)[:200]}", path)

    def get(self, path, query=None, timeout=60):
        return self.request("GET", path, query=query, timeout=timeout)

    def post(self, path, body=None, timeout=60):
        return self.request("POST", path, body=body or {}, timeout=timeout)


# ── the app's own identity ───────────────────────────────────────────────────────────────────────────
_sp = {"token": None, "exp": 0.0}
_sp_lock = threading.Lock()


def sp_token():
    if config.DEV_TOKEN and not config.CLIENT_ID:
        return config.DEV_TOKEN
    if not (config.CLIENT_ID and config.CLIENT_SECRET and config.HOST):
        return None
    with _sp_lock:
        if _sp["token"] and time.time() < _sp["exp"] - 120:
            return _sp["token"]
        auth = base64.b64encode(f"{config.CLIENT_ID}:{config.CLIENT_SECRET}".encode()).decode()
        req = urllib.request.Request(config.HOST + "/oidc/v1/token", method="POST",
                                     data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "all-apis"}).encode(),
                                     headers={"Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read())
        _sp["token"], _sp["exp"] = j["access_token"], time.time() + float(j.get("expires_in", 3600))
        return _sp["token"]


def app_client():
    return Client(sp_token, label="app service principal")


def viewer_client(headers):
    tok = headers.get("x-forwarded-access-token") or (config.DEV_TOKEN or None)
    return Client(lambda: tok, label="viewer")


def token_claims(tok):
    """Decode a JWT payload for diagnostics (scope, exp). No signature check: it is never trusted for access."""
    try:
        part = tok.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


# ── SQL through the Statement Execution API ──────────────────────────────────────────────────────────
_NUM = {"INT", "LONG", "BIGINT", "SHORT", "BYTE", "TINYINT", "SMALLINT", "INTEGER"}
_FLOAT = {"DOUBLE", "FLOAT", "DECIMAL", "REAL"}


def _convert(v, t):
    if v is None:
        return None
    if t in _NUM:
        try:
            return int(v)
        except ValueError:
            return float(v)
    if t in _FLOAT:
        return float(v)
    if t == "BOOLEAN":
        return v in (True, "true", "True")
    return v


def sql(client, warehouse_id, statement, params=None, timeout_s=110):
    """Run one statement as the client's identity and return a list of dicts. Raises ApiError on failure."""
    body = {"warehouse_id": warehouse_id, "statement": statement, "wait_timeout": "30s",
            "on_wait_timeout": "CONTINUE", "disposition": "INLINE", "format": "JSON_ARRAY"}
    if params:
        body["parameters"] = [{"name": k, "value": None if v is None else str(v)} for k, v in params.items()]
    r = client.post("/api/2.0/sql/statements", body, timeout=60)
    t0 = time.time()
    while (r.get("status") or {}).get("state") in ("PENDING", "RUNNING"):
        if time.time() - t0 > timeout_s:
            try:
                client.post(f"/api/2.0/sql/statements/{r['statement_id']}/cancel")
            except ApiError:
                pass
            raise ApiError(504, "TIMEOUT", f"statement still running after {timeout_s}s")
        time.sleep(1.0)
        r = client.get(f"/api/2.0/sql/statements/{r['statement_id']}")
    st = r.get("status") or {}
    if st.get("state") != "SUCCEEDED":
        e = st.get("error") or {}
        raise ApiError(400, e.get("error_code") or st.get("state"), e.get("message") or str(st)[:300], "sql")
    cols = [(c["name"], (c.get("type_name") or "").upper()) for c in r["manifest"]["schema"]["columns"]]
    rows = []
    res = r.get("result") or {}
    while True:
        for row in res.get("data_array") or []:
            rows.append({n: _convert(v, t) for (n, t), v in zip(cols, row)})
        nxt = res.get("next_chunk_internal_link")
        if not nxt:
            break
        res = client.get(nxt)
    return rows
