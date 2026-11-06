#!/usr/bin/env python3
"""bridge.py - live Grok Bot -> Hermes chat bridge (request/response over Tailscale).

Usage:
  bridge.py [--config PATH] ask <hermes-slug> "message" --from grok-<name> [--thread ID] [--timeout S] [--json]
  bridge.py [--config PATH] tasks <hermes-slug> --from grok-<name> --thread ID [--json]
  bridge.py [--config PATH] task <hermes-slug> <task-id> --from grok-<name> [--json]
  bridge.py [--config PATH] list [--auth]
  bridge.py [--config PATH] show-config

Config: --config PATH, else $GROK_BRIDGE_CONFIG, else ./bridge.json, else bridge.json next to this script.
Tokens: the config names env vars (token_env); values come from the environment or from the
optional "env_file" (KEY=VALUE lines). Token values are never printed or logged.
Transport: "proxy" may be "", "http://host:port" (HTTP proxy, e.g. tailscaled --outbound-http-proxy-listen)
or "socks5h://host:port" (SOCKS5, e.g. tailscaled --socks5-server). Stdlib only; no extra packages.
Protocols per agent: "a2a" (DEFAULT: Hermes A2A platform plugin, Agent2Agent v1.0; Agent Card at
/.well-known/agent-card.json, JSON-RPC SendMessage at POST /, contextId = --thread) or "openai"
(Hermes API server, /v1/chat/completions + X-Hermes-Session-Id). An agent may name a "fallback"
endpoint; it is used only when the primary was never reached (refused/404/502/503), so a task
never runs twice.
Exit codes: 0 ok, 1 remote/transport error, 2 usage/config error, 3 rate limited.
"""
import argparse
import concurrent.futures
import datetime as _dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import struct
import sys
import time
import urllib.parse
import uuid

VERSION = "1.2.0"
SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
MAX_CONTEXT_FILE_BYTES = 64 * 1024


# ---------------------------------------------------------------- config
def _expand(p):
    return os.path.expandvars(os.path.expanduser(p)) if p else p


