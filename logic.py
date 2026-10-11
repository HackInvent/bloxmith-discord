"""Pure Discord REST API v10 contracts: settings, requests, bodies, provider envelopes, normalization and errors.

No network, filesystem, framework or third-party import happens here. Every
function is deterministic, so the policy can be checked directly. Credentials
(bot token, webhook URL) are validated here but never stored in a result.
"""

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import re
from urllib.parse import quote, urlsplit

OFFICIAL_API = "https://discord.com"
API_PREFIX = "/api/v10"
WEBHOOK_HOSTS = ("discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com")
DISCORD_EPOCH_MS = 1420070400000
USER_AGENT = "DiscordBot (https://github.com/HackInvent/bloxmith-discord, 0.1.0)"

DEFAULTS = {
    "enabled": False, "secret_ref": "", "webhook_secret_ref": "", "allowed_channels": [], "default_channel": "",
    "default_operation": "send_message", "mentions": "none", "allow_writes": False, "allow_delete": False,
    "file_root": "", "max_file_bytes": 10485760, "read_limit": 100,
    "watch": False, "watch_channels": [], "watch_interval_sec": 30, "emit_existing": False,
    "existing_since_hours": 1, "watch_max_items": 100, "watch_include_bots": False,
    "page_size": 100, "max_pages": 5, "timeout_sec": 30, "max_retries": 2, "max_retry_wait_sec": 30,
    "max_response_bytes": 4194304, "api_base_url": OFFICIAL_API, "ca_file": "",
}
FRAMEWORK_KEYS = {"execution", "runtime_path", "runtime_path_label"}
BOUNDS = {
    "max_file_bytes": (1024, 104857600), "read_limit": (1, 1000), "watch_interval_sec": (10, 86400),
    "existing_since_hours": (1, 168), "watch_max_items": (1, 1000), "page_size": (1, 100), "max_pages": (1, 20),
    "timeout_sec": (5, 300), "max_retries": (0, 5), "max_retry_wait_sec": (1, 120),
    "max_response_bytes": (65536, 33554432),
}
OPERATIONS = ("send_message", "edit_message", "delete_message", "read_messages", "add_reaction", "upload_file",
              "webhook_send", "watch")
DEFAULT_OPERATIONS = ("send_message", "webhook_send")
WRITES = {"send_message", "edit_message", "delete_message", "add_reaction", "upload_file", "webhook_send"}
BOT_OPERATIONS = {"send_message", "edit_message", "delete_message", "read_messages", "add_reaction", "upload_file",
                  "watch"}
MENTION_POLICIES = {"none": (), "users": ("users",), "users_roles": ("users", "roles"),
                    "all": ("users", "roles", "everyone")}
MESSAGE_KEYS = {
    "send_message": {"operation", "channel_id", "content", "embeds", "reply_to", "allowed_mentions",
                     "mention_replied_user", "silent", "suppress_embeds", "request_id"},
    "edit_message": {"operation", "channel_id", "message_id", "content", "embeds", "allowed_mentions",
                     "suppress_embeds", "request_id"},
    "delete_message": {"operation", "channel_id", "message_id", "request_id"},
    "read_messages": {"operation", "channel_id", "limit", "before", "after", "request_id"},
    "add_reaction": {"operation", "channel_id", "message_id", "emoji", "request_id"},
    "webhook_send": {"operation", "content", "embeds", "username", "avatar_url", "thread_id", "allowed_mentions",
                     "silent", "suppress_embeds", "request_id"},
    "watch": {"operation", "request_id"},
}
FILE_KEYS = {"operation", "path", "channel_id", "content", "filename", "description", "spoiler", "reply_to",
             "allowed_mentions", "mention_replied_user", "silent", "request_id"}
