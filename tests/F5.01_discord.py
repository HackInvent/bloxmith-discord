"""FB1/FB2/FB3/FB4/FB5/FB7: pure settings, credentials, requests, bodies, provider errors, watch state and block hooks.

No socket is opened and no provider is contacted; the wallet resolver is a local double.
"""

import json
from pathlib import Path
import sys
from types import MappingProxyType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).resolve().parent)]
from blocs.discord import logic, transport
from blocs.discord.block import DiscordBlock
from bloxsmith_app.block_api import BlockRuntimeContext, BlockRuntimePreparationContext
from discord_fixture import BOT_ID, CUSTOM_EMOJI, GENERAL, HOOK_REF, OTHER, RANDOM, REF, TOKEN, WEBHOOK_ID, WEBHOOK_TOKEN

BASE = {"enabled": True, "secret_ref": REF, "allowed_channels": [GENERAL, RANDOM], "default_channel": GENERAL}
WRITER = {**BASE, "allow_writes": True}
DELETER = {**WRITER, "allow_delete": True}
HOOK_ONLY = {"enabled": True, "webhook_secret_ref": HOOK_REF, "default_operation": "webhook_send", "allow_writes": True}


def rejected(call, code=None):
    """A contract violation must raise a coded error, never fall back silently."""
    try:
        call()
    except logic.DiscordError as exc:
        assert code is None or exc.code == code, (code, exc.code, str(exc))
        assert TOKEN not in str(exc) and WEBHOOK_TOKEN not in str(exc)
        return exc
    raise AssertionError("Invalid value accepted")


def conf(**extra):
    return logic.configuration({**WRITER, **extra})


def settings_contract():
    """# - FB1 - remote access, writes, delete, mentions and watch stay off by default and every setting is bounded."""
    defaults = logic.configuration({})
    assert defaults["enabled"] is False and defaults["allow_writes"] is False and defaults["allow_delete"] is False
    assert defaults["watch"] is False and defaults["emit_existing"] is False and defaults["mentions"] == "none"
    assert defaults["api_base_url"] == logic.OFFICIAL_API == "https://discord.com"
    assert defaults["watch_interval_sec"] == 30 and defaults["page_size"] == 100 and defaults["default_channel"] is None
    assert set(DiscordBlock().default_config()) == set(logic.DEFAULTS)
    config = logic.configuration({**DELETER, "execution": {"inhibited": False}, "allowed_channels": [int(GENERAL), RANDOM],
                                  "default_channel": " " + GENERAL + " ", "watch": True, "watch_channels": [RANDOM]})
    assert config["allowed_channels"] == [GENERAL, RANDOM] and config["default_channel"] == GENERAL
    assert logic.watched(config) == [RANDOM] and logic.watched({**config, "watch_channels": []}) == [GENERAL, RANDOM]
    frozen = logic.configuration(MappingProxyType({**BASE, "allowed_channels": (GENERAL,)}))
    assert frozen["allowed_channels"] == [GENERAL]
    hook = logic.configuration(HOOK_ONLY)
    assert hook["secret_ref"] == "" and hook["default_operation"] == "webhook_send"
    assert logic.configuration({"enabled": False, "watch": True})["watch"] is True, "settings may be prepared while off"
    for raw in ({"enabled": True}, {**BASE, "secret_ref": TOKEN}, {**BASE, "secret_ref": "env://DISCORD"},
                {**BASE, "token": TOKEN}, {**BASE, "bot_token": TOKEN}, {**BASE, "webhook_url": "https://x"},
                {**BASE, "webhook_secret_ref": "https://discord.com/api/webhooks/1/abc"},
                {**BASE, "allowed_channels": []}, {**BASE, "allowed_channels": "1"},
                {**BASE, "allowed_channels": ["#general"]}, {**BASE, "allowed_channels": [GENERAL, GENERAL]},
                {**BASE, "allowed_channels": ["0123"]}, {**BASE, "allowed_channels": [str(2 ** 64)]},
                {**BASE, "allowed_channels": [str(10 ** 15 + index) for index in range(101)]},
                {**BASE, "default_channel": OTHER}, {**BASE, "default_channel": "general"},
                {**BASE, "default_operation": "webhook_send"}, {**BASE, "default_operation": "read_messages"},
                {**HOOK_ONLY, "default_operation": "send_message"}, {**BASE, "mentions": "everyone"},
                {**BASE, "allow_delete": True}, {**BASE, "enabled": "yes"}, {**BASE, "allow_writes": 1},
                {**BASE, "watch": "true"}, {**BASE, "watch_channels": [OTHER]},
                {**HOOK_ONLY, "watch": True}, {**BASE, "watch": True, "watch_interval_sec": 5},
                {**BASE, "watch": True, "allowed_channels": [str(10 ** 15 + index) for index in range(21)]},
                {**BASE, "file_root": "uploads"}, {**BASE, "file_root": "/srv/../etc"}, {**BASE, "ca_file": "ca.pem"},
                {**BASE, "api_base_url": "http://discord.com"}, {**BASE, "api_base_url": "https://discord.com/api/v10"},
                {**BASE, "api_base_url": "https://discordapp.com"}, {**BASE, "api_base_url": "https://user:pw@discord.com"},
                {**BASE, "api_base_url": "https://x.test/?a=1"}, {**BASE, "api_base_url": "ftp://discord.com"},
                {**BASE, "api_base_url": "https://bad host"}, {**BASE, "api_base_url": "http://example.test:8080"}):
        rejected(lambda raw=raw: logic.configuration(raw), "config")
    for key, (low, high) in logic.BOUNDS.items():
        logic.configuration({**BASE, key: low})
        logic.configuration({**BASE, key: high})
        for bad in (low - 1, high + 1, str(low), float(low), True):
            rejected(lambda key=key, bad=bad: logic.configuration({**BASE, key: bad}), "config")
    loopback = logic.configuration({**BASE, "api_base_url": "http://127.0.0.1:8080/"})
    assert loopback["api_base_url"] == "http://127.0.0.1:8080"
    assert logic.configuration({**BASE, "api_base_url": "https://discord.com:443"})["api_base_url"] == logic.OFFICIAL_API