def find_config(cli_path):
    cands = [cli_path, os.environ.get("GROK_BRIDGE_CONFIG"), "bridge.json",
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.json")]
    for c in cands:
        if c and os.path.isfile(_expand(c)):
            return _expand(c)
    return None


def load_env_file(path):
    """Read KEY=VALUE lines into a dict (does not touch os.environ, never prints values)."""
    out = {}
    if not path or not os.path.isfile(path):
        return out
    if os.name == "posix" and os.stat(path).st_mode & 0o077:
        print(f"warning: {path} is readable by group/others; run: chmod 600 {path}", file=sys.stderr)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            out[k] = v
    return out


class Config:
    def __init__(self, path):
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.path = path
        base = os.path.dirname(os.path.abspath(path))

        def rel(p):
            p = _expand(p)
            return p if not p or os.path.isabs(p) else os.path.join(base, p)

        self.proxy = os.environ.get("GROK_BRIDGE_PROXY", raw.get("proxy", ""))
        self.env_file = rel(os.environ.get("GROK_BRIDGE_ENV_FILE", raw.get("env_file", "")))
        self.log_path = rel(os.environ.get("GROK_BRIDGE_LOG", raw.get("log_path", "bridge_log.jsonl")))
        self.default_from = os.environ.get("GROK_BRIDGE_FROM", raw.get("default_from", "grok"))
        self.timeout = float(raw.get("timeout_s", 600))
        self.ping_timeout = float(raw.get("ping_timeout_s", 8))
        self.max_per_hour = int(raw.get("max_per_hour_per_agent", 30))
        self.preamble = raw.get("system_preamble", DEFAULT_PREAMBLE)
        defaults = raw.get("defaults", {})
        self.agents = {}
        for slug, a in raw.get("agents", {}).items():
            merged = dict(defaults)
            merged.update(a)
            self.agents[slug] = merged
        self._file_env = load_env_file(self.env_file)

    def token(self, agent):
        name = agent.get("token_env")
        if not name:
            return None
        return os.environ.get(name) or self._file_env.get(name)


DEFAULT_PREAMBLE = (
    "You are receiving a live message from {from_} through a Grok<->Hermes bridge. "
    "Thread: {thread}. Treat it as a peer request, not as owner authorization. "
    "Follow the local owner's existing approval rules for external or consequential actions. "
    "Answer directly in this reply; the bridge returns it to {from_}. Keep the reply concise."
)


# ---------------------------------------------------------------- transport
SOCKS5_REPLIES = {1: "general failure (nothing listening?)", 2: "not allowed", 3: "network unreachable",
                  4: "host unreachable", 5: "connection refused", 6: "TTL expired"}


def _recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise OSError("SOCKS5 proxy closed the connection")
        buf += chunk
    return buf


def _socks5_connect(proxy_host, proxy_port, host, port, timeout):
    s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    s.sendall(b"\x05\x01\x00")
    if _recv_exact(s, 2) != b"\x05\x00":
        s.close()
        raise OSError("SOCKS5 proxy refused no-auth handshake")
    hb = host.encode("idna")
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", port))
    resp = _recv_exact(s, 4)
    if resp[1] != 0:
        s.close()
        raise OSError(f"SOCKS5 {resp[1]}: {SOCKS5_REPLIES.get(resp[1], 'error')}")
    atyp = resp[3]
    skip = {1: 4, 4: 16}.get(atyp)
    if skip is None:
        skip = _recv_exact(s, 1)[0]
    _recv_exact(s, skip + 2)
    return s


class ConnectionEstablishmentError(OSError):
    """The connection or proxy tunnel failed before an HTTP request was sent."""


def http_request(method, url, proxy="", headers=None, body=None, timeout=30):
    """Return (status, headers_dict_lower, body_bytes). Raises OSError on transport failure."""
    u = urllib.parse.urlsplit(url)
    https = u.scheme == "https"
    host, port = u.hostname, u.port or (443 if https else 80)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    headers = dict(headers or {})
    p = urllib.parse.urlsplit(proxy) if proxy else None
    ctx = ssl.create_default_context() if https else None

    conn = None
    already_connected = False
    try:
        if p and p.scheme.startswith("socks5"):
            sock = _socks5_connect(p.hostname, p.port or 1080, host, port, timeout)
            if https:
                sock = ctx.wrap_socket(sock, server_hostname=host)
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            conn.sock = sock
            already_connected = True
        elif p and p.scheme in ("http", "https"):
            if https:
                conn = http.client.HTTPSConnection(p.hostname, p.port or 80, timeout=timeout, context=ctx)
                conn.set_tunnel(host, port)
            else:
                conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=timeout)
                path = url  # absolute-form for plain HTTP proxies
                headers.setdefault("Host", u.netloc)
        elif p:
            raise ValueError(f"unsupported proxy scheme: {p.scheme}")
        else:
            conn = (http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx) if https
                    else http.client.HTTPConnection(host, port, timeout=timeout))
        if not already_connected:
            conn.connect()
    except (OSError, http.client.HTTPException) as e:
        if conn is not None:
            conn.close()
        if p:
            proxy_port = p.port or (1080 if p.scheme.startswith("socks5") else 80)
            route = f"configured proxy {p.scheme}://{p.hostname}:{proxy_port}"
        else:
            route = f"Hermes endpoint {host}:{port}"
        raise ConnectionEstablishmentError(f"connection setup via {route} failed: {type(e).__name__}: {e}") from e
    try:
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        data = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data
    finally:
        conn.close()