SECRET_REF = re.compile(r"secret://(?:workspace/[A-Za-z0-9_.-]{1,80}|project/[A-Za-z0-9_.-]{1,80}/[A-Za-z0-9_.-]{1,80})\Z")
BOT_TOKEN = re.compile(r"[A-Za-z0-9_.-]{20,256}\Z")
WEBHOOK_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}\Z")
WEBHOOK_PATH = re.compile(r"/api(?:/v[0-9]{1,2})?/webhooks/([1-9][0-9]{0,19})/([A-Za-z0-9_-]{20,128})/?\Z")
SNOWFLAKE = re.compile(r"[1-9][0-9]{0,19}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
CUSTOM_EMOJI = re.compile(r"(?:<a?:([A-Za-z0-9_]{2,32}):([1-9][0-9]{0,19})>|([A-Za-z0-9_]{2,32}):([1-9][0-9]{0,19}))\Z")
BUCKET = re.compile(r"[A-Za-z0-9:_-]{1,64}\Z")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z")
FILE_NAME = re.compile(r"[^/\\\x00-\x1f\x7f]{1,255}\Z")
CONTENT_LIMIT = 2000
READ_CONTENT_LIMIT = 4000
EMBED_LIMIT = 10
EMBED_TOTAL = 6000
EMBED_KEYS = {"title", "description", "url", "timestamp", "color", "footer", "image", "thumbnail", "author", "fields"}
INPUT_LIMIT = 262144
REQUEST_LIMIT = 262144
OUTPUT_LIMIT = 16 * 1024 * 1024
STATE_WATCHES = 16
STATE_CHANNELS = 50
FLAG_SUPPRESS_EMBEDS = 1 << 2
FLAG_SUPPRESS_NOTIFICATIONS = 1 << 12

MESSAGES = {
    "config": "Invalid Discord settings.",
    "disabled": "Remote access is disabled; enable the block explicitly before calling Discord.",
    "secret": "Unlock the wallet and store a Discord bot token under the configured secret_ref.",
    "webhook_secret": "Unlock the wallet and store a Discord webhook URL under the configured webhook_secret_ref.",
    "invalid_input": "The input does not follow the documented Discord request contract; nothing was sent.",
    "request_too_large": "The Discord request body exceeds 256 KiB; nothing was sent.",
    "one_operation": "Send one message request or one file per activation; nothing was sent.",
    "channel_required": "No channel_id was given and no default_channel is configured; nothing was sent.",
    "channel_not_allowed": "The channel is not listed in allowed_channels; nothing was sent.",
    "writes_disabled": "Sending, editing, reacting and uploading need Allow writes; nothing was sent.",
    "delete_disabled": "Deleting a message needs Allow delete; nothing was deleted.",
    "watch_disabled": "Enable Watch channels before sending a watch request; nothing was polled.",
    "bot_not_configured": "This operation needs a bot token reference (secret_ref); nothing was sent.",
    "webhook_not_configured": "webhook_send needs a webhook URL reference (webhook_secret_ref); nothing was sent.",
    "mentions_not_allowed": "The requested mentions exceed the Mentions setting; nothing was sent.",
    "file_root_required": "Configure file_root before uploading local files; nothing was sent.",
    "file_not_found": "The file does not exist inside file_root; nothing was sent.",
    "file_not_allowed": "The file is outside file_root, is not a single-link regular file or passes through a symbolic link.",
    "file_too_large": "The file exceeds max_file_bytes; nothing was sent.",
    "file_empty": "The file is empty; nothing was sent.",
    "file_changed": "The file changed while it was uploaded; the upload was abandoned.",
    "bad_request": "Discord rejected the request (HTTP 400); nothing was changed.",
    "unauthorized": "Discord rejected the credential (HTTP 401); check the wallet secret.",
    "forbidden": "The bot lacks access or permission for this channel or message (HTTP 403).",
    "not_found": "Discord could not find the channel, message, emoji or webhook (HTTP 404).",
    "provider_too_large": "Discord refused the request as too large (HTTP 413); nothing was sent.",
    "rate_limited": "Discord rate limit reached (HTTP 429); retry after the indicated delay.",
    "provider_error": "Discord returned an unexpected error; nothing is assumed changed.",
    "provider_unavailable": "Discord is temporarily unavailable (HTTP 5xx).",
    "provider_unavailable_uncertain": "Discord is temporarily unavailable (HTTP 5xx); the outcome of the write is uncertain.",
    "network": "The Discord API could not be reached; nothing was sent.",
    "network_uncertain": "The connection failed after the request was sent; the outcome is uncertain.",
    "tls": "TLS verification failed for the Discord API origin; nothing was sent.",
    "timeout": "The Discord operation exceeded timeout_sec before the request was complete; nothing was sent.",
    "timeout_uncertain": "The Discord operation exceeded timeout_sec after the request was sent; the outcome is uncertain.",
    "cancelled": "The operation was cancelled after the request was sent; the outcome is uncertain.",
    "cancelled_before_send": "The operation was cancelled before any request was sent.",
    "protocol": "Discord returned an invalid, compressed or oversized response.",
    "protocol_uncertain": "Discord accepted the request but returned an unexpected receipt; the change is likely.",
    "output_too_large": "One page of messages exceeds the 16 MiB output limit; lower page_size.",
    "storage": "The watch cursor needs the node storage of a saved project.",
    "busy": "Another watcher of this node holds the cursor; nothing was polled.",
    "state_invalid": "The stored watch state is unreadable; nothing was polled.",
    "internal": "The Discord operation failed without a complete result.",
}


class DiscordError(ValueError):
    """A content-free diagnostic with a stable machine code and an English message."""

    def __init__(self, code, detail=None, *, uncertain=False, http_status=None, retry_after=None,
                 provider_code=None, provider_message=None, provider_errors=None, scope=None, retryable=False):
        super().__init__(detail or MESSAGES.get(code, MESSAGES["internal"]))
        self.code, self.uncertain = code, bool(uncertain)
        self.http_status, self.retry_after = http_status, retry_after
        self.provider_code, self.provider_message = provider_code, provider_message
        self.provider_errors, self.scope, self.retryable = provider_errors, scope, retryable
        self.attempts = 0

    def envelope(self, **context):
        """Return the public error document; callers add only identifiers, never content."""
        result = {"code": self.code, "message": str(self), "uncertain": self.uncertain}
        for key, value in (("http_status", self.http_status), ("retry_after", self.retry_after),
                           ("provider_code", self.provider_code), ("provider_message", self.provider_message),
                           ("provider_errors", self.provider_errors), ("rate_limit_scope", self.scope)):
            if value is not None:
                result[key] = value
        if self.attempts:
            result["attempts"] = self.attempts
        for key, value in context.items():
            if value is not None:
                result[key] = value
        return result


def fail(code, detail=None, **extra):
    """Raise a structured failure before any side effect."""
    raise DiscordError(code, detail, **extra)


def thaw(value):
    """Turn immutable preparation snapshots back into plain JSON containers."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


def encoded(value, limit=INPUT_LIMIT, code="invalid_input"):
    """Serialize strict finite JSON with bounded depth, element count and bytes."""
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 32 or count > 50000:
            fail(code, "JSON exceeds its depth or element limit.")
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                fail(code, "JSON object keys must be strings.")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, int, float, bool):
            fail(code, "A strict JSON value is required.")
        elif type(item) is float and not math.isfinite(item):
            fail(code, "Non-finite numbers are not accepted.")
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        fail(code, "Finite UTF-8 JSON is required.")
    if len(data) > limit:
        fail(code, "JSON exceeds its byte limit.")
    return data


def strict_json(text, limit=INPUT_LIMIT, code="invalid_input"):
    """Parse JSON text, rejecting duplicate keys, non-finite numbers and oversized input."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8", errors="strict")
        except UnicodeError:
            fail(code, "UTF-8 JSON is required.")
    if not isinstance(text, str):
        fail(code, "JSON text is required.")
    if len(text.encode("utf-8", errors="replace")) > limit:
        fail(code, "JSON exceeds its byte limit.")

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                fail(code, "Duplicate JSON property.")
            result[key] = item
        return result

    def constant(_name):
        fail(code, "Non-finite numbers are not accepted.")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except DiscordError:
        raise
    except (ValueError, RecursionError):
        fail(code, "Malformed JSON.")
    encoded(value, limit, code)
    return value


def boolean(value, name, code="config"):
    if type(value) is not bool:
        fail(code, f"{name} must be a boolean.")
    return value


def integer(value, name, low, high, code="config"):
    if type(value) is not int or not low <= value <= high:
        fail(code, f"{name} must be an integer between {low} and {high}.")
    return value