def credential_contract():
    """# - FB1 - # - FB3 - wallet values: a bot token, or a webhook URL kept to its ID and token; never echoed."""
    assert logic.bot_token(TOKEN) == TOKEN and logic.bot_token("  Bot " + TOKEN + "\n") == TOKEN
    assert logic.bot_token(json.dumps({"token": TOKEN})) == TOKEN
    for bad in ("", "short", TOKEN + " x", "{broken", json.dumps({"bot": TOKEN}), None, 7, "x" * 300):
        rejected(lambda bad=bad: logic.bot_token(bad), "secret")
    official = logic.configuration(HOOK_ONLY)
    for url in (f"https://discord.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
                f"https://discordapp.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
                f"https://ptb.discord.com/api/v10/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}/",
                json.dumps({"url": f"https://canary.discord.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}"})):
        assert logic.webhook(url, official) == (WEBHOOK_ID, WEBHOOK_TOKEN), url
    for url in (f"http://discord.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
                f"https://evil.example/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
                f"https://discord.com.evil.example/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
                f"https://discord.com:8443/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
                f"https://u:p@discord.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
                f"https://discord.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}?wait=true",
                f"https://discord.com/api/webhooks/{WEBHOOK_ID}", f"https://discord.com/api/webhooks/abc/{WEBHOOK_TOKEN}",
                f"https://discord.com/api/webhooks/{WEBHOOK_ID}/short", "not a url", "", None):
        error = rejected(lambda url=url: logic.webhook(url, official), "webhook_secret")
        assert WEBHOOK_TOKEN not in json.dumps(error.envelope())
    local = logic.configuration({**HOOK_ONLY, "api_base_url": "http://127.0.0.1:8123"})
    assert logic.webhook(f"http://127.0.0.1:8123/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}", local)[0] == WEBHOOK_ID
    rejected(lambda: logic.webhook(f"http://127.0.0.1:9999/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}", local),
             "webhook_secret")
    rejected(lambda: logic.webhook(f"https://discord.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}", local),
             "webhook_secret")
    assert logic.redact("a " + TOKEN + " b " + WEBHOOK_TOKEN, [TOKEN, WEBHOOK_TOKEN, ""]) == "a *** b ***"