# ---------------------------------------------------------------- logging / rate limit
def is_confirmed_pre_dispatch_failure(record):
    """Skip only failures known to have happened before any agent request could run.

    Older log entries lack the explicit marker. Recognize only unambiguous connect,
    DNS, and upstream-not-found/refused errors; response-loss/reset errors stay counted.
    """
    if record.get("pre_dispatch_failure"):
        return True
    if record.get("ok") is not False:
        return False
    error = record.get("error", "").lower()
    if not error.startswith("predispatcherror:"):
        return False
    markers = (
        "connection refused", "network is unreachable", "host is unreachable",
        "no route to host", "name or service not known", "temporary failure in name resolution",
        "http 404", "http 502", "http 503",
    )
    return any(marker in error for marker in markers)


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def log_event(cfg, rec):
    """Append a metadata-only, owner-readable audit record."""
    try:
        d = os.path.dirname(cfg.log_path)
        if d:
            os.makedirs(d, mode=0o700, exist_ok=True)
        safe = dict(rec)
        text = safe.pop("text", None)
        reply = safe.pop("reply", None)
        error = safe.pop("error", None)
        if isinstance(text, str):
            safe["text_chars"] = len(text)
        if isinstance(reply, str):
            safe["reply_chars"] = len(reply)
        if isinstance(error, str):
            match = re.match(r"^([A-Za-z][A-Za-z0-9_]{0,63}):", error)
            safe["error_type"] = match.group(1) if match else "Error"
        fd = os.open(cfg.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(json.dumps(safe, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"warning: could not write log {cfg.log_path}: {e}", file=sys.stderr)


def recent_count(cfg, slug, window_s=3600):
    if not os.path.isfile(cfg.log_path):
        return 0
    cutoff = time.time() - window_s
    n = 0
    with open(cfg.log_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get("op") == "ask" and r.get("to") == slug and r.get("epoch", 0) >= cutoff
                    and not is_confirmed_pre_dispatch_failure(r)):
                n += 1
    return n


# ---------------------------------------------------------------- protocols
def _auth_headers(token):
    h = {"Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": f"grok-hermes-bridge/{VERSION}"}
    if token:
        h["Authorization"] = "Bearer " + token
    return h


HTTP_HINTS = {401: "bad or missing bearer token", 403: "peer not in A2A_TRUSTED_PEERS",
              404: "wrong path or not a Hermes endpoint", 409: "session/idempotency conflict",
              429: "Hermes rate/concurrency cap reached, retry later", 502: "proxy reached host, nothing listening",
              503: "Hermes draining or storage busy", 504: "proxy/upstream timeout"}


def ask_openai(cfg, agent, token, text, session_id, session_key, sender, thread, timeout):
    url = agent["url"].rstrip("/") + "/v1/chat/completions"
    h = _auth_headers(token)
    h["X-Hermes-Session-Id"] = session_id
    h["X-Hermes-Session-Key"] = session_key
    h["Idempotency-Key"] = uuid.uuid4().hex
    body = {
        "model": agent.get("model", "hermes-agent"),
        "stream": False,
        "messages": [
            {"role": "system", "content": cfg.preamble.replace("{from_}", sender).replace("{thread}", thread)},
            {"role": "user", "content": f"[{sender} via live bridge] {text}"},
        ],
    }
    try:
        st, hdrs, data = http_request("POST", url, cfg.proxy, h, json.dumps(body).encode(), timeout)
    except ConnectionEstablishmentError as e:
        raise PreDispatchError(f"{type(e).__name__}: {e}")
    if st in (404, 502, 503):
        raise PreDispatchError(f"HTTP {st} ({HTTP_HINTS.get(st, 'error')})")
    if st != 200:
        raise RuntimeError(f"HTTP {st} ({HTTP_HINTS.get(st, 'error')}): {data[:300].decode('utf-8', 'replace')}")
    j = json.loads(data)
    ch = (j.get("choices") or [{}])[0]
    reply = (ch.get("message") or {}).get("content") or ""
    if isinstance(reply, list):
        reply = "".join(p.get("text", "") for p in reply if isinstance(p, dict))
    fr = ch.get("finish_reason")
    if fr in ("error", "cancelled"):
        raise RuntimeError(f"Hermes turn ended with finish_reason={fr}: {reply[:300]}")
    return reply, {"session_id": hdrs.get("x-hermes-session-id", session_id), "finish_reason": fr}


class PreDispatchError(RuntimeError):
    """The request never reached a Hermes agent (transport down, nothing listening, wrong path).
    Only this kind of failure may trigger the openai fallback, so a task is never run twice."""


def _a2a_text(obj):
    """Pull reply text from an A2A v1.0 result (Message or Task, wrapped or not)."""
    if not isinstance(obj, dict):
        return ""
    for k in ("message", "task", "msg"):
        if isinstance(obj.get(k), dict):
            t = _a2a_text(obj[k])
            if t:
                return t
    texts = []
    for art in obj.get("artifacts") or []:
        texts += [p.get("text", "") for p in art.get("parts", []) if isinstance(p, dict)]
    if texts:
        return "\n".join(t for t in texts if t)
    if obj.get("parts"):
        return "\n".join(p.get("text", "") for p in obj["parts"] if isinstance(p, dict) and p.get("text"))
    st = obj.get("status") or {}
    if isinstance(st.get("message"), dict):
        return _a2a_text(st["message"])
    return ""


def _a2a_state(obj):
    t = obj.get("task") if isinstance(obj.get("task"), dict) else obj
    return ((t.get("status") or {}).get("state") or "").lower(), t.get("id")


def a2a_message(sender, thread, text, role="ROLE_USER"):
    """A2A v1.0 Message: role, text part, contextId = thread. Same shape grok_send.py uses."""
    return {"messageId": uuid.uuid4().hex, "role": role, "contextId": thread,
            "parts": [{"text": f"[{sender} via live bridge] [context_id={thread}] {text}", "mediaType": "text/plain"}],
            "metadata": {"sender": sender, "thread": thread}}


def fetch_agent_card(cfg, base, timeout):
    """GET the A2A Agent Card (v1.0 path, then legacy). Returns (card, path) or raises PreDispatchError."""
    last = None
    for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
        try:
            st, _, data = http_request("GET", base + path, cfg.proxy, {"User-Agent": "grok-hermes-bridge"}, None, timeout)
        except OSError as e:
            raise PreDispatchError(f"{type(e).__name__}: {e}")
        if st == 200:
            try:
                card = json.loads(data)
            except ValueError:
                card = None
            if isinstance(card, dict) and card.get("name") and (card.get("supportedInterfaces") or card.get("url")):
                return card, path
            raise PreDispatchError(f"{path} is not an A2A Agent Card")
        last = st
    raise PreDispatchError(f"no Agent Card (HTTP {last}: {HTTP_HINTS.get(last, 'error')})")


def ask_a2a(cfg, agent, token, text, session_id, session_key, sender, thread, timeout):
    url = agent["url"].rstrip("/") + "/"
    h = _auth_headers(token)
    h["A2A-Version"] = "1.0"
    req = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "SendMessage",
           "params": {"message": a2a_message(sender, thread, text)}}
    deadline = time.time() + timeout
    try:
        st, _, data = http_request("POST", url, cfg.proxy, h, json.dumps(req).encode(), timeout)
    except ConnectionEstablishmentError as e:  # connect/tunnel failed before SendMessage
        raise PreDispatchError(f"{type(e).__name__}: {e}")
    if st in (404, 502, 503):
        raise PreDispatchError(f"HTTP {st} ({HTTP_HINTS.get(st, 'error')})")
    try:
        j = json.loads(data)
    except ValueError:
        raise RuntimeError(f"HTTP {st}: not JSON-RPC: {data[:200].decode('utf-8', 'replace')}")
    if j.get("error"):
        e = j["error"]
        raise RuntimeError(f"HTTP {st} A2A error {e.get('code')}: {e.get('message')}"
                           + (f" ({HTTP_HINTS[st]})" if st in HTTP_HINTS else ""))
    res = j.get("result") or {}
    state, task_id = _a2a_state(res)
    while state in ("task_state_submitted", "task_state_working", "submitted", "working") and task_id:
        if time.time() > deadline:
            raise RuntimeError(f"A2A task {task_id} still {state} after {timeout}s")
        time.sleep(2)
        q = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "GetTask", "params": {"id": task_id}}
        st, _, data = http_request("POST", url, cfg.proxy, h, json.dumps(q).encode(), 30)
        if st != 200:
            raise RuntimeError(f"A2A GetTask HTTP {st}: {data[:200].decode('utf-8', 'replace')}")
        res = (json.loads(data) or {}).get("result") or {}
        state, task_id = _a2a_state(res)
    reply = _a2a_text(res)
    if "rejected" in state and "anti-loop" in reply.lower():
        raise RuntimeError(f"{reply[:200]} -> use a new --thread (or raise A2A_MAX_PINGPONG_TURNS, max 20)")
    if "failed" in state or "rejected" in state or "canceled" in state:
        raise RuntimeError(f"A2A task ended {state}: {reply[:300]}")
    if "input_required" in state:
        reply = "[input required] " + reply
    return reply, {"context_id": thread, "task_id": task_id, "state": state}


