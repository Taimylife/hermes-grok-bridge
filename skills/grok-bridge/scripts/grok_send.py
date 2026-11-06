#!/usr/bin/env python3
"""grok_send.py - Hermes -> Grok Bot live message (POSTs JSON to a Grok Bot routine webhook).

Usage:
  grok_send.py --to grok-jim --text "message" [--thread ID] [--from SLUG] [--reply-needed] [--dry-run]
  echo "long message" | grok_send.py --to grok-jim --text -

Env (set in the profile/container .env; values are never printed):
  GROK_BRIDGE_WEBHOOK_URL        routine webhook URL ("POST to" field on the routine panel)   [required]
  GROK_BRIDGE_WEBHOOK_KEY        routine sender key ("key" field)                              [required]
  GROK_BRIDGE_WEBHOOK_URL__<TO>  / GROK_BRIDGE_WEBHOOK_KEY__<TO>  optional per-recipient override,
                                 <TO> = recipient upper-cased with '-' -> '_' (e.g. __GROK_NEWS)
  GROK_BRIDGE_AUTH_HEADER        header carrying the key (default: Authorization)
  GROK_BRIDGE_AUTH_SCHEME        prefix before the key (default: "Bearer"; set "" for a raw key)
  GROK_BRIDGE_EXTRA_KEY_HEADER   optional second header that carries the raw key (e.g. X-Automation-Key)
  GROK_BRIDGE_FROM               required calling-profile identity (e.g. hermes-jim); any --from override must match
  GROK_BRIDGE_MAX_PER_HOUR       send cap per rolling hour (default 20)
  GROK_BRIDGE_STATE_DIR          where the JSONL log lives
  GROK_BRIDGE_WORKSPACE          shared workspace root (default: /workspace), used when the
                                 configured state directory and HERMES_HOME are unavailable
  HTTPS_PROXY / https_proxy      honoured if set
Payload (JSON): an A2A v1.0 Message (the same shape bridge.py sends to Hermes over A2A):
  {"messageId", "role": "ROLE_USER", "contextId": <thread>, "parts": [{"text": <message>, "mediaType": "text/plain"}],
   "metadata": {"sender", "thread", "reply_needed"}}
plus flat compatibility fields {"sender", "thread", "message"} and "reply_needed": true only with --reply-needed.
Exit codes: 0 accepted (HTTP 2xx = Grok run started), 1 send failed, 2 config error, 3 rate limited.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

MAX_TEXT = 8000


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def state_path():
    candidates = []
    configured = env("GROK_BRIDGE_STATE_DIR")
    if configured:
        candidates.append(os.path.expanduser(configured))

    # HERMES_HOME may refer to the host profile path while this script runs in
    # a Docker terminal, where that host path is not mounted. Use it only when
    # the directory (or its parent, if it must be created) is writable.
    hermes_home = env("HERMES_HOME")
    if hermes_home:
        candidates.append(os.path.expanduser(hermes_home))

    # Hermes normally starts this script with HERMES_HOME set. Shell callers
    # (including desktop troubleshooting sessions) may not inherit it, so
    # prefer the canonical shared workspace before falling back to their
    # current directory or private home. The path is overrideable for other
    # deployments that mount the shared workspace elsewhere.
    shared_workspace = os.path.expanduser(env("GROK_BRIDGE_WORKSPACE", "/workspace"))
    if shared_workspace:
        candidates.append(os.path.join(shared_workspace, ".agent-state", "grok-bridge"))

    candidates.append(os.path.join(os.getcwd(), ".agent-state", "grok-bridge"))
    candidates.append(os.path.join(os.path.expanduser("~"), ".hermes"))

    for base in candidates:
        writable_parent = os.path.abspath(base)
        while not os.path.exists(writable_parent):
            parent = os.path.dirname(writable_parent)
            if parent == writable_parent:
                break
            writable_parent = parent
        if os.path.isdir(writable_parent) and os.access(writable_parent, os.W_OK):
            return os.path.join(base, "grok_bridge_log.jsonl")
    return os.path.join(candidates[-1], "grok_bridge_log.jsonl")


def sent_last_hour(path):
    if not os.path.isfile(path):
        return 0
    cutoff, n = time.time() - 3600, 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("ok") and r.get("epoch", 0) >= cutoff:
                n += 1
    return n


def log(path, rec):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        safe = dict(rec)
        error = safe.pop("error", None)
        if isinstance(error, str):
            match = re.match(r"^([A-Za-z][A-Za-z0-9_]{0,63}):", error)
            safe["error_type"] = match.group(1) if match else "Error"
        with open(path, "a", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(json.dumps(safe, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        print(f"warning: Grok bridge audit log could not be written to {path} ({type(exc).__name__}: {exc.strerror}); delivery status is reported separately", file=sys.stderr)
        return False


def a2a_payload(sender, thread, text, reply_needed=False, recipient=None):
    """A2A v1.0 Message shape (role, parts, contextId = thread) + flat fields for the Grok routine."""
    meta = {"sender": sender, "thread": thread, "reply_needed": bool(reply_needed)}
    routed_text = f"[context_id={thread}] {text}"
    payload = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "contextId": thread,
               "parts": [{"text": routed_text, "mediaType": "text/plain"}], "metadata": meta,
               "sender": sender, "thread": thread, "message": routed_text}
    if recipient:
        meta["recipient"] = recipient
        payload["to"] = recipient
    if reply_needed:
        payload["reply_needed"] = True
    return payload


def main(argv=None):
    ap = argparse.ArgumentParser(description="Send a live message from this Hermes agent to a Grok Bot agent")
    ap.add_argument("--to", required=True, help="Grok agent name, e.g. grok-jim")
    ap.add_argument("--text", required=True, help="message text, or - to read stdin")
    ap.add_argument("--thread", help="thread id to continue (default: new id)")
    ap.add_argument("--from", dest="sender", help="sender name (default: profile name)")
    ap.add_argument("--reply-needed", "--reply-expected", dest="reply_needed", action="store_true",
                    help="ask the Grok agent to answer via the bridge")
    ap.add_argument("--dry-run", action="store_true", help="print the payload, do not send")
    a = ap.parse_args(argv)

    text = sys.stdin.read() if a.text == "-" else a.text
    text = text.strip()
    if not text:
        print("empty message", file=sys.stderr)
        return 2
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "\n[truncated by grok_send]"
    to_key = re.sub(r"[^A-Z0-9]", "_", a.to.upper())
    configured_sender = env("GROK_BRIDGE_FROM")
    if not configured_sender or not re.fullmatch(r"hermes-[a-z0-9]+(?:-[a-z0-9]+)*", configured_sender):
        print("missing or invalid GROK_BRIDGE_FROM profile identity; refusing to send", file=sys.stderr)
        return 2
    if a.sender and a.sender != configured_sender:
        print("--from does not match this profile's GROK_BRIDGE_FROM identity; refusing to send", file=sys.stderr)
        return 2
    sender = configured_sender
    override_url = env(f"GROK_BRIDGE_WEBHOOK_URL__{to_key}")
    override_key = env(f"GROK_BRIDGE_WEBHOOK_KEY__{to_key}")
    if bool(override_url) != bool(override_key):
        print("recipient override requires both webhook URL and key; not sent", file=sys.stderr)
        return 2
    if not override_url:
        print("no explicit webhook route for this recipient; refusing fallback to another inbox", file=sys.stderr)
        return 2
    url = override_url
    key = override_key
    thread = re.sub(r"[^A-Za-z0-9._-]+", "-", a.thread or f"{sender}-{uuid.uuid4().hex[:8]}")[:64]
    payload = a2a_payload(sender, thread, text, a.reply_needed, a.to)
    if a.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"url set: {bool(url)}  key set: {bool(key)}", file=sys.stderr)
        return 0
    if not url or not key:
        print("missing GROK_BRIDGE_WEBHOOK_URL / GROK_BRIDGE_WEBHOOK_KEY in the environment", file=sys.stderr)
        return 2
    path = state_path()
    cap = int(env("GROK_BRIDGE_MAX_PER_HOUR", "20"))
    if cap and sent_last_hour(path) >= cap:
        print(f"rate limit: {cap} messages in the last hour; not sent", file=sys.stderr)
        return 3

    scheme = os.environ.get("GROK_BRIDGE_AUTH_SCHEME", "Bearer").strip()  # "" = send the raw key
    headers = {"Content-Type": "application/json", "User-Agent": "hermes-grok-bridge/1.0",
               env("GROK_BRIDGE_AUTH_HEADER", "Authorization"): (f"{scheme} {key}" if scheme else key)}
    extra = env("GROK_BRIDGE_EXTRA_KEY_HEADER")
    if extra:
        headers[extra] = key
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "epoch": int(time.time()),
           "to": a.to, "sender": sender, "thread": thread, "reply_needed": bool(a.reply_needed), "chars": len(text)}
    try:
        with urllib.request.urlopen(req, timeout=float(env("GROK_BRIDGE_TIMEOUT", "15"))) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    except (urllib.error.URLError, OSError) as e:
        # Webhook URLs can contain bearer-like credentials; never echo the URL.
        rec.update(ok=False, error=f"{type(e).__name__}: request failed")
        log(path, rec)
        print(f"send failed: {type(e).__name__} (request URL and body omitted)", file=sys.stderr)
        return 1
    ok = 200 <= status < 300
    rec.update(ok=ok, http=status)
    audit_logged = log(path, rec)
    if not ok:
        print(f"send failed: HTTP {status} (routine paused, wrong URL, or stale key)", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "to": a.to, "thread": thread, "http": status, "audit_logged": audit_logged,
                      "note": "HTTP 2xx = Grok run started; any answer arrives later via the bridge"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