def request_contract():
    """# - FB2 - # - FB3 - # - FB5 - explicit operations; switches, allowlist and mentions apply before the wallet."""
    config = conf()
    plain = logic.message_request("Build finished @everyone", config)
    assert plain["operation"] == "send_message" and plain["channel_id"] == GENERAL
    assert plain["body"] == {"content": "Build finished @everyone", "allowed_mentions": {"parse": [], "replied_user": False}}
    assert logic.message_request(b"bytes", config)["body"]["content"] == "bytes"
    rich = logic.message_request({"channel_id": RANDOM, "content": "Report", "reply_to": "1300000000000000099",
                                  "silent": True, "suppress_embeds": True, "request_id": "r-1",
                                  "embeds": [{"title": "Build", "description": "ok", "color": 0x2ECC71,
                                              "fields": [{"name": "Tests", "value": "12", "inline": True}],
                                              "footer": {"text": "CI"}, "timestamp": "2026-01-02T10:00:00Z",
                                              "url": "https://ci.example.test/1", "image": {"url": "attachment://a.png"}}]},
                                 config)
    body = rich["body"]
    assert body["message_reference"] == {"message_id": "1300000000000000099", "channel_id": RANDOM}
    assert body["flags"] == logic.FLAG_SUPPRESS_EMBEDS | logic.FLAG_SUPPRESS_NOTIFICATIONS and rich["request_id"] == "r-1"
    assert body["embeds"][0]["fields"] == [{"name": "Tests", "value": "12", "inline": True}]
    assert logic.message_request({"embeds": json.dumps({"title": "Only"})}, config)["body"]["embeds"] == [{"title": "Only"}]
    # Mentions: an explicit object, never wider than the setting.
    users = conf(mentions="users")
    assert logic.message_request("hi <@1>", users)["body"]["allowed_mentions"] == {"parse": ["users"], "replied_user": False}
    picked = logic.message_request({"content": "hi", "allowed_mentions": {"users": [BOT_ID]}}, users)
    assert picked["body"]["allowed_mentions"] == {"parse": [], "users": [BOT_ID], "replied_user": False}
    replied = logic.message_request({"content": "re", "reply_to": GENERAL, "mention_replied_user": True}, users)
    assert replied["body"]["allowed_mentions"]["replied_user"] is True
    everyone = conf(mentions="all")
    assert logic.message_request("x", everyone)["body"]["allowed_mentions"]["parse"] == ["users", "roles", "everyone"]
    for document, setting in (({"content": "x", "allowed_mentions": {"parse": ["everyone"]}}, users),
                              ({"content": "x", "allowed_mentions": {"parse": ["users"]}}, config),
                              ({"content": "x", "allowed_mentions": {"roles": ["1300000000000000002"]}}, users),
                              ({"content": "x", "reply_to": GENERAL, "mention_replied_user": True}, config)):
        rejected(lambda document=document, setting=setting: logic.message_request(document, setting), "mentions_not_allowed")
    edit = logic.message_request({"operation": "edit_message", "message_id": "1300000000000000099", "content": "v2",
                                  "suppress_embeds": False}, config)
    assert edit["body"] == {"content": "v2", "allowed_mentions": {"parse": []}, "flags": 0}
    assert logic.message_request({"operation": "edit_message", "message_id": GENERAL, "suppress_embeds": True},
                                 config)["body"]["flags"] == logic.FLAG_SUPPRESS_EMBEDS
    read = logic.message_request({"operation": "read_messages", "limit": 20, "before": GENERAL}, logic.configuration(BASE))
    assert (read["limit"], read["before"], read["after"]) == (20, GENERAL, None)
    assert logic.message_request({"operation": "read_messages"}, logic.configuration(BASE))["limit"] == 100
    react = logic.message_request({"operation": "add_reaction", "message_id": GENERAL, "emoji": "👍"}, config)
    assert react["emoji"] == "👍" and react["emoji_path"] == "%F0%9F%91%8D"
    for raw, name in (("<:party:" + CUSTOM_EMOJI + ">", "party:" + CUSTOM_EMOJI),
                      ("<a:dance:" + CUSTOM_EMOJI + ">", "dance:" + CUSTOM_EMOJI),
                      ("abc:" + CUSTOM_EMOJI, "abc:" + CUSTOM_EMOJI), ("1️⃣", "1️⃣")):
        assert logic.emoji_value(raw)[0] == name, raw
    for bad in ("", "thumbsup", ":thumbsup:", "a/b", "👍 x", "x" * 40, 5, "<:x:" + CUSTOM_EMOJI + ">"):
        rejected(lambda bad=bad: logic.emoji_value(bad), "invalid_input")
    hook = logic.message_request({"content": "Deployed", "username": "Release bot", "thread_id": "1100000000000000005",
                                  "avatar_url": "https://cdn.example.test/a.png"}, logic.configuration(HOOK_ONLY))
    assert hook["operation"] == "webhook_send" and hook["channel_id"] is None and hook["thread_id"] == "1100000000000000005"
    assert hook["body"]["username"] == "Release bot" and hook["body"]["allowed_mentions"]["parse"] == []
    watch = logic.message_request({"operation": "watch", "request_id": "w"}, conf(watch=True))
    assert watch == {"operation": "watch", "request_id": "w", "channel_id": None}
    upload = logic.file_request("reports/q1.pdf", config)
    assert upload["path"] == "reports/q1.pdf" and upload["channel_id"] == GENERAL and upload["filename"] is None
    assert upload["body"] == {"allowed_mentions": {"parse": [], "replied_user": False}}
    named = logic.file_request({"path": "a.png", "filename": "chart.png", "content": "Chart", "description": "Sales",
                                "spoiler": True}, config)
    assert logic.upload_name(named, {"name": "a.png"}) == "SPOILER_chart.png" and named["description"] == "Sales"
    # Each refusal happens before any wallet access, with a stable code.
    for document, setting, code in (
            ("hello", logic.configuration(BASE), "writes_disabled"),
            ({"operation": "delete_message", "message_id": GENERAL}, config, "delete_disabled"),
            ({"operation": "watch"}, config, "watch_disabled"),
            ({"operation": "send_message", "content": "x"}, logic.configuration(HOOK_ONLY), "bot_not_configured"),
            ({"operation": "webhook_send", "content": "x"}, config, "webhook_not_configured"),
            ({"content": "x", "channel_id": OTHER}, config, "channel_not_allowed"),
            ({"content": "x"}, conf(default_channel=""), "channel_required"),
            ({"content": "x", "channel_id": "#general"}, config, "invalid_input"),
            ({"content": ""}, config, "invalid_input"), ({"content": "x" * 2001}, config, "invalid_input"),
            ({"content": "x", "tts": True}, config, "invalid_input"), ({"operation": "ban"}, config, "invalid_input"),
            ({"content": "x", "request_id": "bad id"}, config, "invalid_input"),
            ({"embeds": [{"title": "x" * 257}]}, config, "invalid_input"),
            ({"embeds": [{"description": "x" * 4001}, {"description": "x" * 2000}]}, config, "invalid_input"),
            ({"embeds": [{"title": "x"}] * 11}, config, "invalid_input"),
            ({"embeds": [{"title": "x", "video": {}}]}, config, "invalid_input"),
            ({"embeds": [{"fields": [{"name": "a", "value": "b"}] * 26}]}, config, "invalid_input"),
            ({"embeds": [{"color": 0x1000000}]}, config, "invalid_input"),
            ({"embeds": [{"url": "javascript:alert(1)"}]}, config, "invalid_input"),
            ({"embeds": [{"timestamp": "yesterday"}]}, config, "invalid_input"), ({"embeds": []}, config, "invalid_input"),
            ({"content": "x", "allowed_mentions": {"parse": ["users"], "users": [BOT_ID]}}, users, "invalid_input"),
            ({"content": "x", "allowed_mentions": {"parse": ["users", "users"]}}, users, "invalid_input"),
            ({"content": "x", "mention_replied_user": True}, users, "invalid_input"),
            ({"content": "x", "silent": "yes"}, config, "invalid_input"),
            ({"operation": "edit_message", "content": "x"}, config, "invalid_input"),
            ({"operation": "edit_message", "message_id": GENERAL}, config, "invalid_input"),
            ({"operation": "read_messages", "limit": 101}, config, "invalid_input"),
            ({"operation": "read_messages", "limit": 0}, config, "invalid_input"),
            ({"operation": "read_messages", "before": GENERAL, "after": GENERAL}, config, "invalid_input"),
            ({"operation": "add_reaction", "message_id": GENERAL}, config, "invalid_input"),
            ({"operation": "webhook_send", "content": "x", "username": "Discord bot"},
             logic.configuration(HOOK_ONLY), "invalid_input"),
            ({"operation": "webhook_send", "content": "x", "avatar_url": "http://x.test/a.png"},
             logic.configuration(HOOK_ONLY), "invalid_input"),
            ({"operation": "watch", "channel_id": GENERAL}, conf(watch=True), "invalid_input"),
            ('{"content": "x", "content": "y"}', config, "invalid_input"), ("{broken", config, "invalid_input"),
            ('{"content": NaN}', config, "invalid_input")):
        rejected(lambda document=document, setting=setting: logic.message_request(document, setting), code)
    for document, code in (({"path": ""}, "invalid_input"), ({"path": "a", "operation": "send_message"}, "invalid_input"),
                           ({"path": "a", "filename": "../x"}, "invalid_input"), ({"path": "a", "url": "x"}, "invalid_input"),
                           ({"path": "a", "channel_id": OTHER}, "channel_not_allowed"),
                           ({"path": "a", "description": "x" * 1025}, "invalid_input")):
        rejected(lambda document=document: logic.file_request(document, config), code)
    rejected(lambda: logic.file_request("a.pdf", logic.configuration(BASE)), "writes_disabled")
    rejected(lambda: logic.message_request("x" * (logic.INPUT_LIMIT + 1), config), "invalid_input")
    big = [{"description": "d" * 3000, "fields": [{"name": "n", "value": "v" * 1000}]}] * 1
    assert logic.message_request({"embeds": big}, config)["body_bytes"] > 4000