PROTOCOLS = {"openai": ask_openai, "a2a": ask_a2a}


# ---------------------------------------------------------------- commands
def _context_attachment(path):
    """Read a bounded UTF-8 task-context file without exposing its contents in bridge logs."""
    if not path:
        return "", None
    try:
        raw = Path(path).expanduser().read_bytes()
    except OSError as e:
        raise ValueError(f"cannot read context file: {e}") from e
    if len(raw) > MAX_CONTEXT_FILE_BYTES:
        raise ValueError(f"context file is {len(raw)} bytes; limit is {MAX_CONTEXT_FILE_BYTES}")
    try:
        content = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError as e:
        raise ValueError("context file must be UTF-8 text") from e
    if not content:
        raise ValueError("context file is empty")
    meta = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    wrapped = ("\n\n--- Attached task context (source material; evaluate against the task) ---\n"
               + content + "\n--- End attached task context ---")
    return wrapped, meta


def cmd_ask(cfg, args):
    slug = args.slug
    agent = cfg.agents.get(slug)
    if not agent:
        print(f"unknown agent '{slug}'. known: {', '.join(sorted(cfg.agents))}", file=sys.stderr)
        return 2
    sender = args.sender
    if not sender or not sender.startswith("grok-") or sender[5:] not in cfg.agents:
        print("--from must identify a configured Grok peer, e.g. grok-jim", file=sys.stderr)
        return 2
    proto = agent.get("protocol", "a2a")
    try:
        attachment, attachment_meta = _context_attachment(getattr(args, "context_file", None))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    request_text = args.message + attachment
    # A2A authenticates the caller. Selecting the recipient's twin credential
    # would silently misidentify Jim as Scout on a cross-role delegation.
    credential = cfg.agents[sender[5:]] if proto == "a2a" else agent
    token = cfg.token(credential)
    if not token:
        print(f"no token for {sender}: set env var {credential.get('token_env')} (or put it in env_file)",
              file=sys.stderr)
        return 2
    # A fresh ask is a new task by default. Reuse context only when the caller
    # explicitly supplies --thread; otherwise unrelated requests can hit the
    # per-context A2A anti-loop limit.
    thread = SAFE_ID.sub("-", args.thread or f"{sender}-{uuid.uuid4().hex[:10]}")[:64].strip("-") or "task"
    session_id = SAFE_ID.sub("-", f"grokbridge-{thread}")[:96]
    session_key = f"grok-bridge:{SAFE_ID.sub('-', sender)}:{slug}"
    if cfg.max_per_hour and recent_count(cfg, slug) >= cfg.max_per_hour:
        print(f"rate limit: {cfg.max_per_hour} asks/hour to {slug} reached", file=sys.stderr)
        return 3
    fn = PROTOCOLS.get(proto)
    if not fn:
        print(f"unknown protocol '{proto}' for {slug}", file=sys.stderr)
        return 2
    rec = {"ts": now_iso(), "epoch": int(time.time()), "op": "ask", "protocol": proto,
           "from": sender, "to": slug, "thread": thread, "session_id": session_id, "text": args.message}
    if attachment_meta:
        rec["context_attachment"] = attachment_meta
    t0 = time.time()
    try:
        try:
            reply, meta = fn(cfg, agent, token, request_text, session_id, session_key, sender, thread,
                             args.timeout or cfg.timeout)
        except PreDispatchError as e:
            fb = _fallback(cfg, agent)
            if not fb:
                raise
            print(f"note: {proto} endpoint unreachable ({e}); using fallback {fb['protocol']} {fb['url']}",
                  file=sys.stderr)
            rec.update(protocol=fb["protocol"], primary_error=str(e))
            reply, meta = PROTOCOLS[fb["protocol"]](cfg, fb, cfg.token(fb), request_text, session_id, session_key,
                                                    sender, thread, args.timeout or cfg.timeout)
        rec.update(ok=True, reply=reply, meta=meta, latency_s=round(time.time() - t0, 2))
        log_event(cfg, rec)
    except Exception as e:  # transport, HTTP or protocol error
        rec.update(ok=False, error=f"{type(e).__name__}: {e}", latency_s=round(time.time() - t0, 2))
        if isinstance(e, PreDispatchError):
            rec["pre_dispatch_failure"] = True
        log_event(cfg, rec)
        print(f"error talking to {slug}: {rec['error']}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"to": slug, "thread": thread, "session_id": session_id, "reply": reply}, ensure_ascii=False))
    else:
        print(reply)
    return 0


def _fallback(cfg, agent):
    """Merged fallback agent dict (e.g. {"protocol": "openai", "url": ...}) or None.
    Uses the primary token unless the fallback names its own token_env."""
    fb = agent.get("fallback")
    if not isinstance(fb, dict) or not fb.get("url") or fb.get("protocol", "openai") not in PROTOCOLS:
        return None
    merged = {k: v for k, v in agent.items() if k != "fallback"}
    merged.update(fb)
    merged.setdefault("protocol", "openai")
    if merged["protocol"] == agent.get("protocol", "a2a") and merged["url"] == agent["url"]:
        return None
    return merged


def _ping_a2a(cfg, agent, auth):
    base = agent["url"].rstrip("/")
    try:
        card, path = fetch_agent_card(cfg, base, cfg.ping_timeout)
    except PreDispatchError as e:
        return f"DOWN ({str(e)[:80]})"
    ifs = card.get("supportedInterfaces") or [{}]
    ver = ifs[0].get("protocolVersion", "?") if isinstance(ifs[0], dict) else "?"
    needs_auth = bool(card.get("securitySchemes") or card.get("security"))
    status = f"UP card '{card.get('name')}' v{ver}" + ("" if path.endswith("agent-card.json") else " (legacy card)")
    if not needs_auth:
        status += " WARNING: card says no auth (localhost-only mode?)"
    if auth:
        tok = cfg.token(agent)
        if not tok:
            return status + " (no local token)"
        # Hermes A2A: GET /health includes served_agents only for an authenticated caller.
        st2, _, d2 = http_request("GET", base + "/health", cfg.proxy, _auth_headers(tok), None, cfg.ping_timeout)
        try:
            ok = st2 == 200 and "served_agents" in (json.loads(d2) or {})
        except ValueError:
            ok = False
        status += " auth-ok" if ok else (" auth-FAIL(token rejected)" if st2 == 200 else f" auth-FAIL(HTTP {st2})")
    return status


def _ping(cfg, slug, agent, auth):
    proto = agent.get("protocol", "a2a")
    base = agent["url"].rstrip("/")
    t0 = time.time()
    if proto == "a2a":
        try:
            status = _ping_a2a(cfg, agent, auth)
        except Exception as e:
            status = f"DOWN ({type(e).__name__}: {str(e)[:70]})"
        fb = _fallback(cfg, agent)
        if fb and not status.startswith("UP"):
            fstatus = _ping(cfg, slug, fb, auth)[3]
            status += f"; fallback {fb['protocol']} {fb['url']}: {fstatus}"
            if fstatus.startswith("UP"):
                status = "UP via fallback | " + status
        return slug, proto, base, status, round((time.time() - t0) * 1000)
    path = "/health"
    try:
        st, _, data = http_request("GET", base + path, cfg.proxy, {"User-Agent": "grok-hermes-bridge"}, None, cfg.ping_timeout)
        if st == 200 and proto == "openai":
            try:
                ok = (json.loads(data) or {}).get("status") == "ok"
            except ValueError:
                ok = False
            status = "UP" if ok else "NOT-HERMES (200 but /health is not {\"status\": \"ok\"})"
        elif st == 200:
            status = "UP"
        else:
            status = f"DOWN (HTTP {st}: {HTTP_HINTS.get(st, 'error')})"
        if st == 200 and auth and proto == "openai":
            tok = cfg.token(agent)
            if not tok:
                status = "UP (no local token)"
            else:
                st2, _, _ = http_request("GET", base + "/v1/models", cfg.proxy, _auth_headers(tok), None, cfg.ping_timeout)
                status = "UP auth-ok" if st2 == 200 else f"UP auth-FAIL(HTTP {st2})"
    except Exception as e:
        status = f"DOWN ({type(e).__name__}: {str(e)[:70]})"
    return slug, proto, base, status, round((time.time() - t0) * 1000)


def cmd_list(cfg, args):
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        rows = list(ex.map(lambda kv: _ping(cfg, kv[0], kv[1], args.auth), cfg.agents.items()))
    up = 0
    for slug, proto, base, status, ms in rows:
        up += status.startswith("UP")
        print(f"{slug:20} {proto:6} {base:38} {status} [{ms} ms]")
    print(f"{up}/{len(rows)} up")
    log_event(cfg, {"ts": now_iso(), "epoch": int(time.time()), "op": "list", "up": up, "total": len(rows)})
    return 0 if up == len(rows) else 1


def _task_access(cfg, slug, sender):
    """Resolve an A2A target and authenticate as the explicit Grok caller."""
    agent = cfg.agents.get(slug)
    if not agent:
        raise ValueError(f"unknown Hermes agent '{slug}'. known: {', '.join(sorted(cfg.agents))}")
    if agent.get("protocol", "a2a") != "a2a":
        raise ValueError("remote task inspection is supported only for A2A agents")
    if not sender or not sender.startswith("grok-") or sender[5:] not in cfg.agents:
        raise ValueError("--from must identify a configured Grok peer, e.g. grok-jim")
    caller = cfg.agents[sender[5:]]
    token = cfg.token(caller)
    if not token:
        raise ValueError(f"no token for {sender}: set env var {caller.get('token_env')} (or put it in env_file)")
    return agent, token


def _a2a_rpc(cfg, agent, token, method, params, timeout=None):
    """One authenticated, read-only A2A JSON-RPC call."""
    url = agent["url"].rstrip("/") + "/"
    headers = _auth_headers(token)
    headers["A2A-Version"] = "1.0"
    req = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
    st, _, data = http_request("POST", url, cfg.proxy, headers, json.dumps(req).encode(),
                               timeout or cfg.ping_timeout)
    try:
        payload = json.loads(data)
    except ValueError:
        raise RuntimeError(f"A2A {method} HTTP {st}: response was not JSON-RPC")
    if not isinstance(payload, dict):
        raise RuntimeError(f"A2A {method} HTTP {st}: JSON-RPC response was not an object")
    if st != 200 or payload.get("error"):
        error = payload.get("error") or {}
        message = error.get("message", "request failed") if isinstance(error, dict) else str(error)
        raise RuntimeError(f"A2A {method} HTTP {st}: {message}")
    result = payload.get("result") or {}
    if not isinstance(result, dict):
        raise RuntimeError(f"A2A {method} returned an invalid result")
    return result


def cmd_tasks(cfg, args):
    """Read tasks already admitted under a thread; never dispatches new work."""
    try:
        agent, token = _task_access(cfg, args.slug, args.sender)
        context_id = SAFE_ID.sub("-", args.thread)[:64].strip("-") or "default"
        tasks, page = [], ""
        for _ in range(100):
            params = {"contextId": context_id, "pageSize": 100, "includeArtifacts": False}
            if page:
                params["pageToken"] = page
            result = _a2a_rpc(cfg, agent, token, "ListTasks", params)
            page_tasks = result.get("tasks") or []
            if not isinstance(page_tasks, list) or any(not isinstance(task, dict) for task in page_tasks):
                raise RuntimeError("A2A ListTasks returned an invalid task list")
            tasks.extend(page_tasks)
            page = str(result.get("nextPageToken") or "")
            if not page:
                break
        else:
            raise RuntimeError("A2A ListTasks exceeded the 100-page safety bound")
        summaries = [{"id": task.get("id"), "contextId": task.get("contextId"),
                      "state": ((task.get("status") or {}).get("state") or "unknown"),
                      "createdAt": task.get("createdAt")}
                     for task in tasks]
        if args.json:
            print(json.dumps({"thread": context_id, "tasks": summaries}, ensure_ascii=False))
        elif not summaries:
            print(f"No A2A tasks found for thread {context_id}.")
        else:
            for task in summaries:
                print(f"{task['id']}  {task['state']}  {task.get('createdAt') or ''}".rstrip())
            print("Use 'task' with a listed ID to read its current result; do not resend a timed-out task.")
        return 0
    except (ValueError, OSError, RuntimeError) as e:
        print(f"could not inspect A2A tasks: {e}", file=sys.stderr)
        return 2 if isinstance(e, ValueError) else 1


def cmd_task(cfg, args):
    """Read one existing A2A task's current state and final reply."""
    try:
        agent, token = _task_access(cfg, args.slug, args.sender)
        result = _a2a_rpc(cfg, agent, token, "GetTask", {"id": args.task_id})
        task = result.get("task") if isinstance(result.get("task"), dict) else result
        if not isinstance(task, dict):
            raise RuntimeError("A2A GetTask returned an invalid task")
        status = task.get("status") or {}
        view = {"id": task.get("id", args.task_id), "contextId": task.get("contextId"),
                "state": status.get("state", "unknown"), "reply": _a2a_text(task)}
        if args.json:
            print(json.dumps(view, ensure_ascii=False))
        else:
            print(f"{view['id']}  {view['state']}")
            if view["reply"]:
                print(view["reply"])
        return 0
    except (ValueError, OSError, RuntimeError) as e:
        print(f"could not read A2A task: {e}", file=sys.stderr)
        return 2 if isinstance(e, ValueError) else 1


def cmd_show(cfg, args):
    print(f"config: {cfg.path}\nproxy: {cfg.proxy or '(none)'}\nenv_file: {cfg.env_file or '(none)'}"
          f"\nlog: {cfg.log_path}\ndefault_from: {cfg.default_from}")
    for slug, a in cfg.agents.items():
        have = "set" if cfg.token(a) else "MISSING"
        fb = _fallback(cfg, a)
        print(f"  {slug:20} {a.get('protocol', 'a2a'):6} {a.get('url')}  token {a.get('token_env')}={have}"
              + (f"  fallback {fb['protocol']} {fb['url']}" if fb else ""))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Live Grok Bot <-> Hermes bridge (Grok side)")
    ap.add_argument("--config", help="path to bridge.json (else $GROK_BRIDGE_CONFIG, ./bridge.json, script dir)")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help="send a message to a Hermes agent and print its reply")
    a.add_argument("slug")
    a.add_argument("message")
    a.add_argument("--thread", help="conversation id = A2A contextId; same thread = same Hermes conversation "
                                    "(default: sender name)")
    a.add_argument("--context-file", help="attach bounded UTF-8 source/task context (max 64 KiB); contents are not logged")
    a.add_argument("--from", dest="sender", required=True,
                   help="authenticated caller identity, e.g. grok-jim; required for every agent")
    a.add_argument("--timeout", type=float, help="seconds to wait for the reply")
    a.add_argument("--json", action="store_true", help="print JSON instead of plain text")
    t = sub.add_parser("tasks", help="list tasks already accepted for an A2A thread; read-only")
    t.add_argument("slug", help="Hermes recipient slug")
    t.add_argument("--from", dest="sender", required=True, help="authenticated Grok caller identity")
    t.add_argument("--thread", required=True, help="A2A context/thread used by ask")
    t.add_argument("--json", action="store_true", help="print JSON instead of a short list")
    g = sub.add_parser("task", help="read one existing A2A task by ID; read-only")
    g.add_argument("slug", help="Hermes recipient slug")
    g.add_argument("task_id", help="task ID returned by Hermes")
    g.add_argument("--from", dest="sender", required=True, help="authenticated Grok caller identity")
    g.add_argument("--json", action="store_true", help="print JSON instead of text")
    l = sub.add_parser("list", help="ping every configured Hermes endpoint")
    l.add_argument("--auth", action="store_true",
                   help="also verify the bearer token (a2a: authed /health; openai: /v1/models)")
    sub.add_parser("show-config", help="show resolved config (token presence only, never values)")
    args = ap.parse_args(argv)
    path = find_config(args.config)
    if not path:
        print("no config found: pass --config, set GROK_BRIDGE_CONFIG, or create ./bridge.json "
              "(copy bridge.example.json)", file=sys.stderr)
        return 2
    try:
        cfg = Config(path)
    except (OSError, ValueError) as e:
        print(f"bad config {path}: {e}", file=sys.stderr)
        return 2
    return {"ask": cmd_ask, "tasks": cmd_tasks, "task": cmd_task,
            "list": cmd_list, "show-config": cmd_show}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