def snowflake(value, name, code="invalid_input"):
    """A Discord ID (snowflake): decimal text, or a JSON integer, below 2^64."""
    if type(value) is int and 0 < value < 2 ** 64:
        return str(value)
    if isinstance(value, str) and SNOWFLAKE.fullmatch(value.strip()) and int(value.strip()) < 2 ** 64:
        return value.strip()
    fail(code, f"{name} must be a Discord ID (snowflake), such as 1234567890123456789.")


def optional_snowflake(document, key):
    value = document.get(key)
    return None if value is None else snowflake(value, key)


def snowflake_at(ms):
    """The smallest snowflake of a Unix time in milliseconds, usable as an `after` position."""
    return str(max(0, int(ms) - DISCORD_EPOCH_MS) << 22)


def snowflake_time(ident):
    """ISO 8601 UTC creation time of a snowflake, or None."""
    if not isinstance(ident, str) or not SNOWFLAKE.fullmatch(ident):
        return None
    ms = (int(ident) >> 22) + DISCORD_EPOCH_MS
    stamp = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


def path_text(value, name, code="config"):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        fail(code, f"{name} must be a bounded path without control characters.")
    value = value.strip()
    if value and (not value.startswith("/") or ".." in value.split("/")):
        fail(code, f"{name} must be empty or an absolute path without '..' components.")
    return value


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def split_origin(value, name="api_base_url", code="config"):
    """Parse a bare origin and return (scheme, host, port); no path, query, fragment or credentials."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        fail(code, f"{name} must be an ASCII URL without spaces.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        fail(code, f"{name} has an invalid host or port.")
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in ("https", "http") or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or "?" in value or "#" in value
            or parsed.path not in ("", "/") or "\\" in value or "%" in host):
        fail(code, f"{name} must be a bare https:// origin without credentials, path, query or fragment.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or host.endswith(".") or any(not HOST_LABEL.fullmatch(label) for label in host.split(".")):
            fail(code, f"{name} has an invalid host name.")
    if port is not None and not 1 <= port <= 65535:
        fail(code, f"{name} has an invalid port.")
    return parsed.scheme, host, port


def origin_text(scheme, host, port):
    netloc = "[" + host + "]" if ":" in host else host
    return scheme + "://" + netloc + (":" + str(port) if port is not None else "")


def api_origin(value):
    """The official https://discord.com origin, or a loopback test double (HTTP or HTTPS)."""
    scheme, host, port = split_origin(value)
    if is_loopback(host):
        return origin_text(scheme, host, port)
    if scheme != "https" or host != "discord.com" or port not in (None, 443):
        fail("config", "api_base_url must stay https://discord.com; only a loopback test host may replace it.")
    return OFFICIAL_API


def secret_reference(value, name):
    if not isinstance(value, str) or len(value) > 256:
        fail("config", f"{name} must be a wallet reference such as secret://workspace/discord_bot.")
    value = value.strip()
    if value and not SECRET_REF.fullmatch(value):
        fail("config", f"{name} must be a wallet reference such as secret://workspace/discord_bot, never the credential.")
    return value


def channel_list(value, name, limit):
    if not isinstance(value, list) or len(value) > limit:
        fail("config", f"{name} must be a JSON array of at most {limit} Discord channel IDs.")
    channels = [snowflake(item, name, "config") for item in value]
    if len(set(channels)) != len(channels):
        fail("config", f"{name} must not contain duplicates.")
    return channels


def configuration(raw):
    """Pure settings validation; no wallet, filesystem or network access."""
    raw = thaw(raw)
    if not isinstance(raw, Mapping):
        fail("config", "Settings must be a JSON object.")
    unknown = set(raw) - set(DEFAULTS) - FRAMEWORK_KEYS
    if unknown:
        fail("config", "Unknown setting: " + sorted(unknown)[0] + ". Credentials belong in the wallet.")
    config = {**{key: thaw(value) for key, value in DEFAULTS.items()},
              **{key: value for key, value in raw.items() if key in DEFAULTS}}
    for key in ("enabled", "allow_writes", "allow_delete", "watch", "emit_existing", "watch_include_bots"):
        boolean(config[key], key)
    for key, (low, high) in BOUNDS.items():
        integer(config[key], key, low, high)
    config["secret_ref"] = secret_reference(config["secret_ref"], "secret_ref")
    config["webhook_secret_ref"] = secret_reference(config["webhook_secret_ref"], "webhook_secret_ref")
    config["allowed_channels"] = channel_list(config["allowed_channels"], "allowed_channels", 100)
    config["watch_channels"] = channel_list(config["watch_channels"], "watch_channels", 20)
    if any(item not in config["allowed_channels"] for item in config["watch_channels"]):
        fail("config", "watch_channels must also be listed in allowed_channels.")
    default = config["default_channel"]
    if default in ("", None):
        config["default_channel"] = None
    else:
        config["default_channel"] = snowflake(default, "default_channel", "config")
        if config["default_channel"] not in config["allowed_channels"]:
            fail("config", "default_channel must also be listed in allowed_channels.")
    if config["default_operation"] not in DEFAULT_OPERATIONS:
        fail("config", "default_operation must be send_message or webhook_send.")
    if config["mentions"] not in MENTION_POLICIES:
        fail("config", "mentions must be none, users, users_roles or all.")
    if config["allow_delete"] and not config["allow_writes"]:
        fail("config", "allow_delete needs allow_writes.")
    config["file_root"] = path_text(config["file_root"], "file_root")
    config["ca_file"] = path_text(config["ca_file"], "ca_file")
    config["api_base_url"] = api_origin(config["api_base_url"])
    if config["enabled"]:
        if not config["secret_ref"] and not config["webhook_secret_ref"]:
            fail("config", "Remote access requires secret_ref (bot token) or webhook_secret_ref (webhook URL).")
        if config["secret_ref"] and not config["allowed_channels"]:
            fail("config", "A bot token requires at least one channel ID in allowed_channels.")
        if config["default_operation"] == "send_message" and not config["secret_ref"]:
            fail("config", "default_operation send_message needs secret_ref; choose webhook_send for a webhook only.")
        if config["default_operation"] == "webhook_send" and not config["webhook_secret_ref"]:
            fail("config", "default_operation webhook_send needs webhook_secret_ref.")
    if config["watch"] and config["enabled"] and not config["secret_ref"]:
        fail("config", "Watch channels needs secret_ref: a webhook cannot read messages.")
    if config["watch"] and not config["watch_channels"] and len(config["allowed_channels"]) > 20:
        fail("config", "Watch at most 20 channels: list them in watch_channels.")
    return config


