"""Loopback-only Discord REST API v10 double. No Discord account or credential is used.

It reproduces the documented request/response shapes used by the block:
`Authorization: Bot <token>`; channel messages (create with content, embeds,
message_reference, allowed_mentions, flags, nonce + enforce_nonce; edit; delete
answering 204; reads with limit and before/after, newest first, `after`
returning the messages right after the given ID); reactions (PUT .../@me, 204);
multipart uploads (payload_json + files[n] with attachments); webhook execution
with ?wait=true and thread_id; error bodies {"message", "code", "errors"};
X-RateLimit-* headers and 429 bodies with retry_after and global.
Snowflake IDs follow Discord's epoch so they grow with time.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

# Built at run time so no fixture literal resembles a provider credential format.
TOKEN = "-".join(("discord", "fixture", "bot", "token", "not", "a", "real", "one", "0123456789"))
OTHER_TOKEN = "-".join(("discord", "fixture", "revoked", "token", "0123456789"))
WEBHOOK_TOKEN = "-".join(("fixture", "webhook", "token", "not", "real", "0123456789"))
REF = "secret://workspace/discord_fixture"
HOOK_REF = "secret://workspace/discord_hook_fixture"
GUILD = "1000000000000000001"
GENERAL = "1100000000000000001"
RANDOM = "1100000000000000002"
PRIVATE = "1100000000000000003"
OTHER = "1100000000000000004"
FORUM_THREAD = "1100000000000000005"
MISSING = "1100000000000000009"
BOT_ID = "1200000000000000001"
USER_ID = "1300000000000000001"
WEBHOOK_ID = "1400000000000000001"
CUSTOM_EMOJI = "1500000000000000001"
EPOCH = 1420070400000
BOT = {"id": BOT_ID, "username": "fixture-bot", "global_name": None, "bot": True}
PERSON = {"id": USER_ID, "username": "ada", "global_name": "Ada", "bot": False}


def settings(api, **extra):
    """Block settings aimed at this fixture only."""
    return {"enabled": True, "secret_ref": REF, "allowed_channels": [GENERAL, RANDOM, PRIVATE, MISSING],
            "default_channel": GENERAL, "api_base_url": api.base_url, "ca_file": api.ca_file, "timeout_sec": 20,
            **extra}


def webhook_url(api, token=WEBHOOK_TOKEN, ident=WEBHOOK_ID):
    return f"{api.base_url}/api/webhooks/{ident}/{token}"


def iso(ms):
    stamp = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}000+00:00"


def error(code, message, **extra):
    return {"message": message, "code": code, **extra}


@contextmanager
def discord_api(tls=False):
    """Run the double on a dynamic loopback port; every owned thread is joined on exit."""
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        lock = threading.Lock()
        api = SimpleNamespace(calls=[], script={}, release=threading.Event(), closing=threading.Event(),
                              tokens={TOKEN}, holding=0, denied={PRIVATE}, last=[0], nonces={},
                              channels={GENERAL: [], RANDOM: [], PRIVATE: [], OTHER: [], FORUM_THREAD: []},
                              buckets={}, bucket_limit=50, reset_after="1.0")

        def next_id(ms=None):
            with lock:
                moment = ms if ms is not None else int(time.time() * 1000)
                value = max((moment - EPOCH) << 22, api.last[0] + 1)
                api.last[0] = value
                return str(value)

        def add(channel, content, author=PERSON, ms=None, **extra):
            ident = next_id(ms)
            moment = (int(ident) >> 22) + EPOCH
            message = {"id": ident, "channel_id": channel, "guild_id": GUILD, "author": dict(author),
                       "content": content, "timestamp": iso(moment), "edited_timestamp": None, "tts": False,
                       "mention_everyone": False, "mentions": [], "mention_roles": [], "attachments": [],
                       "embeds": [], "pinned": False, "type": 0, "flags": 0, **extra}
            api.channels[channel].append(message)
            return message

        api.next_id, api.add = next_id, add
        for index in range(1, 8):
            add(GENERAL, f"history {index}", ms=int(time.time() * 1000) - 3600000 * 3 + index * 1000)
        add(RANDOM, "bot status", author=BOT)

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def reply(self, status, body, headers=None):
                data = b"" if body is None else json.dumps(body).encode("utf-8")
                self.send_response(status)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                if data:
                    self.wfile.write(data)
                self.close_connection = True

            def bucket(self, name):
                """Discord-style X-RateLimit-* headers for one route bucket."""
                with lock:
                    used = api.buckets.get(name, 0) + 1
                    api.buckets[name] = used
                left = max(0, api.bucket_limit - used)
                return {"X-RateLimit-Limit": str(api.bucket_limit), "X-RateLimit-Remaining": str(left),
                        "X-RateLimit-Reset-After": api.reset_after, "X-RateLimit-Bucket": "fixture-" + name}

            def scripted(self, kind):
                """Apply one scripted action; return True when the reply has been handled."""
                queue = api.script.get(kind)
                if not queue:
                    return False
                step = queue.pop(0)
                if step == "pass":
                    return False
                if step == "hold":
                    with lock:
                        api.holding += 1
                    try:
                        api.release.wait(20)
                    finally:
                        with lock:
                            api.holding -= 1
                    return api.closing.is_set()
                if step == "lost":
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.close_connection = True
                    return True
                if step == "html502":
                    self.raw(502, b"<html>Bad Gateway</html>", {"Content-Type": "text/html"})
                    return True
                if step == "oversized":
                    self.reply(200, [{"id": "1300000000000000001", "channel_id": GENERAL, "content": "y" * 70000}])
                    return True
                if step == "gzip":
                    self.raw(200, b"\x1f\x8b", {"Content-Type": "application/json", "Content-Encoding": "gzip"})
                    return True
                self.reply(step["status"], step.get("body"), step.get("headers"))
                return True

            def raw(self, status, data, headers):
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def handle_any(self, method):
                parsed = urlsplit(self.path)
                raw = self.body()
                call = {"method": method, "path": parsed.path, "query": parse_qs(parsed.query), "at": time.monotonic(),
                        "auth": self.headers.get("Authorization"), "agent": self.headers.get("User-Agent"),
                        "accept_encoding": self.headers.get("Accept-Encoding"),
                        "content_type": self.headers.get("Content-Type"), "body": None, "files": [], "kind": None}
                media = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip()
                if media == "application/json" and raw:
                    call["body"] = json.loads(raw)
                elif media == "multipart/form-data":
                    message = BytesParser(policy=HTTP).parsebytes(
                        b"Content-Type: " + self.headers["Content-Type"].encode("ascii") + b"\r\n\r\n" + raw)
                    for part in message.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        data = part.get_payload(decode=True)
                        if name == "payload_json":
                            call["body"] = json.loads(data)
                        else:
                            call["files"].append({"field": name, "filename": part.get_filename(), "size": len(data),
                                                  "data": data, "content_type": part.get_content_type()})
                with lock:
                    api.calls.append(call)
                hook = re.fullmatch(r"/api/v10/webhooks/([0-9]+)/([A-Za-z0-9_-]+)", parsed.path)
                if hook:
                    call["kind"] = "webhook"
                    call["path"] = f"/api/v10/webhooks/{hook.group(1)}/<token>"
                    call["webhook_token"] = hook.group(2)
                    if self.scripted("webhook"):
                        return
                    return self.webhook(method, hook.group(1), hook.group(2), call)
                match = re.fullmatch(r"/api/v10/channels/([0-9]+)/messages(?:/([0-9]+))?(?:/reactions/([^/]+)/@me)?",
                                     parsed.path)
                if not match:
                    return self.reply(404, error(0, "404: Not Found"))
                channel, message_id, emoji = match.groups()
                kind = {("POST", False, False): "upload" if call["files"] else "send", ("GET", False, False): "read",
                        ("PATCH", True, False): "edit", ("DELETE", True, False): "delete",
                        ("PUT", True, True): "react"}.get((method, bool(message_id), bool(emoji)))
                call["kind"] = kind
                if kind is None:
                    return self.reply(405, error(0, "405: Method Not Allowed"))
                if self.scripted(kind):
                    return
                if call["auth"] != "Bot " + TOKEN or TOKEN not in api.tokens:
                    return self.reply(401, error(0, "401: Unauthorized"))
                if channel not in api.channels:
                    return self.reply(404, error(10003, "Unknown Channel"))
                if channel in api.denied:
                    return self.reply(403, error(50001, "Missing Access"))
                headers = self.bucket(f"{method}-{channel}")
                handler = getattr(self, "op_" + kind)
                return handler(channel, message_id, emoji, call, headers)

            def find(self, channel, message_id):
                return next((item for item in api.channels[channel] if item["id"] == message_id), None)

            def check_message(self, body, files=()):
                content = body.get("content", "")
                embeds = body.get("embeds") or []
                if len(content) > 2000:
                    return error(50035, "Invalid Form Body", errors={"content": {"_errors": [
                        {"code": "BASE_TYPE_MAX_LENGTH", "message": "Must be 2000 or fewer in length."}]}})
                for index, embed in enumerate(embeds):
                    if len(embed.get("title", "")) > 256:
                        return error(50035, "Invalid Form Body", errors={"embeds": {str(index): {"title": {"_errors": [
                            {"code": "BASE_TYPE_MAX_LENGTH", "message": "Must be 256 or fewer in length."}]}}}})
                if not content and not embeds and not files:
                    return error(50006, "Cannot send an empty message")
                mentions = body.get("allowed_mentions")
                if not isinstance(mentions, dict) or not isinstance(mentions.get("parse"), list):
                    return error(50035, "Invalid Form Body", errors={"allowed_mentions": {"_errors": [
                        {"code": "MISSING", "message": "The block must always restrict mentions."}]}})
                return None

            def pinged(self, body):
                """Users pinged by @everyone/@here or <@id> under the allowed_mentions rules."""
                mentions, content = body.get("allowed_mentions") or {}, body.get("content", "")
                parse = mentions.get("parse", [])
                everyone = "everyone" in parse and ("@everyone" in content or "@here" in content)
                users = [ident for ident in re.findall(r"<@!?([0-9]+)>", content)
                         if "users" in parse or ident in mentions.get("users", [])]
                return everyone, users

            def created(self, channel, body, author, files=(), headers=None, webhook=None):
                nonce = body.get("nonce")
                if nonce is not None and body.get("enforce_nonce"):
                    existing = api.nonces.get((channel, author["id"], str(nonce)))
                    if existing is not None:
                        return self.reply(200, existing, headers)
                everyone, users = self.pinged(body)
                attachments = []
                for index, item in enumerate(files):
                    described = next((entry for entry in body.get("attachments", []) if str(entry.get("id")) == str(index)), {})
                    attachments.append({"id": api.next_id(), "filename": described.get("filename") or item["filename"],
                                        "size": item["size"], "content_type": item["content_type"],
                                        "url": f"https://cdn.example.test/attachments/{channel}/{item['filename']}",
                                        **({"description": described["description"]} if described.get("description") else {})})
                extra = {"embeds": [dict(embed, type="rich") for embed in body.get("embeds", [])],
                         "attachments": attachments, "mention_everyone": everyone,
                         "mentions": [{"id": ident, "username": "user" + ident[-2:]} for ident in users],
                         "flags": body.get("flags", 0)}
                reference = body.get("message_reference")
                if reference:
                    if self.find(channel, reference["message_id"]) is None:
                        return self.reply(400, error(50035, "Invalid Form Body", errors={"message_reference": {"_errors": [
                            {"code": "MESSAGE_REFERENCE_UNKNOWN_MESSAGE", "message": "Unknown message"}]}}))
                    extra.update(message_reference={"message_id": reference["message_id"], "channel_id": channel},
                                 type=19)
                if webhook:
                    extra["webhook_id"] = webhook
                message = api.add(channel, body.get("content", ""), author=author, **extra)
                if nonce is not None:
                    message["nonce"] = nonce
                    api.nonces[(channel, author["id"], str(nonce))] = message
                return self.reply(200, message, headers)

            def op_send(self, channel, _message_id, _emoji, call, headers):
                problem = self.check_message(call["body"] or {})
                if problem:
                    return self.reply(400, problem, headers)
                return self.created(channel, call["body"], BOT, headers=headers)

            def op_upload(self, channel, _message_id, _emoji, call, headers):
                body = call["body"] or {}
                problem = self.check_message(body, call["files"])
                if problem:
                    return self.reply(400, problem, headers)
                if any(item["size"] > 64 * 1024 for item in call["files"]):
                    return self.reply(413, error(40005, "Request entity too large"), headers)
                return self.created(channel, body, BOT, files=call["files"], headers=headers)

            def op_edit(self, channel, message_id, _emoji, call, headers):
                message = self.find(channel, message_id)
                if message is None:
                    return self.reply(404, error(10008, "Unknown Message"), headers)
                if message["author"]["id"] != BOT_ID:
                    return self.reply(403, error(50005, "Cannot edit a message authored by another user"), headers)
                body = call["body"] or {}
                if "content" in body:
                    message["content"] = body["content"]
                if "embeds" in body:
                    message["embeds"] = [dict(embed, type="rich") for embed in body["embeds"]]
                if "flags" in body:
                    message["flags"] = body["flags"]
                message["edited_timestamp"] = iso(int(time.time() * 1000))
                return self.reply(200, message, headers)

            def op_delete(self, channel, message_id, _emoji, _call, headers):
                message = self.find(channel, message_id)
                if message is None:
                    return self.reply(404, error(10008, "Unknown Message"), headers)
                api.channels[channel].remove(message)
                return self.reply(204, None, headers)

            def op_react(self, channel, message_id, emoji, _call, headers):
                message = self.find(channel, message_id)
                if message is None:
                    return self.reply(404, error(10008, "Unknown Message"), headers)
                name = unquote(emoji)
                if ":" in name:
                    label, ident = name.split(":", 1)
                    if ident != CUSTOM_EMOJI:
                        return self.reply(400, error(10014, "Unknown Emoji"), headers)
                    entry = {"id": ident, "name": label}
                else:
                    if not name or name.isascii():
                        return self.reply(400, error(10014, "Unknown Emoji"), headers)
                    entry = {"id": None, "name": name}
                reactions = message.setdefault("reactions", [])
                current = next((item for item in reactions if item["emoji"] == entry), None)
                if current is None:
                    reactions.append({"emoji": entry, "count": 1, "me": True})
                elif not current["me"]:
                    current.update(count=current["count"] + 1, me=True)
                return self.reply(204, None, headers)

            def op_read(self, channel, _message_id, _emoji, call, headers):
                query = call["query"]
                try:
                    limit = int(query.get("limit", ["50"])[0])
                except ValueError:
                    limit = 0
                if not 1 <= limit <= 100:
                    return self.reply(400, error(50035, "Invalid Form Body", errors={"limit": {"_errors": [
                        {"code": "NUMBER_TYPE_MAX", "message": "Must be between 1 and 100."}]}}), headers)
                ordered = sorted(api.channels[channel], key=lambda item: int(item["id"]))
                if "after" in query:
                    after = int(query["after"][0])
                    page = [item for item in ordered if int(item["id"]) > after][:limit]
                elif "before" in query:
                    before = int(query["before"][0])
                    page = [item for item in ordered if int(item["id"]) < before][-limit:]
                else:
                    page = ordered[-limit:]
                return self.reply(200, list(reversed(page)), headers)

            def webhook(self, method, ident, token, call):
                if method != "POST":
                    return self.reply(405, error(0, "405: Method Not Allowed"))
                if ident != WEBHOOK_ID:
                    return self.reply(404, error(10015, "Unknown Webhook"))
                if token != WEBHOOK_TOKEN:
                    return self.reply(401, error(50027, "Invalid Webhook Token"))
                if call["auth"] is not None:
                    return self.reply(400, error(0, "A webhook request never needs a bot token."))
                headers = self.bucket("webhook")
                body = call["body"] or {}
                problem = self.check_message(body)
                if problem:
                    return self.reply(400, problem, headers)
                name = body.get("username", "Fixture Hook")
                if "discord" in name.lower():
                    return self.reply(400, error(50035, "Invalid Form Body"), headers)
                thread = call["query"].get("thread_id", [None])[0]
                channel = thread or GENERAL
                if channel not in api.channels:
                    return self.reply(400, error(10003, "Unknown Channel"), headers)
                if call["query"].get("wait") != ["true"]:
                    api.add(channel, body.get("content", ""), author={"id": WEBHOOK_ID, "username": name, "bot": True},
                            webhook_id=WEBHOOK_ID)
                    return self.reply(204, None, headers)
                return self.created(channel, body, {"id": WEBHOOK_ID, "username": name, "global_name": None, "bot": True},
                                    headers=headers, webhook=WEBHOOK_ID)

            def do_GET(self):
                self.handle_any("GET")

            def do_POST(self):
                self.handle_any("POST")

            def do_PATCH(self):
                self.handle_any("PATCH")

            def do_DELETE(self):
                self.handle_any("DELETE")

            def do_PUT(self):
                self.handle_any("PUT")

        class Server(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                # A client that gave up (cancellation, deadline) closes its socket first: not a fixture fault.
                if isinstance(sys.exc_info()[1], OSError):
                    return
                super().handle_error(request, client_address)

        server = Server(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        ca = ""
        if tls:
            assert shutil.which("openssl"), "OpenSSL is required for the disposable HTTPS fixture"
            certificate, key = directory / "ca.pem", directory / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            ca = str(certificate)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        api.base_url = ("https" if tls else "http") + "://127.0.0.1:" + str(server.server_port)
        api.ca_file = ca
        api.kinds = lambda: [call["kind"] for call in api.calls]
        api.of = lambda kind: [call for call in api.calls if call["kind"] == kind]
        try:
            yield api
        finally:
            api.closing.set()
            api.release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()
