"""Bounded, cancellable and rate-limited HTTPS exchanges with the Discord REST API.

Standard library only. Each exchange runs in a helper thread so cancellation,
Stop and the activation deadline can shut the socket down at once. Discord's
per-route buckets (`X-RateLimit-*` headers) and 429 `retry_after` are tracked
process-wide per credential, so several nodes using one bot respect them
together. Reads are retried after HTTP 429, 5xx or a lost connection; writes
only after HTTP 429, which Discord did not process. Ambiguous writes are
reported, never replayed.
"""

import hashlib
import http.client
import json
import mimetypes
import os
from pathlib import Path
import secrets
import socket
import ssl
import stat
import threading
import time
from urllib.parse import urlsplit

from . import logic

CHUNK = 65536
THREAD_NAME = "discord-http"
_LIMIT_LOCK = threading.Lock()
_BUCKETS = {}
_ROUTES = {}
_GLOBAL = {}


class Failure(Exception):
    """A content-free transport outcome: code and the phase it happened in."""

    def __init__(self, code, phase):
        super().__init__(code)
        self.code, self.phase = code, phase


def reject_constant(_value):
    raise ValueError("Non-finite JSON number.")


def remaining(deadline, phase):
    left = deadline - time.monotonic()
    if left <= 0:
        raise Failure("timeout", phase)
    return max(0.001, left)


def bucket_identity(scope, route):
    """Routes sharing a Discord bucket hash share one limit for the same major parameter."""
    template, major = route
    return (scope, _ROUTES.get((scope, template), template), major)


def limit_wait(scope, route, now=None):
    """Seconds before this route may be called again (0.0 when free), from stored buckets."""
    with _LIMIT_LOCK:
        now = time.monotonic() if now is None else now
        until = max(_GLOBAL.get(scope, 0.0), _BUCKETS.get(bucket_identity(scope, route), 0.0))
        return max(0.0, until - now)


def limit_record(scope, route, headers, now=None, retry_after=None, global_limit=False):
    """Remember an exhausted bucket (remaining 0) or a 429 delay; at most 1024 entries are kept."""
    info = logic.rate_headers(headers)
    with _LIMIT_LOCK:
        now = time.monotonic() if now is None else now
        if len(_BUCKETS) >= 1024:
            for key in [key for key, until in _BUCKETS.items() if until <= now]:
                del _BUCKETS[key]
            if len(_BUCKETS) >= 1024:
                _BUCKETS.clear()
        if len(_ROUTES) >= 1024:
            _ROUTES.clear()
        if info["bucket"]:
            _ROUTES[(scope, route[0])] = info["bucket"]
        identity = bucket_identity(scope, route)
        if retry_after is not None:
            if global_limit or info["global"]:
                _GLOBAL[scope] = max(_GLOBAL.get(scope, 0.0), now + retry_after)
            else:
                _BUCKETS[identity] = max(_BUCKETS.get(identity, 0.0), now + retry_after)
        elif info["remaining"] == 0 and info["reset_after"] is not None:
            _BUCKETS[identity] = max(_BUCKETS.get(identity, 0.0), now + info["reset_after"])
        elif info["remaining"]:
            _BUCKETS.pop(identity, None)


def open_inside(root, parts, limit):
    """Walk from the canonical root with O_NOFOLLOW so no symlink can be swapped in."""
    flags = os.O_RDONLY | os.O_CLOEXEC
    try:
        directory = os.open(root, flags | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise Failure("file_not_allowed", "connect") from None
    try:
        for part in parts[:-1]:
            child = os.open(part, flags | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        raise Failure("file_not_found", "connect") from None
    except OSError:
        raise Failure("file_not_allowed", "connect") from None
    finally:
        os.close(directory)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise Failure("file_not_allowed", "connect")
    if info.st_size > limit or info.st_size <= 0:
        os.close(descriptor)
        raise Failure("file_too_large" if info.st_size > limit else "file_empty", "connect")
    return descriptor, info


def multipart(payload, upload, boundary):
    """payload_json then files[0]; returns (head, tail) around the streamed file bytes."""
    media = mimetypes.guess_type(upload["filename"])[0] or "application/octet-stream"
    head = ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            "Content-Type: application/json\r\n\r\n").encode("ascii") + payload + b"\r\n"
    head += ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\""
             + logic.multipart_name(upload["filename"]) + "\"\r\nContent-Type: " + media + "\r\n\r\n").encode("utf-8")
    tail = ("\r\n--" + boundary + "--\r\n").encode("ascii")
    return head, tail