def watched(config):
    """The channels the watch polls: watch_channels, or every allowlisted channel (at most 20)."""
    return list(config["watch_channels"] or config["allowed_channels"])[:20]


def credential(value, key="token"):
    """Accept bare text or {"<key>": "..."}; return the stripped wallet value, never echoed."""
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            document = json.loads(value)
        except ValueError:
            return None
        value = document.get(key) if isinstance(document, dict) else None
    return value.strip() if isinstance(value, str) else None


def bot_token(value):
    """Validate the wallet bot token before it reaches a header; an optional 'Bot ' prefix is removed."""
    token = credential(value)
    if token is not None and token.startswith("Bot "):
        token = token[4:].strip()
    if token is None or not BOT_TOKEN.fullmatch(token):
        fail("secret", "The wallet secret is not a Discord bot token (20-256 letters, digits, '.', '_' or '-').")
    return token


def webhook(value, config):
    """Parse the wallet webhook URL into (id, token); its origin must be Discord or the loopback test origin.

    Only the ID and token are kept: requests always go to the configured API origin, so a wallet
    value can never redirect them to another host.
    """
    text = credential(value, "url")
    if text is None or len(text) > 512 or any(ord(char) <= 32 or ord(char) >= 127 for char in text):
        fail("webhook_secret")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        fail("webhook_secret")
    host = (parsed.hostname or "").lower()
    match = WEBHOOK_PATH.fullmatch(parsed.path or "")
    if (match is None or parsed.username is not None or parsed.password is not None or parsed.query
            or parsed.fragment or int(match.group(1)) >= 2 ** 64):
        fail("webhook_secret", "The wallet secret is not a Discord webhook URL (https://discord.com/api/webhooks/ID/TOKEN).")
    api_scheme, api_host, api_port = split_origin(config["api_base_url"])
    if is_loopback(api_host):
        allowed = (parsed.scheme, host, port) == (api_scheme, api_host, api_port)
    else:
        allowed = parsed.scheme == "https" and host in WEBHOOK_HOSTS and port in (None, 443)
    if not allowed:
        fail("webhook_secret", "The webhook URL must be on discord.com; nothing was sent.")
    return match.group(1), match.group(2)


def redact(text, secrets=()):
    """Mask the credentials wherever they could appear."""
    text = "" if text is None else str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def clean(value, limit):
    """Bounded single-line provider text with control characters removed."""
    if not isinstance(value, str):
        return None
    value = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value).strip()
    return value[:limit] if value else None


def identifier(value):
    if value is None:
        return None
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        fail("invalid_input", "request_id needs 1-128 ASCII letters, digits or ._:- characters.")
    return value


def structured(value, primary):
    """Read a JSON object, or plain text as {primary: text}; '{' must start valid JSON."""
    if isinstance(value, Mapping):
        document = thaw(value)
        encoded(document)
        return document
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeError:
            fail("invalid_input", "Inputs must be UTF-8 text or a JSON object.")
    if not isinstance(value, str):
        fail("invalid_input", "Inputs must be UTF-8 text or a JSON object.")
    if len(value.encode("utf-8", errors="replace")) > INPUT_LIMIT:
        fail("invalid_input", "The input exceeds 256 KiB.")
    if value.lstrip().startswith("{"):
        document = strict_json(value.strip())
        if not isinstance(document, dict):
            fail("invalid_input", "A value starting with '{' must be one JSON object.")
        return document
    return {primary: value}


def flag(document, key):
    value = document.get(key, False)
    if type(value) is not bool:
        fail("invalid_input", f"{key} must be a JSON boolean.")
    return value


def text_value(value, name, limit, required):
    if value is None and not required:
        return None
    if not isinstance(value, str) or "\x00" in value or len(value) > limit or (required and not value.strip()):
        fail("invalid_input", f"{name} must be text of 1-{limit} characters.")
    return value if value.strip() else None


def url_value(value, name, schemes=("https", "http"), attachment=False):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        fail("invalid_input", f"{name} must be a URL without spaces.")
    if attachment and value.startswith("attachment://") and FILE_NAME.fullmatch(value[13:]):
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        fail("invalid_input", f"{name} must be a valid URL.")
    if parsed.scheme not in schemes or not parsed.netloc:
        fail("invalid_input", f"{name} must be an {' or '.join(schemes)} URL.")
    return value


def embed_value(item, index):
    """One embed with Discord's documented field limits; unknown keys are refused."""
    where = f"embeds[{index}]"
    if not isinstance(item, dict) or not item:
        fail("invalid_input", f"{where} must be a non-empty JSON object.")
    unknown = set(item) - EMBED_KEYS
    if unknown:
        fail("invalid_input", f"Unknown {where} field: {sorted(unknown)[0]}.")
    result, total = {}, 0
    for key, limit in (("title", 256), ("description", 4096)):
        if key in item:
            result[key] = text_value(item[key], f"{where}.{key}", limit, True)
            total += len(result[key])
    if "url" in item:
        result["url"] = url_value(item["url"], f"{where}.url")
    if "timestamp" in item:
        if not isinstance(item["timestamp"], str) or not TIMESTAMP.fullmatch(item["timestamp"]):
            fail("invalid_input", f"{where}.timestamp must be an ISO 8601 date-time such as 2026-01-02T10:00:00Z.")
        result["timestamp"] = item["timestamp"]
    if "color" in item:
        result["color"] = integer(item["color"], f"{where}.color", 0, 0xFFFFFF, "invalid_input")
    for key in ("image", "thumbnail"):
        if key in item:
            part = item[key]
            if not isinstance(part, dict) or set(part) != {"url"}:
                fail("invalid_input", f"{where}.{key} must be {{\"url\": ...}}.")
            result[key] = {"url": url_value(part["url"], f"{where}.{key}.url", attachment=True)}
    if "footer" in item:
        part = item["footer"]
        if not isinstance(part, dict) or "text" not in part or set(part) - {"text", "icon_url"}:
            fail("invalid_input", f"{where}.footer must be {{\"text\", \"icon_url\"}}.")
        result["footer"] = {"text": text_value(part["text"], f"{where}.footer.text", 2048, True)}
        total += len(result["footer"]["text"])
        if "icon_url" in part:
            result["footer"]["icon_url"] = url_value(part["icon_url"], f"{where}.footer.icon_url", attachment=True)
    if "author" in item:
        part = item["author"]
        if not isinstance(part, dict) or "name" not in part or set(part) - {"name", "url", "icon_url"}:
            fail("invalid_input", f"{where}.author must be {{\"name\", \"url\", \"icon_url\"}}.")
        result["author"] = {"name": text_value(part["name"], f"{where}.author.name", 256, True)}
        total += len(result["author"]["name"])
        for key in ("url", "icon_url"):
            if key in part:
                result["author"][key] = url_value(part[key], f"{where}.author.{key}", attachment=key == "icon_url")
    if "fields" in item:
        fields = item["fields"]
        if not isinstance(fields, list) or len(fields) > 25:
            fail("invalid_input", f"{where}.fields must be an array of at most 25 fields.")
        result["fields"] = []
        for position, field in enumerate(fields):
            name = f"{where}.fields[{position}]"
            if not isinstance(field, dict) or not {"name", "value"} <= set(field) or set(field) - {"name", "value", "inline"}:
                fail("invalid_input", f"{name} must be {{\"name\", \"value\", \"inline\"}}.")
            entry = {"name": text_value(field["name"], name + ".name", 256, True),
                     "value": text_value(field["value"], name + ".value", 1024, True)}
            if "inline" in field:
                entry["inline"] = boolean(field["inline"], name + ".inline", "invalid_input")
            total += len(entry["name"]) + len(entry["value"])
            result["fields"].append(entry)
    return result, total