def provider_contract():
    """# - FB4 - Discord error bodies map to stable codes; writes after 5xx are uncertain; 429 and buckets."""
    for status, code in ((400, "bad_request"), (401, "unauthorized"), (403, "forbidden"), (404, "not_found"),
                         (413, "provider_too_large"), (429, "rate_limited"), (500, "provider_unavailable"),
                         (502, "provider_unavailable"), (409, "provider_error")):
        error = rejected(lambda status=status: logic.provider_failure(status, {"message": "m", "code": 1}), code)
        assert error.http_status == status and error.provider_code == 1 and error.provider_message == "m"
    write = rejected(lambda: logic.provider_failure(503, None, mutation=True), "provider_unavailable")
    assert write.uncertain and "uncertain" in str(write)
    assert not rejected(lambda: logic.provider_failure(503, None), "provider_unavailable").uncertain
    assert not rejected(lambda: logic.provider_failure(400, {}, mutation=True), "bad_request").uncertain
    form = rejected(lambda: logic.provider_failure(400, {"code": 50035, "message": "Invalid Form Body", "errors": {
        "embeds": {"0": {"title": {"_errors": [{"code": "BASE_TYPE_MAX_LENGTH", "message": "Must be 256 or fewer."}]}}},
        "content": {"_errors": [{"message": "Bad " + TOKEN}]}}}, [TOKEN]), "bad_request")
    assert form.provider_errors == ["embeds.0.title: Must be 256 or fewer.", "content: Bad ***"]
    limited = rejected(lambda: logic.provider_failure(429, {"message": "You are being rate limited.", "retry_after": 1.5,
                                                            "global": True}, retry_after=3), "rate_limited")
    assert limited.retry_after == 1.5 and limited.scope == "global"
    assert rejected(lambda: logic.provider_failure(429, None, retry_after=4.0), "rate_limited").retry_after == 4.0
    leak = rejected(lambda: logic.provider_failure(403, {"message": "token " + TOKEN, "code": 50001}, [TOKEN]), "forbidden")
    assert TOKEN not in json.dumps(leak.envelope()) and leak.provider_message == "token ***"
    config = conf(max_retries=2, max_retry_wait_sec=10)
    assert logic.retry_delay(limited, 1, config, 30, mutation=True) == 1.5
    assert logic.retry_delay(limited, 3, config, 30, mutation=True) is None
    assert logic.retry_delay(logic.DiscordError("rate_limited", retry_after=11), 1, config, 30, mutation=False) is None
    assert logic.retry_delay(logic.DiscordError("rate_limited", retry_after=5), 1, config, 5, mutation=False) is None
    assert logic.retry_delay(logic.DiscordError("provider_unavailable"), 1, config, 30, mutation=False) == 1.0
    assert logic.retry_delay(logic.DiscordError("provider_unavailable"), 2, config, 30, mutation=False) == 2.0
    assert logic.retry_delay(logic.DiscordError("provider_unavailable"), 1, config, 30, mutation=True) is None
    assert logic.retry_delay(logic.DiscordError("network", retryable=True), 1, config, 30, mutation=False) == 1.0
    assert logic.retry_delay(logic.DiscordError("network"), 1, config, 30, mutation=True) is None
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset-after": "0.25", "x-ratelimit-bucket": "abc123",
               "x-ratelimit-scope": "user"}
    assert logic.rate_headers(headers) == {"remaining": 0, "reset_after": 0.25, "bucket": "abc123", "global": False,
                                           "scope": "user"}
    assert logic.rate_headers({"x-ratelimit-reset-after": "nan", "x-ratelimit-bucket": "bad bucket!"})["bucket"] is None
    # Buckets: an exhausted bucket delays the next call of the route; routes sharing a hash share it.
    scope, route = ("origin", "digest"), logic.route_key("POST", "send_message", GENERAL)
    transport.limit_record(scope, route, headers, now=100.0)
    assert abs(transport.limit_wait(scope, route, now=100.1) - 0.15) < 1e-6
    assert transport.limit_wait(scope, logic.route_key("POST", "send_message", RANDOM), now=100.1) == 0.0
    other = logic.route_key("PATCH", "edit_message", GENERAL)
    assert transport.limit_wait(scope, other, now=100.1) == 0.0
    transport.limit_record(scope, other, {"x-ratelimit-bucket": "abc123", "x-ratelimit-remaining": "0",
                                          "x-ratelimit-reset-after": "0.5"}, now=100.1)
    assert abs(transport.limit_wait(scope, route, now=100.1) - 0.5) < 1e-6, "a shared bucket hash shares the reset"
    transport.limit_record(scope, route, {"x-ratelimit-bucket": "abc123", "x-ratelimit-remaining": "4"}, now=100.2)
    assert transport.limit_wait(scope, other, now=100.2) == 0.0, "a later response with remaining capacity frees it"
    transport.limit_record(scope, route, {}, now=100.2, retry_after=2.0, global_limit=True)
    assert abs(transport.limit_wait(scope, logic.route_key("GET", "read_messages", RANDOM), now=100.2) - 2.0) < 1e-6
    assert transport.limit_wait(("origin", "other-bot"), route, now=100.2) == 0.0, "another credential is independent"


