"""Cluster Right-Sizer — a read-only Databricks App on the Python standard library.

Routes (all relative, so it works at "/" standalone and at "/apps/<name>/" inside App-ception):
  /                   the page
  /static/...         its assets (no CDN, no web font from the internet)
  /api/health         build id + a fingerprint of the served code
  /api/overview       fleet view: access, KPIs, one row per all-purpose cluster
  /api/cluster        one cluster: worker timeline, utilisation, heatmap, nodes, users, recommendations
  /api/access         what the viewer can read, re-probed (?refresh=1)
  /api/whoami         the viewer as the app sees them, and the scopes on the forwarded token (never the token)
  /api/genie          GET state (+ questions for ?name=&id=) · POST ask {question, conversation_id} · GET ?cid=&mid= poll
  /api/pat            GET status · POST save {pat} · DELETE forget  (the viewer's own PAT, sealed in a cookie)
"""
import hashlib
import json
import mimetypes
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lib import access, config, dbapi, genie, pat, service  # noqa: E402

STATIC = os.path.join(HERE, "static")


def _fingerprint():
    h = hashlib.md5()
    for root, dirs, files in os.walk(HERE):
        # the Apps container keeps its pre-installed libraries in <src>/.venv: not our code
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "node_modules"))
        for f in sorted(files):
            if f.endswith((".py", ".js", ".css", ".html", ".svg")):
                with open(os.path.join(root, f), "rb") as fh:
                    h.update(fh.read())
    return h.hexdigest()


SOURCE_MD5 = _fingerprint()
STARTED = time.time()