def embeds_value(value):
    """At most 10 embeds and 6000 characters of embed text in total, as Discord documents."""
    if value is None:
        return None
    if isinstance(value, str):
        value = strict_json(value)
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not 1 <= len(value) <= EMBED_LIMIT:
        fail("invalid_input", "embeds must be a JSON array of 1-10 embed objects.")
    result, total = [], 0
    for index, item in enumerate(value):
        embed, size = embed_value(item, index)
        result.append(embed)
        total += size
    if total > EMBED_TOTAL:
        fail("invalid_input", "The embeds exceed Discord's 6000-character total.")
    return result


def mentions(document, config):
    """The allowed_mentions object sent with a message, never wider than the Mentions setting.

    Discord pings @everyone, @here, roles and users found in the content unless
    allowed_mentions restricts them; the block always sends an explicit object.
    """
    policy = MENTION_POLICIES[config["mentions"]]
    raw = document.get("allowed_mentions")
    replied = flag(document, "mention_replied_user")
    if raw is None:
        result = {"parse": list(policy)}
    else:
        if not isinstance(raw, dict) or set(raw) - {"parse", "users", "roles", "replied_user"}:
            fail("invalid_input", "allowed_mentions must be {\"parse\", \"users\", \"roles\", \"replied_user\"}.")
        parse = raw.get("parse", [])
        if not isinstance(parse, list) or len(set(parse)) != len(parse) or any(
                item not in ("users", "roles", "everyone") for item in parse):
            fail("invalid_input", "allowed_mentions.parse must list users, roles or everyone at most once.")
        if any(item not in policy for item in parse):
            fail("mentions_not_allowed")
        result = {"parse": list(parse)}
        for key, kind in (("users", "users"), ("roles", "roles")):
            items = raw.get(key)
            if items is None:
                continue
            if not isinstance(items, list) or len(items) > 100:
                fail("invalid_input", f"allowed_mentions.{key} must be an array of at most 100 IDs.")
            if items and kind not in policy:
                fail("mentions_not_allowed")
            if items and kind in parse:
                fail("invalid_input", f"allowed_mentions cannot list {key} and parse {kind} together.")
            ids = [snowflake(item, "allowed_mentions." + key) for item in items]
            if len(set(ids)) != len(ids):
                fail("invalid_input", f"allowed_mentions.{key} must not contain duplicates.")
            if ids:
                result[key] = ids
        if "replied_user" in raw:
            replied = replied or boolean(raw["replied_user"], "allowed_mentions.replied_user", "invalid_input")
    if replied and not policy:
        fail("mentions_not_allowed")
    result["replied_user"] = replied
    return result


def flags(document, *, silent):
    value = FLAG_SUPPRESS_EMBEDS if flag(document, "suppress_embeds") else 0
    if silent and flag(document, "silent"):
        value |= FLAG_SUPPRESS_NOTIFICATIONS
    return value


def target_channel(document, config):
    raw = document.get("channel_id")
    value = config["default_channel"] if raw is None else snowflake(raw, "channel_id")
    if value is None:
        fail("channel_required")
    if value not in config["allowed_channels"]:
        fail("channel_not_allowed")
    return value


def gate(operation, config):
    """Switches checked before any wallet access: writes, delete, watch and the configured credentials."""
    if operation in WRITES and not config["allow_writes"]:
        fail("writes_disabled")
    if operation == "delete_message" and not config["allow_delete"]:
        fail("delete_disabled")
    if operation == "watch" and not config["watch"]:
        fail("watch_disabled")
    if operation in BOT_OPERATIONS and not config["secret_ref"]:
        fail("bot_not_configured")
    if operation == "webhook_send" and not config["webhook_secret_ref"]:
        fail("webhook_not_configured")


def emoji_value(value):
    """A Unicode emoji, or a custom emoji as name:id (or <:name:id>); returned URL-encoded for the path."""
    if not isinstance(value, str) or not value.strip():
        fail("invalid_input", "emoji must be a Unicode emoji or a custom emoji name:id.")
    value = value.strip()
    custom = CUSTOM_EMOJI.fullmatch(value)
    if custom:
        name = (custom.group(1) or custom.group(3)) + ":" + (custom.group(2) or custom.group(4))
        return name, quote(name, safe=":")
    if (len(value) > 32 or not any(ord(char) > 127 for char in value)
            or any(ord(char) <= 32 or ord(char) == 127 or (char.isascii() and char.isalpha()) or char in "/?#%\\"
                   for char in value)):
        fail("invalid_input", "emoji must be a Unicode emoji or a custom emoji name:id.")
    return value, quote(value, safe="")