def normalization_contract():
    """# - FB3 - # - FB5 - messages keep public fields only; receipts carry provider IDs, never credentials."""
    raw = {"id": "1300000000000000100", "channel_id": GENERAL, "guild_id": "1000000000000000001",
           "author": {"id": BOT_ID, "username": "fixture-bot", "bot": True, "avatar": "hash", "email": "x@example.test"},
           "content": "hello\nworld", "timestamp": "2026-01-02T10:00:00.123000+00:00", "edited_timestamp": None,
           "type": 19, "message_reference": {"message_id": "1300000000000000099"}, "mention_everyone": True,
           "mentions": [{"id": "1300000000000000001", "username": "ada"}], "mention_roles": ["1600000000000000001"],
           "attachments": [{"id": "1700000000000000001", "filename": "a.png", "size": 12, "content_type": "image/png",
                            "url": "https://cdn.example.test/a.png", "proxy_url": "https://media.example.test/a.png"}],
           "embeds": [{"type": "rich", "title": "T", "fields": [{"name": "n", "value": "v"}], "provider": {"name": "x"}}],
           "reactions": [{"emoji": {"id": None, "name": "👍"}, "count": 2, "me": True},
                         {"emoji": {"id": CUSTOM_EMOJI, "name": "party"}, "count": 1, "me": False}],
           "thread": {"id": "1100000000000000005"}, "member": {"roles": []}, "nonce": "n"}
    message = logic.normalize_message(raw)
    assert message["author"] == {"id": BOT_ID, "username": "fixture-bot", "global_name": None, "bot": True}
    assert message["content"] == "hello\nworld" and message["reply_to"] == "1300000000000000099" and message["type"] == 19
    assert message["mentions"] == ["1300000000000000001"] and message["mention_roles"] == ["1600000000000000001"]
    assert message["attachments"] == [{"id": "1700000000000000001", "filename": "a.png", "size": 12,
                                       "content_type": "image/png", "url": "https://cdn.example.test/a.png"}]
    assert message["embeds"] == [{"type": "rich", "title": "T", "fields": [{"name": "n", "value": "v", "inline": False}]}]
    assert message["reactions"] == [{"emoji": "👍", "count": 2, "me": True},
                                    {"emoji": "party:" + CUSTOM_EMOJI, "count": 1, "me": False}]
    assert message["thread_id"] == "1100000000000000005" and "member" not in message and "nonce" not in message
    assert logic.normalize_message({"id": "x"}) is None and logic.normalize_message({"id": GENERAL}) is None
    page, size = logic.page_messages([raw, {**raw, "id": "1300000000000000050"}, {"bad": 1}])
    assert [item["id"] for item in page] == ["1300000000000000050", "1300000000000000100"] and size == 3
    rejected(lambda: logic.page_messages({"messages": []}), "protocol")
    request = {"operation": "send_message", "request_id": "r", "channel_id": GENERAL, "body": {"content": "x"}}
    receipt = logic.sent_receipt(raw, request, 1)
    assert receipt == {"request_id": "r", "operation": "send_message", "channel_id": GENERAL,
                       "message_id": "1300000000000000100", "guild_id": "1000000000000000001",
                       "timestamp": "2026-01-02T10:00:00.123000+00:00", "requests": 1}
    assert rejected(lambda: logic.sent_receipt({**raw, "channel_id": RANDOM}, request, 1), "protocol").uncertain
    assert rejected(lambda: logic.sent_receipt(None, request, 1), "protocol").uncertain
    edit = {"operation": "edit_message", "request_id": None, "channel_id": GENERAL, "message_id": "1", "body": {}}
    rejected(lambda: logic.sent_receipt(raw, edit, 1), "protocol")
    assert logic.snowflake_time("175928847299117063") == "2016-04-30T11:18:25.796Z", "Discord's documented example"
    assert logic.snowflake_at(1462015105796) == str(1462015105796 - logic.DISCORD_EPOCH_MS << 22)
    first = logic.nonce(GENERAL, "req-1", "random")
    assert len(first) == 25 and first == logic.nonce(GENERAL, "req-1", "other") != logic.nonce(RANDOM, "req-1", "x")
    assert logic.nonce(GENERAL, None, "r" * 30) == "r" * 25
    assert logic.multipart_name('a"b\\c\nd.txt') == "a_b_c_d.txt"