class Handler(BaseHTTPRequestHandler):
    server_version = "cluster-right-sizer"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    def _headers(self):
        return {k.lower(): v for k, v in self.headers.items()}

    def _send(self, code, body, ctype="application/json", cache="no-store", cookie=None):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, {k: v[-1] for k, v in parse_qs(u.query).items()}
        # Inside App-ception the proxy strips the prefix; tolerate it anyway.
        pre = self.headers.get("x-forwarded-prefix") or ""
        if pre and path.startswith(pre):
            path = path[len(pre):] or "/"
        try:
            if path in ("/", "/index.html"):
                return self._index()
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/health":
                return self._send(200, {"ok": True, "build": config.BUILD_ID, "source_md5": SOURCE_MD5,
                                        "uptime_s": round(time.time() - STARTED), "demo": config.DEMO})
            demo = q.get("demo") == "1" and config.DEMO != "off"
            hdrs = self._headers()
            if q.get("as") == "app" and config.SP_DIAGNOSTICS:
                hdrs = service.as_app(hdrs)
            if q.get("simulate") == "no_system_tables" and config.SP_DIAGNOSTICS:
                hdrs = dict(hdrs, **{"x-crs-simulate": "no_system_tables"})
            self._headers = lambda: hdrs
            days = int(q.get("days") or config.LOOKBACK_DAYS)
            if path == "/api/overview":
                ov = service.overview(self._headers(), days, demo, q.get("refresh") == "1")
                return self._send(200, ov, cookie=pat.clear_header() if (ov.get("phase_a") or {}).get("clear_cookie") else None)
            if path == "/api/pat":
                ps = pat.from_request(self._headers())
                return self._send(200, ps.public(), cookie=pat.clear_header() if ps.clear else None)
            if path == "/api/cluster":
                d = service.cluster_detail(self._headers(), q.get("key", ""), days, demo, "audit" if q.get("part") == "audit" else "core")
                return self._send(200 if d else 404, d or {"error": "no such cluster in this view"})
            if path == "/api/access":
                h = self._headers()
                ident = service.viewer_identity(h)
                return self._send(200, access.probe(dbapi.viewer_client(h), ident["email"] or ident["user"], force=q.get("refresh") == "1"))
            if path == "/api/genie":
                ident = service.viewer_identity(hdrs)
                v = dbapi.viewer_client(hdrs)
                if q.get("cid") and q.get("mid"):
                    return self._send(200, genie.poll(v, q["cid"], q["mid"]))
                st = genie.probe(v, ident["email"] or ident["user"])
                st["questions"] = genie.questions_for(q.get("name"), q.get("id")) if q.get("id") else []
                return self._send(200, st)
            if path == "/api/diag/clusters" and config.SP_DIAGNOSTICS:
                # what the identity in use can do with each named cluster: get + events status codes
                ps = pat.from_request(hdrs)
                c = dbapi.Client(lambda: ps.pat, label="token")
                out = {"identity": service.viewer_identity(hdrs), "pat": ps.public(), "clusters": []}
                for cid in [x for x in q.get("ids", "").split(",") if x][:20]:
                    row = {"cluster_id": cid}
                    for name, fn in (("get", lambda: c.get("/api/2.1/clusters/get", query={"cluster_id": cid})),
                                     ("events", lambda: c.post("/api/2.1/clusters/events", {"cluster_id": cid, "page_size": 5}))):
                        try:
                            r = fn(); row[name] = {"status": 200, "n": len(r.get("events", [])) if name == "events" else r.get("state")}
                        except dbapi.ApiError as e:
                            row[name] = {"status": e.status, "code": e.code, "message": e.message[:160]}
                    out["clusters"].append(row)
                return self._send(200, out)
            if path == "/api/whoami":
                h = self._headers()
                tok = h.get("x-forwarded-access-token") or ""
                c = dbapi.token_claims(tok) if tok else {}
                return self._send(200, {"viewer": service.viewer_identity(h), "token_scopes": (c.get("scope") or "").split(),
                                        "token_expires_in_s": round(c["exp"] - time.time()) if c.get("exp") else None,
                                        "app_identity": config.describe()["app_identity"]})
            return self._send(404, {"error": "not found", "path": path})
        except dbapi.ApiError as e:
            return self._send(502, {"error": e.public()})
        except Exception as e:  # never a bare 500 page: the UI shows this
            traceback.print_exc()
            return self._send(500, {"error": {"kind": "error", "message": f"{type(e).__name__}: {e}"[:400]}})

    def _write_guard(self):
        """SameSite=Strict already blocks cross-site sends; also require a custom header a form cannot set."""
        return self.headers.get("x-crs") == "1" and "json" in (self.headers.get("content-type") or "")

    def do_POST(self):
        path = urlparse(self.path).path
        pre = self.headers.get("x-forwarded-prefix") or ""
        if pre and path.startswith(pre):
            path = path[len(pre):] or "/"
        if path not in ("/api/pat", "/api/genie"):
            return self._send(404, {"error": "not found"})
        if not self._write_guard():
            return self._send(403, {"error": "missing request header"})
        try:
            n = min(int(self.headers.get("content-length") or 0), 8192)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "bad JSON"})
        if path == "/api/genie":
            if not config.GENIE_SPACE_ID:
                return self._send(404, {"error": "No Genie space is configured."})
            try:
                return self._send(200, genie.ask(dbapi.viewer_client(self._headers()), body.get("question"), body.get("conversation_id")))
            except dbapi.ApiError as e:
                return self._send(e.status if 400 <= e.status < 600 else 502, {"error": e.message[:300], "kind": e.kind()})
        code, out, cookie = pat.save(self._headers(), body.get("pat"))
        service.forget_cache()
        return self._send(code, out, cookie=cookie)

    def do_DELETE(self):
        path = urlparse(self.path).path
        pre = self.headers.get("x-forwarded-prefix") or ""
        if pre and path.startswith(pre):
            path = path[len(pre):] or "/"
        if path != "/api/pat":
            return self._send(404, {"error": "not found"})
        if self.headers.get("x-crs") != "1":
            return self._send(403, {"error": "missing request header"})
        service.forget_cache()
        return self._send(200, {"ok": True}, cookie=pat.clear_header())

    def _index(self):
        with open(os.path.join(STATIC, "index.html"), "rb") as f:
            html = f.read().replace(b"{{V}}", SOURCE_MD5[:10].encode()).replace(b"{{DEMO}}", config.DEMO.encode())
        self._send(200, html, "text/html; charset=utf-8")

    def _static(self, rel):
        p = os.path.normpath(os.path.join(STATIC, rel))
        if not p.startswith(STATIC + os.sep) or not os.path.isfile(p):
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        if p.endswith(".woff2"):
            ctype = "font/woff2"
        with open(p, "rb") as f:
            self._send(200, f.read(), ctype, "public, max-age=3600")


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    srv.daemon_threads = True
    print(f"cluster-right-sizer on :{config.PORT} build={config.BUILD_ID} md5={SOURCE_MD5[:10]} demo={config.DEMO}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