def message_body(document, config, *, kind):
    """JSON body of a create, edit or webhook message, sized before anything is sent."""
    embeds = embeds_value(document.get("embeds"))
    required = kind not in ("upload", "edit") and embeds is None
    content = text_value(document.get("content"), "content", CONTENT_LIMIT, required)
    body = {}
    if content is not None:
        body["content"] = content
    if embeds is not None:
        body["embeds"] = embeds
    if kind == "edit" and not body and not flag(document, "suppress_embeds"):
        fail("invalid_input", "edit_message needs content, embeds or suppress_embeds.")
    body["allowed_mentions"] = mentions(document, config)
    value = flags(document, silent=kind != "edit")
    if value or kind == "edit" and "suppress_embeds" in document:
        body["flags"] = value
    return body


def message_request(value, config):
    """Build one message operation; checks run before the wallet is read."""
    document = structured(value, "content")
    operation = document.get("operation", config["default_operation"])
    if operation not in MESSAGE_KEYS:
        fail("invalid_input", "operation must be one of " + ", ".join(sorted(MESSAGE_KEYS)) + ".")
    unknown = set(document) - MESSAGE_KEYS[operation]
    if unknown:
        fail("invalid_input", "Unknown " + operation + " field: " + sorted(unknown)[0] + ".")
    request_id = identifier(document.get("request_id"))
    gate(operation, config)
    request = {"operation": operation, "request_id": request_id, "channel_id": None}
    if operation == "watch":
        return request
    if operation == "webhook_send":
        body = message_body(document, config, kind="webhook")
        username = document.get("username")
        if username is not None:
            name = text_value(username, "username", 80, True).strip()
            if "discord" in name.lower() or "clyde" in name.lower():
                fail("invalid_input", "username cannot contain 'discord' or 'clyde' (refused by Discord).")
            body["username"] = name
        if document.get("avatar_url") is not None:
            body["avatar_url"] = url_value(document["avatar_url"], "avatar_url", ("https",))
        request.update(thread_id=optional_snowflake(document, "thread_id"), body=body)
        request["body_bytes"] = len(encoded(body, REQUEST_LIMIT, "request_too_large"))
        return request
    channel = target_channel(document, config)
    request["channel_id"] = channel
    if operation == "send_message":
        body = message_body(document, config, kind="send")
        reply = optional_snowflake(document, "reply_to")
        if reply is not None:
            body["message_reference"] = {"message_id": reply, "channel_id": channel}
        elif body["allowed_mentions"]["replied_user"]:
            fail("invalid_input", "mention_replied_user needs reply_to.")
        request["body"] = body
    elif operation == "edit_message":
        request["message_id"] = snowflake(document.get("message_id"), "message_id")
        body = message_body(document, config, kind="edit")
        body["allowed_mentions"].pop("replied_user", None)
        request["body"] = body
    elif operation in ("delete_message", "add_reaction"):
        request["message_id"] = snowflake(document.get("message_id"), "message_id")
        if operation == "add_reaction":
            request["emoji"], request["emoji_path"] = emoji_value(document.get("emoji"))
    else:
        limit = document.get("limit", config["read_limit"])
        if type(limit) is not int or not 1 <= limit <= config["read_limit"]:
            fail("invalid_input", f"limit must be an integer between 1 and read_limit ({config['read_limit']}).")
        before, after = optional_snowflake(document, "before"), optional_snowflake(document, "after")
        if before is not None and after is not None:
            fail("invalid_input", "Give before or after, not both.")
        request.update(limit=limit, before=before, after=after)
    if "body" in request:
        request["body_bytes"] = len(encoded(request["body"], REQUEST_LIMIT, "request_too_large"))
    return request


def file_request(value, config):
    """Build one upload_file; the path is resolved later inside file_root only."""
    document = structured(value, "path")
    if document.get("operation", "upload_file") != "upload_file":
        fail("invalid_input", "The file input only accepts upload_file.")
    unknown = set(document) - FILE_KEYS
    if unknown:
        fail("invalid_input", "Unknown upload_file field: " + sorted(unknown)[0] + ".")
    path = document.get("path")
    if not isinstance(path, str) or not path.strip() or len(path) > 4096 or any(ord(char) < 32 for char in path):
        fail("invalid_input", "path must be a non-empty file path without control characters.")
    request_id = identifier(document.get("request_id"))
    gate("upload_file", config)
    channel = target_channel(document, config)
    filename = document.get("filename")
    if filename is not None and (not isinstance(filename, str) or not FILE_NAME.fullmatch(filename)
                                 or filename.strip() in ("", ".", "..")):
        fail("invalid_input", "filename must be 1-255 characters without slashes or control characters.")
    description = document.get("description")
    if description is not None:
        description = text_value(description, "description", 1024, True)
    body = message_body(document, config, kind="upload")
    reply = optional_snowflake(document, "reply_to")
    if reply is not None:
        body["message_reference"] = {"message_id": reply, "channel_id": channel}
    elif body["allowed_mentions"]["replied_user"]:
        fail("invalid_input", "mention_replied_user needs reply_to.")
    request = {"operation": "upload_file", "request_id": request_id, "channel_id": channel, "path": path.strip(),
               "filename": filename, "description": description, "spoiler": flag(document, "spoiler"), "body": body}
    request["body_bytes"] = len(encoded(body, REQUEST_LIMIT, "request_too_large"))
    return request


def upload_name(request, located):
    name = request["filename"] or located["name"]
    if request["spoiler"] and not name.startswith("SPOILER_"):
        name = "SPOILER_" + name
    return name[:255]


def nonce(channel, request_id, random_text):
    """Discord nonce (at most 25 characters) with enforce_nonce: a resend with the same request_id
    within a few minutes returns the first message instead of creating a duplicate."""
    if request_id:
        return hashlib.sha256(f"{channel}|{request_id}".encode("ascii")).hexdigest()[:25]
    return random_text[:25]


def multipart_name(name):
    """Header-safe multipart filename: no quotes, backslashes or control characters."""
    safe = "".join("_" if ord(char) < 32 or ord(char) == 127 or char in "\"\\" else char for char in name)
    return (safe or "upload")[:255]


def channel_path(channel, *rest):
    return API_PREFIX + "/channels/" + channel + "/messages" + "".join("/" + part for part in rest)


def webhook_path(webhook_id, webhook_token):
    return API_PREFIX + "/webhooks/" + webhook_id + "/" + webhook_token