def watch_state_contract():
    """# - FB5 - the persisted watch document holds channel cursors only, and is validated before use."""
    config = conf(watch=True)
    key = logic.watch_key(config)
    assert len(key) == 24 and REF not in key and key != logic.watch_key({**config, "secret_ref": "secret://workspace/x"})
    document = logic.with_watch(logic.empty_state(), key, {GENERAL: "1300000000000000100", RANDOM: "0"}, 1700000000)
    assert logic.state_document(document) is document
    text = json.dumps(document)
    assert REF not in text and TOKEN not in text
    for bad in ({}, {"schema_version": 2, "watches": {}}, {"schema_version": 1, "watches": {}, "extra": 1},
                {"schema_version": 1, "watches": {"short": {"channels": {}, "updated_at": 1}}},
                {"schema_version": 1, "watches": {key: {"channels": {GENERAL: 5}, "updated_at": 1}}},
                {"schema_version": 1, "watches": {key: {"channels": {"general": "1"}, "updated_at": 1}}},
                {"schema_version": 1, "watches": {key: {"channels": {GENERAL: "01"}, "updated_at": 1}}},
                {"schema_version": 1, "watches": {key: {"channels": {}, "updated_at": "1"}}}):
        rejected(lambda bad=bad: logic.state_document(bad), "state_invalid")
    many = logic.empty_state()
    for index in range(20):
        many = logic.with_watch(many, f"{index:024x}", {}, index)
    assert len(many["watches"]) == logic.STATE_WATCHES and f"{19:024x}" in many["watches"] and f"{0:024x}" not in many["watches"]