def perform(state, origin, ca_file, method, path, headers, body, upload, deadline, limit):
    """One request/response without redirects, cookies, proxies or decompression."""
    parsed = urlsplit(origin)
    host, port = parsed.hostname, parsed.port
    opened, conn = None, None
    try:
        parts = [body] if body else []
        length = len(body or b"")
        if upload is not None:
            # Validate the local file before any connection; failures send nothing.
            opened = open_inside(upload["root"], upload["parts"], upload["max_bytes"])
            if opened[1].st_size != upload["size"]:
                raise Failure("file_changed", "connect")
            boundary = "BloxSmithDiscord" + secrets.token_hex(16)
            head, tail = multipart(body, upload, boundary)
            headers = {**headers, "Content-Type": "multipart/form-data; boundary=" + boundary}
            parts, length = [head, opened, tail], len(head) + opened[1].st_size + len(tail)
        if parsed.scheme == "https":
            context = ssl.create_default_context(cafile=ca_file or None)
            conn = http.client.HTTPSConnection(host, port or 443, timeout=remaining(deadline, "connect"), context=context)
        else:
            conn = http.client.HTTPConnection(host, port or 80, timeout=remaining(deadline, "connect"))
        state["conn"] = conn
        conn.connect()
        if state.get("aborted"):
            raise Failure("network", "connect")
        state["phase"] = "send"
        conn.putrequest(method, path, skip_host=False, skip_accept_encoding=True)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.putheader("Content-Length", str(length))
        conn.sock.settimeout(remaining(deadline, "send"))
        conn.endheaders()
        for part in parts:
            if isinstance(part, tuple):
                handle, info = part
                sent = 0
                while sent < info.st_size:
                    chunk = os.read(handle, min(CHUNK, info.st_size - sent))
                    if not chunk:
                        raise Failure("file_changed", "send")
                    conn.sock.settimeout(remaining(deadline, "send"))
                    conn.send(chunk)
                    sent += len(chunk)
                after = os.fstat(handle)
                if os.read(handle, 1) or (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                    raise Failure("file_changed", "send")
            else:
                conn.sock.settimeout(remaining(deadline, "send"))
                conn.send(part)
        state["phase"] = "receive"
        conn.sock.settimeout(remaining(deadline, "receive"))
        response = conn.getresponse()
        chunks, total = [], 0
        while total <= limit:
            if conn.sock is not None:
                conn.sock.settimeout(remaining(deadline, "receive"))
            piece = response.read1(min(CHUNK, limit + 1 - total))
            if not piece:
                break
            total += len(piece)
            chunks.append(piece)
        found = {key.lower(): value for key, value in response.getheaders()}
        retry = found.get("retry-after", "").strip()
        result = {"status": response.status, "headers": found, "body": None, "body_error": None,
                  "retry_after": logic.number(retry)}
        media = found.get("content-type", "").split(";", 1)[0].strip().lower()
        if total > limit:
            result["body_error"] = "too_large"
        elif found.get("content-encoding", "identity").strip().lower() not in ("identity", ""):
            result["body_error"] = "encoded"
        elif total == 0:
            result["body_error"] = "empty"
        elif media != "application/json":
            result["body_error"] = "not_json"
        else:
            try:
                result["body"] = json.loads(b"".join(chunks).decode("utf-8", errors="strict"),
                                            parse_constant=reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                result["body_error"] = "invalid_json"
        return result
    except Failure:
        raise
    except ssl.SSLCertVerificationError:
        raise Failure("tls", state["phase"]) from None
    except ssl.SSLError:
        raise Failure("tls" if state["phase"] == "connect" else "network", state["phase"]) from None
    except (socket.timeout, TimeoutError):
        raise Failure("timeout", state["phase"]) from None
    except (OSError, http.client.HTTPException, ValueError, AttributeError):
        raise Failure("network", state["phase"]) from None
    finally:
        if conn is not None:
            conn.close()
        if opened is not None:
            os.close(opened[0])


def abort(state):
    """Shut the socket down from the supervising thread; the helper thread then fails fast."""
    state["aborted"] = True
    conn = state.get("conn")
    sock = getattr(conn, "sock", None) if conn is not None else None
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def exchange(origin, ca_file, method, path, headers, body, upload, *, deadline, cancel, limit):
    """Run one request in a helper thread and supervise cancellation and the deadline."""
    state, box = {"phase": "connect", "conn": None}, {}

    def target():
        try:
            box["result"] = perform(state, origin, ca_file, method, path, headers, body, upload, deadline, limit)
        except Failure as failure:
            box["failure"] = failure
        except Exception:  # Defensive: an unexpected library error is a transport failure.
            box["failure"] = Failure("network", state["phase"])

    worker = threading.Thread(target=target, name=THREAD_NAME, daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(0.05)
        if not worker.is_alive():
            break
        if cancel():
            phase = state["phase"]
            abort(state)
            worker.join(2)
            raise Failure("cancelled", phase)
        if time.monotonic() >= deadline + 0.5:
            phase = state["phase"]
            abort(state)
            worker.join(2)
            raise Failure("timeout", phase)
    if "failure" in box:
        raise box["failure"]
    return box["result"]


class Session:
    """One activation or watch poll: one deadline, one credential, rate-limited and bounded requests.

    `secret` is the bot token (sent as `Authorization: Bot ...`) or, for a webhook, its token,
    which only appears in the request path. Neither is ever part of a result or error.
    """

    def __init__(self, config, secret, cancel=lambda: False, *, bot=True):
        self.config, self.secret, self.bot = config, secret, bot
        self.cancel = cancel if callable(cancel) else (lambda: False)
        self.deadline = time.monotonic() + config["timeout_sec"]
        self.secrets = [secret]
        self.requests = 0
        # Buckets are keyed by a digest of the credential; the credential itself is never stored.
        self.scope = (config["api_base_url"], hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16])

    def pause(self, seconds):
        """Cancellation-aware wait before a rate-limited request or a retry."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.cancel():
                logic.fail("cancelled", logic.MESSAGES["cancelled_before_send"])
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))

    def wait_bucket(self, route):
        """Respect an exhausted bucket before sending; a wait above the bounds is reported instead."""
        wait = limit_wait(self.scope, route)
        if wait <= 0:
            return
        if wait > self.config["max_retry_wait_sec"] or time.monotonic() + wait > self.deadline - 0.2:
            logic.fail("rate_limited", "The Discord rate-limit bucket of this route is exhausted; nothing was sent.",
                       retry_after=round(wait, 3))
        self.pause(wait)

    def send(self, method, path, headers, body, upload, *, mutation):
        """One exchange mapped to a structured outcome; never raises a raw library error."""
        if self.cancel():
            logic.fail("cancelled", logic.MESSAGES["cancelled_before_send"])
        if self.deadline - time.monotonic() <= 0.2:
            logic.fail("timeout")
        self.requests += 1
        try:
            return exchange(self.config["api_base_url"], self.config["ca_file"], method, path, headers, body, upload,
                            deadline=self.deadline, cancel=self.cancel, limit=self.config["max_response_bytes"])
        except Failure as failure:
            after_send = failure.phase == "receive"
            uncertain = mutation and after_send
            if failure.code in ("tls", "file_not_found", "file_not_allowed", "file_too_large", "file_empty",
                                "file_changed"):
                logic.fail(failure.code)
            if failure.code == "timeout":
                logic.fail("timeout", logic.MESSAGES["timeout_uncertain" if uncertain else "timeout"], uncertain=uncertain)
            if failure.code == "cancelled":
                logic.fail("cancelled", logic.MESSAGES["cancelled" if uncertain else "cancelled_before_send"],
                           uncertain=uncertain)
            logic.fail("network", logic.MESSAGES["network_uncertain" if uncertain else "network"], uncertain=uncertain,
                       retryable=not mutation)

    def request(self, method, path, route, *, query=None, payload=None, upload=None, mutation, empty=False):
        """Call <api_base_url><path>[?query]; return the JSON body (None for an accepted 204) or raise a coded error."""
        target = path + ("?" + query if query else "")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": logic.USER_AGENT,
                   "Connection": "close"}
        if self.bot:
            headers["Authorization"] = "Bot " + self.secret
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if upload is None:
                headers["Content-Type"] = "application/json"
        attempts = 0
        while True:
            attempts += 1
            try:
                self.wait_bucket(route)
                outcome = self.send(method, target, headers, body, upload, mutation=mutation)
                status = outcome["status"]
                if status == 429:
                    document = outcome["body"] if isinstance(outcome["body"], dict) else {}
                    wait = logic.number(document.get("retry_after"))
                    wait = wait if wait is not None else (outcome["retry_after"] if outcome["retry_after"] is not None else 1.0)
                    limit_record(self.scope, route, outcome["headers"], retry_after=wait,
                                 global_limit=document.get("global") is True)
                else:
                    limit_record(self.scope, route, outcome["headers"])
                if 200 <= status < 300:
                    if empty and status == 204:
                        return None
                    if outcome["body_error"] or outcome["body"] is None:
                        logic.fail("protocol", logic.MESSAGES["protocol_uncertain"] if mutation else None,
                                   uncertain=mutation)
                    return outcome["body"]
                if status >= 500 and outcome["body_error"]:
                    logic.fail("provider_unavailable", logic.MESSAGES["provider_unavailable_uncertain"] if mutation else None,
                               uncertain=mutation, http_status=status, retryable=True)
                scope = logic.rate_headers(outcome["headers"])["scope"]
                logic.provider_failure(status, outcome["body"], self.secrets, mutation=mutation,
                                       retry_after=outcome["retry_after"], scope=scope)
            except logic.DiscordError as exc:
                exc.attempts = attempts
                wait = logic.retry_delay(exc, attempts, self.config, self.deadline - time.monotonic(), mutation=mutation)
                if wait is None:
                    raise
                if exc.code != "rate_limited":
                    self.pause(wait)
                # A 429 wait is recorded in the bucket table: wait_bucket() honours it before the retry.


def locate(path_text, config):
    """Resolve an input path strictly inside file_root, refusing links and special files."""
    if not config["file_root"]:
        logic.fail("file_root_required")
    configured = Path(os.path.normpath(config["file_root"]))
    try:
        root = configured.resolve(strict=True)
    except (OSError, RuntimeError):
        logic.fail("file_root_required", "file_root does not exist or cannot be read.")
    if not root.is_dir() or root == Path(root.anchor):
        logic.fail("file_root_required", "file_root must be an existing dedicated directory, not a filesystem root.")
    candidate = Path(path_text)
    if any(part in ("..", ".") for part in candidate.parts):
        logic.fail("file_not_allowed")
    if candidate.is_absolute():
        normalized = Path(os.path.normpath(candidate))
        for base in (configured, root):
            try:
                relative = normalized.relative_to(base)
                break
            except ValueError:
                continue
        else:
            logic.fail("file_not_allowed")
    else:
        relative = candidate
    parts = [part for part in relative.parts if part not in ("", "/")]
    if not parts:
        logic.fail("file_not_allowed")
    current, info = root, None
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            logic.fail("file_not_found")
        except OSError:
            logic.fail("file_not_allowed")
        last = index == len(parts) - 1
        if stat.S_ISLNK(info.st_mode) or (not last and not stat.S_ISDIR(info.st_mode)):
            logic.fail("file_not_allowed")
        if last and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            logic.fail("file_not_allowed")
    if info.st_size > config["max_file_bytes"]:
        logic.fail("file_too_large")
    if info.st_size <= 0:
        logic.fail("file_empty")
    return {"root": str(root), "parts": parts, "size": info.st_size, "name": current.name}