def route_key(method, operation, major):
    """Rate-limit route: method, route template and its major parameter (channel or webhook)."""
    template = {
        "send_message": "/channels/{channel_id}/messages", "upload_file": "/channels/{channel_id}/messages",
        "read_messages": "/channels/{channel_id}/messages", "watch": "/channels/{channel_id}/messages",
        "edit_message": "/channels/{channel_id}/messages/{message_id}",
        "delete_message": "/channels/{channel_id}/messages/{message_id}",
        "add_reaction": "/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me",
        "webhook_send": "/webhooks/{webhook_id}/{webhook_token}",
    }[operation]
    return method + " " + template, major


def number(value):
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and 0 <= result <= 10 ** 7 else None


def rate_headers(headers):
    """X-RateLimit-* response headers: remaining, reset_after (seconds), bucket, global and scope."""
    remaining = headers.get("x-ratelimit-remaining")
    remaining = int(remaining) if isinstance(remaining, str) and remaining.strip().isdigit() else None
    bucket = headers.get("x-ratelimit-bucket")
    return {"remaining": remaining, "reset_after": number(headers.get("x-ratelimit-reset-after")),
            "bucket": bucket.strip() if isinstance(bucket, str) and BUCKET.fullmatch(bucket.strip()) else None,
            "global": str(headers.get("x-ratelimit-global", "")).strip().lower() == "true",
            "scope": clean(headers.get("x-ratelimit-scope"), 16)}


def map_status(status):
    return {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "bad_request",
            413: "provider_too_large", 429: "rate_limited"}.get(status, "provider_unavailable" if status >= 500 else "provider_error")


def form_errors(value, secrets, prefix="", found=None, depth=0):
    """Flatten Discord's nested `errors` object into at most five 'path: message' lines."""
    found = [] if found is None else found
    if not isinstance(value, dict) or depth > 8 or len(found) >= 5:
        return found
    for key, item in value.items():
        if len(found) >= 5 or not isinstance(key, str):
            break
        if key == "_errors" and isinstance(item, list):
            for entry in item[:5 - len(found)]:
                text = clean(entry.get("message"), 200) if isinstance(entry, dict) else None
                if text:
                    found.append(redact((prefix or "body") + ": " + text, secrets))
        else:
            form_errors(item, secrets, (prefix + "." if prefix else "") + clean(key, 40) if clean(key, 40) else prefix,
                        found, depth + 1)
    return found


def provider_failure(status, body, secrets=(), *, mutation=False, retry_after=None, scope=None):
    """Raise the coded error of a non-2xx response with Discord's {"code", "message", "errors"} body."""
    code = map_status(status)
    document = body if isinstance(body, dict) else {}
    provider_code = document.get("code") if type(document.get("code")) is int else None
    message = clean(document.get("message"), 300)
    errors = form_errors(document.get("errors"), secrets) or None
    if status == 429:
        wait = number(document.get("retry_after"))
        retry_after = wait if wait is not None else retry_after
        if document.get("global") is True:
            scope = "global"
    detail = MESSAGES["provider_unavailable_uncertain"] if code == "provider_unavailable" and mutation else None
    fail(code, detail, uncertain=mutation and code == "provider_unavailable", http_status=status,
         retry_after=round(retry_after, 3) if code == "rate_limited" and retry_after is not None else None,
         provider_code=provider_code, provider_message=redact(message, secrets) if message else None,
         provider_errors=errors, scope=scope if code == "rate_limited" else None,
         retryable=code == "provider_unavailable")


def retry_delay(error, attempts, config, deadline_left, *, mutation):
    """Seconds to wait before another attempt, or None. Writes are retried only after HTTP 429."""
    retryable = error.code == "rate_limited" or (not mutation and (error.code == "provider_unavailable"
                                                                    or error.retryable))
    if not retryable or attempts > config["max_retries"]:
        return None
    wait = error.retry_after if error.retry_after is not None else min(2 ** (attempts - 1), 8)
    if wait > config["max_retry_wait_sec"] or wait + 1 >= deadline_left:
        return None
    return float(wait)


def user(value):
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not SNOWFLAKE.fullmatch(value["id"]):
        return None
    return {"id": value["id"], "username": clean(value.get("username"), 64),
            "global_name": clean(value.get("global_name"), 64), "bot": value.get("bot") is True}


def ids(values, limit=50):
    if not isinstance(values, list):
        return []
    found = []
    for item in values[:limit]:
        ident = item.get("id") if isinstance(item, dict) else item
        if isinstance(ident, str) and SNOWFLAKE.fullmatch(ident):
            found.append(ident)
    return found


def link(value):
    return value if isinstance(value, str) and len(value) <= 2048 and value.startswith(("https://", "http://")) and not any(
        ord(char) <= 32 for char in value) else None