def block_contract():
    """# - FB1 - # - FB4 - # - FB5 - # - FB7 - block hooks fail before side effects, and surfaces stay bounded and translated."""
    block = DiscordBlock()
    ports_in = tuple(SimpleNamespace(id=port["id"], name=port["name"]) for port in block.model["ports"]["inputs"])
    ports_out = tuple(SimpleNamespace(id=port["id"], name=port["name"]) for port in block.model["ports"]["outputs"])
    assert [port.name for port in ports_in] == ["message", "file"]
    assert [port.name for port in ports_out] == ["messages", "sent", "error"]
    assert all(port["execution_requirement"] == "not_required_for_execution" for port in block.model["ports"]["inputs"])
    assert block.model["version"] == "0.1.0" and block.model["config"] == logic.DEFAULTS
    calls = []

    def resolver(ref):
        calls.append(ref)
        return "not a token " + TOKEN

    def run(config, inputs, services=None):
        context = BlockRuntimeContext(config={**block.default_config(), **config}, inputs=inputs, input_ports=ports_in,
                                      output_ports=ports_out, services={"resolve_secret": resolver, **(services or {})})
        result = block.execute_runtime(context)
        assert result.status in ("success", "skipped"), result
        return result, {output.port_name: json.loads(output.value) for output in result.outputs}

    assert run(BASE, {})[0].status == "skipped"
    for config, inputs, code in (({**WRITER, "enabled": False}, {"message": "hi"}, "disabled"),
                                 (BASE, {"message": "hi"}, "writes_disabled"),
                                 (WRITER, {"message": "hi", "file": "a.pdf"}, "one_operation"),
                                 (WRITER, {"message": '{"content":'}, "invalid_input"),
                                 (WRITER, {"message": {"content": "x", "channel_id": OTHER}}, "channel_not_allowed"),
                                 (WRITER, {"file": "a.pdf"}, "file_root_required"),
                                 ({**WRITER, "file_root": "/nonexistent-discord-fixture"}, {"file": "a.pdf"},
                                  "file_root_required"),
                                 ({**WRITER, "watch": True}, {"message": {"operation": "watch"}}, "storage"),
                                 (HOOK_ONLY, {"message": {"operation": "read_messages"}}, "bot_not_configured"),
                                 ({**BASE, "secret_ref": TOKEN}, {"message": "hi"}, "config")):
        result, outputs = run(config, inputs)
        assert set(outputs) == {"error"} and outputs["error"]["code"] == code, outputs
        assert result.metadata["discord"] == {"status": "error", "code": code, "uncertain": False}
        assert TOKEN not in json.dumps(outputs) and TOKEN not in " ".join(result.logs)
    assert not calls, "The wallet must not be read before settings, request, file and storage validation"
    result, outputs = run(WRITER, {"message": {"content": "hi", "request_id": "req-1", "channel_id": RANDOM}})
    assert outputs["error"]["code"] == "secret" and outputs["error"]["request_id"] == "req-1" and calls == [REF]
    assert outputs["error"]["channel_id"] == RANDOM and outputs["error"]["operation"] == "send_message"
    assert TOKEN not in json.dumps(outputs) and result.logs == ["[discord-error] send_message: secret"]
    result, outputs = run(HOOK_ONLY, {"message": "notify"})
    assert outputs["error"]["code"] == "webhook_secret" and calls[-1] == HOOK_REF and TOKEN not in json.dumps(outputs)

    def prepared(config, mode="zeromq_active"):
        context = BlockRuntimePreparationContext.from_context(SimpleNamespace(config=config, runtime_mode=mode))
        return block.prepare_runtime(context)

    assert prepared(WRITER).listen_on_run is False
    assert prepared({**BASE, "watch": True}).listen_on_run is True
    assert prepared({**BASE, "watch": True}, "centralized").listen_on_run is False, "One Shot never listens"
    assert prepared({**BASE, "enabled": False, "watch": True}).listen_on_run is False
    rejected(lambda: prepared({**BASE, "secret_ref": TOKEN}), "config")
    rejected(lambda: prepared({**BASE, "watch": True, "watch_interval_sec": 9}), "config")
    node = block.build_node_payload(node_id="chat", position={"x": 0, "y": 0})
    accepted = block.handle_ui_action(node=node, action="save_settings", values={"node_patch": {"config": WRITER}})
    operation = accepted["graph_operations"][-1]
    assert operation["op"] == "update_node_config" and operation["replace"] is True
    assert operation["config"]["allowed_channels"] == [GENERAL, RANDOM] and operation["config"]["allow_writes"] is True
    refused = block.handle_ui_action(node=node, action="save_settings",
                                     values={"node_patch": {"config": {**BASE, "allow_delete": True}}})
    assert "allow_writes" in refused["error"] and "graph_operations" not in refused
    html = block.render_modal(node=node)["html"]
    assert 'data-block-config-field="secret_ref"' in html and 'data-discord-show="write" hidden' in html
    assert 'data-discord-show="watch" hidden' in html and 'data-block-value-type="json"' in html and "{{" not in html
    assert 'data-block-config-field="file_root"' in html and '<option value="webhook_send">' in html
    writing = {**node, "config": {**node["config"], **DELETER, "watch": True}}
    panel = block.render_inspector_panel(node=writing)["html"]
    assert 'data-discord-show="write" hidden' not in panel and 'data-discord-show="watch" hidden' not in panel
    card = block.render_node_card(node=node)["html"]
    assert card.count("Remote access off") == 1 and 'data-i18n="block.discord.card_disabled" data-i18n-params=""' in card
    assert "Read only · 2 channels" in block.render_node_card(node={**node, "config": {**node["config"], **BASE}})["html"]
    card = block.render_node_card(node={**node, "config": {**node["config"], **WRITER}})["html"]
    assert "Send and read · 2 channels" in card and 'data-i18n="block.discord.card_write"' in card
    assert "Watching · 2 channels" in block.render_node_card(node=writing)["html"]
    assert "Webhook notifications" in block.render_node_card(node={**node, "config": {**node["config"], **HOOK_ONLY}})["html"]
    single = {**node, "config": {**node["config"], **WRITER, "allowed_channels": [GENERAL]}}
    assert "Send and read · 1 channel<" in block.render_node_card(node=single)["html"]
    assert REF not in card and GENERAL not in card
    assert block.translate("block.discord.card_write", {"count": 3}, lang="fr") == "Envoi et lecture · 3 salons"
    assert block.translate("block.discord.card_watch", {"count": 1}, lang="fr") == "Surveillance · 1 salon"


def main():
    settings_contract()
    credential_contract()
    request_contract()
    provider_contract()
    normalization_contract()
    watch_state_contract()
    block_contract()
    print("[ok] Discord settings, credentials, requests, mentions, embeds, provider errors, buckets, messages, watch state "
          "and hooks")


if __name__ == "__main__":
    main()