def embed_out(value):
    """Public embed fields read back from Discord, bounded."""
    if not isinstance(value, dict):
        return None
    result = {}
    for key, limit in (("type", 32), ("title", 256), ("description", 4096)):
        text = clean(value.get(key), limit) if key != "description" else (
            value[key][:limit] if isinstance(value.get(key), str) else None)
        if text:
            result[key] = text
    if link(value.get("url")):
        result["url"] = value["url"]
    if type(value.get("color")) is int:
        result["color"] = value["color"]
    for key in ("image", "thumbnail"):
        part = value.get(key)
        if isinstance(part, dict) and link(part.get("url")):
            result[key] = {"url": part["url"]}
    if isinstance(value.get("footer"), dict) and clean(value["footer"].get("text"), 2048):
        result["footer"] = {"text": clean(value["footer"]["text"], 2048)}
    if isinstance(value.get("author"), dict) and clean(value["author"].get("name"), 256):
        result["author"] = {"name": clean(value["author"]["name"], 256)}
    fields = []
    for item in value.get("fields", [])[:25] if isinstance(value.get("fields"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("value"), str):
            fields.append({"name": item["name"][:256], "value": item["value"][:1024], "inline": item.get("inline") is True})
    if fields:
        result["fields"] = fields
    return result or None


def attachment(value):
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not SNOWFLAKE.fullmatch(value["id"]):
        return None
    result = {"id": value["id"], "filename": clean(value.get("filename"), 255),
              "size": value["size"] if type(value.get("size")) is int and value["size"] >= 0 else None,
              "content_type": clean(value.get("content_type"), 128), "url": link(value.get("url"))}
    if clean(value.get("description"), 1024):
        result["description"] = clean(value["description"], 1024)
    return result


def reaction(value):
    if not isinstance(value, dict) or not isinstance(value.get("emoji"), dict) or type(value.get("count")) is not int:
        return None
    emoji = value["emoji"]
    name = clean(emoji.get("name"), 64)
    if not name:
        return None
    if isinstance(emoji.get("id"), str) and SNOWFLAKE.fullmatch(emoji["id"]):
        name = name + ":" + emoji["id"]
    return {"emoji": name, "count": max(0, value["count"]), "me": value.get("me") is True}


def stamp(value):
    return value if isinstance(value, str) and len(value) <= 64 and TIMESTAMP.fullmatch(value) else None


def normalize_message(value):
    """Public message fields: IDs, author, content, references, attachments, embeds and reactions."""
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not SNOWFLAKE.fullmatch(value["id"]):
        return None
    channel = value.get("channel_id")
    if not isinstance(channel, str) or not SNOWFLAKE.fullmatch(channel):
        return None
    reference = value.get("message_reference") if isinstance(value.get("message_reference"), dict) else {}
    reply = reference.get("message_id")
    thread = value.get("thread") if isinstance(value.get("thread"), dict) else {}
    content = value.get("content") if isinstance(value.get("content"), str) else ""
    result = {
        "id": value["id"], "channel_id": channel,
        "guild_id": value["guild_id"] if isinstance(value.get("guild_id"), str) and SNOWFLAKE.fullmatch(value["guild_id"]) else None,
        "author": user(value.get("author")), "content": content[:READ_CONTENT_LIMIT],
        "timestamp": stamp(value.get("timestamp")) or snowflake_time(value["id"]),
        "edited_timestamp": stamp(value.get("edited_timestamp")),
        "type": value["type"] if type(value.get("type")) is int else 0,
        "webhook_id": value["webhook_id"] if isinstance(value.get("webhook_id"), str) and SNOWFLAKE.fullmatch(value["webhook_id"]) else None,
        "reply_to": reply if isinstance(reply, str) and SNOWFLAKE.fullmatch(reply) else None,
        "thread_id": thread["id"] if isinstance(thread.get("id"), str) and SNOWFLAKE.fullmatch(thread["id"]) else None,
        "mention_everyone": value.get("mention_everyone") is True,
        "mentions": ids(value.get("mentions")), "mention_roles": ids(value.get("mention_roles")),
        "pinned": value.get("pinned") is True,
        "attachments": [item for item in (attachment(entry) for entry in (value.get("attachments") or [])[:10]
                                          if isinstance(value.get("attachments"), list)) if item],
        "embeds": [item for item in (embed_out(entry) for entry in (value.get("embeds") or [])[:10]
                                     if isinstance(value.get("embeds"), list)) if item],
        "reactions": [item for item in (reaction(entry) for entry in (value.get("reactions") or [])[:20]
                                        if isinstance(value.get("reactions"), list)) if item],
    }
    return result


def page_messages(body):
    """Validate one GET /messages page: messages in ascending ID order, and the raw page size."""
    if not isinstance(body, list):
        fail("protocol", "Discord returned a page that is not a message array.")
    normalized = [item for item in (normalize_message(entry) for entry in body) if item]
    normalized.sort(key=lambda item: int(item["id"]))
    return normalized, len(body)


def size_of(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def sent_receipt(body, request, requests):
    """Public receipt of a created or edited message: provider IDs, never the token."""
    message = normalize_message(body) if isinstance(body, dict) else None
    if message is None:
        fail("protocol", MESSAGES["protocol_uncertain"], uncertain=True)
    if request.get("channel_id") and message["channel_id"] != request["channel_id"]:
        fail("protocol", MESSAGES["protocol_uncertain"], uncertain=True)
    result = {"request_id": request.get("request_id"), "operation": request["operation"],
              "channel_id": message["channel_id"], "message_id": message["id"], "guild_id": message["guild_id"],
              "timestamp": message["timestamp"], "requests": requests}
    if request["operation"] == "edit_message":
        if message["id"] != request["message_id"]:
            fail("protocol", MESSAGES["protocol_uncertain"], uncertain=True)
        result["edited_timestamp"] = message["edited_timestamp"]
    if request["operation"] == "upload_file":
        result["attachments"] = message["attachments"]
    if request["operation"] == "webhook_send":
        result["webhook_id"] = message["webhook_id"]
        if request.get("thread_id"):
            result["thread_id"] = request["thread_id"]
    if request["operation"] == "send_message" and request["body"].get("message_reference"):
        result["reply_to"] = request["body"]["message_reference"]["message_id"]
    return result


def watch_key(config):
    """One watch per API origin and bot token reference; the document never holds the reference."""
    material = "|".join((config["api_base_url"], config["secret_ref"]))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def empty_state():
    return {"schema_version": 1, "watches": {}}


def state_document(value):
    """Validate the persisted cursor document; malformed state is never guessed."""
    if not isinstance(value, dict) or value.get("schema_version") != 1 or set(value) != {"schema_version", "watches"}:
        fail("state_invalid")
    watches = value["watches"]
    if not isinstance(watches, dict) or len(watches) > STATE_WATCHES:
        fail("state_invalid")
    for key, entry in watches.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{24}", key) or not isinstance(entry, dict)
                or set(entry) != {"channels", "updated_at"} or type(entry["updated_at"]) is not int
                or not isinstance(entry["channels"], dict) or len(entry["channels"]) > STATE_CHANNELS):
            fail("state_invalid")
        for channel, cursor in entry["channels"].items():
            if (not isinstance(channel, str) or not SNOWFLAKE.fullmatch(channel) or not isinstance(cursor, str)
                    or not (cursor == "0" or SNOWFLAKE.fullmatch(cursor))):
                fail("state_invalid")
    return value


def with_watch(document, key, channels, now):
    """Return a new document holding these cursors; the 16 most recent watches are retained."""
    watches = dict(document["watches"])
    watches[key] = {"channels": dict(channels), "updated_at": int(now)}
    if len(watches) > STATE_WATCHES:
        for old in sorted(watches, key=lambda item: watches[item]["updated_at"])[:len(watches) - STATE_WATCHES]:
            watches.pop(old)
    return {"schema_version": 1, "watches": watches}
