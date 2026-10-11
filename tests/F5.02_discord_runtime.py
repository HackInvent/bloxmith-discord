"""FB2-FB6: loopback Discord protocol, writes, reads, uploads, webhook, rate limits, errors, watch, listener and both runtimes.

Every provider exchange targets the loopback double in discord_fixture.py. The
wallet and its obviously fake credentials exist only in disposable test storage.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).resolve().parent)]
from blocs.discord import listener, logic, operations, storage, transport, watch
from blocs.discord.block import DiscordBlock
from block_test_packages import install_test_package, prepare_release_run, surface_payload
from discord_fixture import (BOT, BOT_ID, CUSTOM_EMOJI, FORUM_THREAD, GENERAL, HOOK_REF, MISSING, OTHER_TOKEN, PRIVATE,
                             RANDOM, REF, TOKEN, WEBHOOK_ID, WEBHOOK_TOKEN, discord_api, settings, webhook_url)
from ui_smoke_common import (create_project_api, create_run_api, data_edge, display_node, graph_payload, http_json,
                             isolated_server, stop_run_api, text_node, wait_for_run_predicate, wait_for_run_terminal)

AGENT = "DiscordBot (https://github.com/HackInvent/bloxmith-discord, 0.1.0)"


def config_of(api, **extra):
    return logic.configuration(settings(api, **{"allow_writes": True, **extra}))


def run(config, value, token=TOKEN, cancel=lambda: False, storage_dir=None, now_ms=None, port="message", located=None):
    """Execute one request through the real session and operations, as the block does."""
    request = logic.message_request(value, config) if port == "message" else logic.file_request(value, config)
    extra = {"storage_dir": storage_dir, "now_ms": now_ms}
    if request["operation"] == "webhook_send":
        hook = logic.webhook(token, config)
        session = transport.Session(config, hook[1], cancel, bot=False)
        extra["webhook"] = hook
    else:
        session = transport.Session(config, token, cancel)
    if request["operation"] == "upload_file":
        extra["located"] = located or transport.locate(request["path"], config)
    values = operations.run(session, request, config, **extra)
    return values.get("sent") or values.get("messages")


def failure(call, code):
    try:
        call()
    except logic.DiscordError as exc:
        assert exc.code == code, (code, exc.code, str(exc))
        text = json.dumps(exc.envelope())
        assert TOKEN not in text and WEBHOOK_TOKEN not in text
        return exc
    raise AssertionError("Expected " + code)


def until(predicate, timeout=10.0, label="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(label)


def reset_limits():
    transport._BUCKETS.clear()
    transport._ROUTES.clear()
    transport._GLOBAL.clear()


def contents(api, channel):
    return [item["content"] for item in api.channels[channel]]


def write_protocol():
    """# - FB2 - send, reply, embeds, mentions, nonce deduplication, edit, delete and reactions."""
    reset_limits()
    with discord_api() as api:
        config = config_of(api, allow_delete=True)
        sent = run(config, "Build finished @everyone <@1300000000000000001>")
        call = api.calls[-1]
        assert call["method"] == "POST" and call["path"] == f"/api/v10/channels/{GENERAL}/messages"
        assert call["auth"] == "Bot " + TOKEN and call["agent"] == AGENT and call["accept_encoding"] == "identity"
        assert call["body"]["allowed_mentions"] == {"parse": [], "replied_user": False}
        assert call["body"]["enforce_nonce"] is True and len(call["body"]["nonce"]) == 24
        created = api.channels[GENERAL][-1]
        assert created["content"].startswith("Build finished") and created["mention_everyone"] is False
        assert created["mentions"] == [], "mentions never ping without the Mentions setting"
        assert sent == {"request_id": None, "operation": "send_message", "channel_id": GENERAL,
                        "message_id": created["id"], "guild_id": created["guild_id"], "timestamp": created["timestamp"],
                        "requests": 1}
        pinging = config_of(api, mentions="all")
        run(pinging, "Release @everyone")
        assert api.channels[GENERAL][-1]["mention_everyone"] is True
        picked = run(config_of(api, mentions="users"), {"content": "Hi <@1300000000000000001> <@1300000000000000002>",
                                                         "allowed_mentions": {"users": ["1300000000000000001"]}})
        assert [item["id"] for item in api.channels[GENERAL][-1]["mentions"]] == ["1300000000000000001"]
        reply = run(config, {"channel_id": RANDOM, "content": "Thanks", "reply_to": api.channels[RANDOM][0]["id"],
                             "silent": True, "request_id": "r-1",
                             "embeds": [{"title": "Build 42", "description": "All green", "color": 3066993,
                                         "fields": [{"name": "Tests", "value": "120", "inline": True}]}]})
        message = api.channels[RANDOM][-1]
        assert reply["reply_to"] == api.channels[RANDOM][0]["id"] and message["type"] == 19 and reply["request_id"] == "r-1"
        assert message["flags"] == logic.FLAG_SUPPRESS_NOTIFICATIONS and message["embeds"][0]["title"] == "Build 42"
        # A resend with the same request_id is deduplicated by Discord's nonce: one message only.
        count = len(api.channels[RANDOM])
        again = run(config, {"channel_id": RANDOM, "content": "Thanks", "request_id": "r-1"})
        assert again["message_id"] == reply["message_id"] and len(api.channels[RANDOM]) == count
        assert run(config, {"channel_id": RANDOM, "content": "Thanks", "request_id": "r-2"})["message_id"] != reply["message_id"]
        lost = failure(lambda: run(config, {"content": "x", "reply_to": "1300000000000000077"}), "bad_request")
        assert lost.provider_code == 50035 and lost.provider_errors == ["message_reference: Unknown message"]
        long_title = {"embeds": [{"title": "t" * 200}], "content": "x" * 2000}
        assert run(config, long_title)["operation"] == "send_message"
        edited = run(config, {"operation": "edit_message", "message_id": sent["message_id"], "content": "Build fixed",
                              "request_id": "e-1"})
        call = api.calls[-1]
        assert call["method"] == "PATCH" and call["path"].endswith("/messages/" + sent["message_id"])
        assert call["body"] == {"content": "Build fixed", "allowed_mentions": {"parse": []}}
        assert edited["operation"] == "edit_message" and edited["edited_timestamp"] and edited["request_id"] == "e-1"
        assert next(item for item in api.channels[GENERAL] if item["id"] == sent["message_id"])["content"] == "Build fixed"
        foreign = failure(lambda: run(config, {"operation": "edit_message", "message_id": api.channels[GENERAL][0]["id"],
                                               "content": "hack"}), "forbidden")
        assert foreign.provider_code == 50005 and foreign.http_status == 403 and not foreign.uncertain
        reacted = run(config, {"operation": "add_reaction", "message_id": sent["message_id"], "emoji": "👍"})
        call = api.calls[-1]
        assert call["method"] == "PUT" and call["path"].endswith("/reactions/%F0%9F%91%8D/@me")
        assert reacted == {"request_id": None, "operation": "add_reaction", "channel_id": GENERAL,
                           "message_id": sent["message_id"], "emoji": "👍", "requests": 1}
        run(config, {"operation": "add_reaction", "message_id": sent["message_id"], "emoji": "<:party:" + CUSTOM_EMOJI + ">"})
        assert api.calls[-1]["path"].endswith("/reactions/party:" + CUSTOM_EMOJI + "/@me")
        reactions = next(item for item in api.channels[GENERAL] if item["id"] == sent["message_id"])["reactions"]
        assert [entry["emoji"]["name"] for entry in reactions] == ["👍", "party"]
        unknown = failure(lambda: run(config, {"operation": "add_reaction", "message_id": sent["message_id"],
                                               "emoji": "nope:1500000000000000009"}), "bad_request")
        assert unknown.provider_code == 10014
        removed = run(config, {"operation": "delete_message", "message_id": sent["message_id"]})
        assert removed["deleted"] is True and api.calls[-1]["method"] == "DELETE"
        assert all(item["id"] != sent["message_id"] for item in api.channels[GENERAL])
        gone = failure(lambda: run(config, {"operation": "delete_message", "message_id": sent["message_id"]}), "not_found")
        assert gone.provider_code == 10008 and gone.provider_message == "Unknown Message"
        denied = failure(lambda: run(config, {"content": "x", "channel_id": PRIVATE}), "forbidden")
        assert denied.provider_code == 50001
        assert failure(lambda: run(config, {"content": "x", "channel_id": MISSING}), "not_found").provider_code == 10003
        assert TOKEN not in json.dumps([sent, reply, edited, reacted, removed, picked])
    print("[ok] Discord send, mentions, reply, embeds, nonce, edit, reactions and delete", flush=True)


def read_protocol():
    """# - FB3 - bounded history reads, newest first or forward from after, with exact continuation IDs."""
    reset_limits()
    with discord_api() as api:
        config = config_of(api, page_size=3, allow_writes=False)
        ids = [item["id"] for item in api.channels[GENERAL]]
        read = run(config, {"operation": "read_messages", "limit": 5, "request_id": "q-1"})
        assert [item["id"] for item in read["messages"]] == ids[-5:], "newest five, returned oldest first"
        assert [call["query"] for call in api.calls] == [{"limit": ["3"]}, {"limit": ["2"], "before": [ids[-3]]}]
        assert read["has_more"] and read["next_before"] == ids[-5] and read["next_after"] is None
        assert read["pages"] == 2 and read["direction"] == "backward" and read["stop_reason"] == "limit"
        first = read["messages"][0]
        assert first["author"] == {"id": "1300000000000000001", "username": "ada", "global_name": "Ada", "bot": False}
        assert first["content"] == "history 3" and first["channel_id"] == GENERAL and first["timestamp"].endswith("+00:00")
        rest = run(config, {"operation": "read_messages", "before": read["next_before"]})
        assert [item["id"] for item in rest["messages"]] == ids[:2] and not rest["has_more"] and rest["stop_reason"] == "end"
        forward = run(config, {"operation": "read_messages", "after": ids[0], "limit": 4})
        assert [item["id"] for item in forward["messages"]] == ids[1:5] and forward["next_after"] == ids[4]
        assert api.calls[-1]["query"] == {"limit": ["1"], "after": [ids[3]]}
        tail = run(config, {"operation": "read_messages", "after": forward["next_after"]})
        assert [item["id"] for item in tail["messages"]] == ids[5:] and not tail["has_more"]
        capped = run(config_of(api, page_size=2, max_pages=2), {"operation": "read_messages"})
        assert capped["count"] == 4 and capped["stop_reason"] == "max_pages" and capped["next_before"] == ids[-4]
        everything = run(config_of(api), {"operation": "read_messages", "request_id": "all"})
        assert everything["count"] == 7 and everything["pages"] == 1 and not everything["has_more"]
        empty = run(config, {"operation": "read_messages", "channel_id": RANDOM, "after": api.channels[RANDOM][-1]["id"]})
        assert empty["count"] == 0 and empty["messages"] == [] and not empty["has_more"]
        assert failure(lambda: run(config, {"operation": "read_messages", "channel_id": PRIVATE}), "forbidden").http_status == 403
        size = logic.OUTPUT_LIMIT
        try:
            logic.OUTPUT_LIMIT = 1500
            sized = run(config, {"operation": "read_messages", "limit": 7})
            assert sized["stop_reason"] == "output_limit" and 0 < sized["count"] < 7
            logic.OUTPUT_LIMIT = 50
            failure(lambda: run(config, {"operation": "read_messages"}), "output_too_large")
        finally:
            logic.OUTPUT_LIMIT = size
    print("[ok] Discord history pages, before/after continuation, page and output bounds", flush=True)


def upload_and_webhook_protocol():
    """# - FB2 - # - FB3 - multipart uploads from file_root only, and webhook messages without a bot."""
    reset_limits()
    with discord_api() as api, tempfile.TemporaryDirectory() as folder:
        share = Path(folder).resolve() / "share"
        (share / "reports").mkdir(parents=True)
        (share / "reports" / "q1.csv").write_bytes(b"month,total\njan,12\n")
        (share / "chart.png").write_bytes(b"\x89PNG fixture")
        (share / "big.bin").write_bytes(b"0" * 70000)
        (share / "empty.txt").write_bytes(b"")
        (Path(folder) / "outside.txt").write_text("secret")
        os.symlink(Path(folder) / "outside.txt", share / "link.txt")
        config = config_of(api, file_root=str(share), max_file_bytes=1024 * 1024)
        sent = run(config, {"path": "reports/q1.csv", "content": "Q1 report", "description": "Totals", "request_id": "u-1"},
                   port="file")
        call = api.calls[-1]
        assert call["kind"] == "upload" and call["content_type"].startswith("multipart/form-data; boundary=")
        assert call["body"]["attachments"] == [{"id": 0, "filename": "q1.csv", "description": "Totals"}]
        assert call["body"]["content"] == "Q1 report" and call["body"]["allowed_mentions"]["parse"] == []
        assert call["files"] == [{"field": "files[0]", "filename": "q1.csv", "size": 19,
                                  "data": b"month,total\njan,12\n", "content_type": "text/csv"}]
        assert sent["operation"] == "upload_file" and sent["attachments"][0]["filename"] == "q1.csv"
        assert sent["attachments"][0]["size"] == 19 and sent["attachments"][0]["description"] == "Totals"
        spoiler = run(config, {"path": str(share / "chart.png"), "spoiler": True, "filename": "graph.png"}, port="file")
        assert spoiler["attachments"][0]["filename"] == "SPOILER_graph.png"
        assert api.calls[-1]["files"][0]["content_type"] == "image/png"
        before = len(api.calls)
        for path, code in (("missing.txt", "file_not_found"), ("../outside.txt", "file_not_allowed"),
                           ("link.txt", "file_not_allowed"), (str(Path(folder) / "outside.txt"), "file_not_allowed"),
                           ("empty.txt", "file_empty"), ("reports", "file_not_allowed")):
            failure(lambda path=path: run(config, {"path": path}, port="file"), code)
        failure(lambda: run(config_of(api, file_root=str(share), max_file_bytes=1024), "big.bin", port="file"),
                "file_too_large")
        assert len(api.calls) == before, "refused files send nothing"
        too_big = failure(lambda: run(config, "big.bin", port="file"), "provider_too_large")
        assert too_big.provider_code == 40005 and not too_big.uncertain
        located = transport.locate("chart.png", config)
        (share / "chart.png").write_bytes(b"\x89PNG changed size")
        before = len(api.calls)
        failure(lambda: run(config, "chart.png", port="file", located=located), "file_changed")
        assert len(api.calls) == before
        failure(lambda: run(config_of(api), "chart.png", port="file"), "file_root_required")
        # Webhook: the ID and token come from the wallet URL; requests go to the configured origin.
        hook = logic.configuration({**settings(api), "secret_ref": "", "allowed_channels": [], "default_channel": "",
                                    "webhook_secret_ref": HOOK_REF, "default_operation": "webhook_send",
                                    "allow_writes": True})
        notified = run(hook, {"content": "Deployed v2 @everyone", "username": "Release bot", "request_id": "h-1"},
                       token=webhook_url(api))
        call = api.calls[-1]
        assert call["kind"] == "webhook" and call["path"] == f"/api/v10/webhooks/{WEBHOOK_ID}/<token>"
        assert call["webhook_token"] == WEBHOOK_TOKEN and call["auth"] is None and call["query"] == {"wait": ["true"]}
        assert call["body"] == {"content": "Deployed v2 @everyone", "allowed_mentions": {"parse": [], "replied_user": False},
                                "username": "Release bot"}
        message = api.channels[GENERAL][-1]
        assert notified == {"request_id": "h-1", "operation": "webhook_send", "channel_id": GENERAL,
                            "message_id": message["id"], "guild_id": message["guild_id"], "timestamp": message["timestamp"],
                            "requests": 1, "webhook_id": WEBHOOK_ID}
        assert message["webhook_id"] == WEBHOOK_ID and message["mention_everyone"] is False
        threaded = run(hook, {"content": "In thread", "thread_id": FORUM_THREAD}, token=webhook_url(api))
        assert api.calls[-1]["query"] == {"wait": ["true"], "thread_id": [FORUM_THREAD]}
        assert threaded["channel_id"] == FORUM_THREAD and threaded["thread_id"] == FORUM_THREAD
        bad = failure(lambda: run(hook, "x", token=webhook_url(api, token="-".join(("wrong", "webhook", "token", "0123456789")))),
                      "unauthorized")
        assert bad.provider_code == 50027
        assert failure(lambda: run(hook, "x", token=webhook_url(api, ident="1400000000000000009")),
                       "not_found").provider_code == 10015
        text = json.dumps([notified, threaded])
        assert WEBHOOK_TOKEN not in text and TOKEN not in text
    print("[ok] Discord multipart upload from file_root, refusals and webhook execution", flush=True)


def error_protocol():
    """# - FB4 - buckets, 429 retry_after, 5xx, lost connections, oversized bodies, cancellation, deadlines and TLS."""
    reset_limits()
    with discord_api() as api:
        config = config_of(api, max_retry_wait_sec=3)
        reading = {"operation": "read_messages", "limit": 2}
        expired = failure(lambda: run(config, "x", OTHER_TOKEN), "unauthorized")
        assert expired.http_status == 401 and not expired.uncertain
        # A bucket reported exhausted (remaining 0) delays the next call of that route only.
        api.bucket_limit = 2
        started = time.monotonic()
        for index in range(3):
            run(config, f"paced {index}")
        times = [call["at"] for call in api.of("send")[-3:]]
        assert times[2] - times[1] >= 0.9 and time.monotonic() - started >= 0.9, times
        before = time.monotonic()
        run(config, {"content": "other channel", "channel_id": RANDOM})
        assert time.monotonic() - before < 0.5, "another channel has its own bucket"
        api.reset_after = "5.0"
        run(config, "sets a long reset")
        before = len(api.calls)
        refused = failure(lambda: run(config, "x"), "rate_limited")
        assert 3 < refused.retry_after <= 5 and len(api.calls) == before, "a reset above the bound is reported, not awaited"
        api.reset_after = "1.0"
        api.bucket_limit = 50
        api.buckets.clear()
        reset_limits()
        # 429: retry_after is honoured within bounds for reads and writes; above the bound it is reported.
        limited = {"status": 429, "body": {"message": "You are being rate limited.", "retry_after": 0.6, "global": False},
                   "headers": {"Retry-After": "1", "X-RateLimit-Scope": "user"}}
        api.script["send"] = [limited]
        started = time.monotonic()
        assert run(config, "after 429")["requests"] == 2 and time.monotonic() - started >= 0.55
        assert api.kinds()[-2:] == ["send", "send"]
        api.script["read"] = [limited]
        assert run(config, reading)["requests"] == 2
        api.script["send"] = [{"status": 429, "body": {"message": "Slow down", "retry_after": 9.5, "global": False},
                               "headers": {"X-RateLimit-Scope": "shared"}}]
        slow = failure(lambda: run(config, "x"), "rate_limited")
        assert slow.retry_after == 9.5 and slow.attempts == 1 and slow.scope == "shared"
        reset_limits()
        api.script["send"] = [{"status": 429, "body": {"message": "Global", "retry_after": 0.4, "global": True},
                               "headers": {"X-RateLimit-Global": "true", "X-RateLimit-Scope": "global"}}]
        assert run(config, "after global")["requests"] == 2
        api.script["send"] = [limited] * 3
        assert failure(lambda: run(config_of(api, max_retries=0), "x"), "rate_limited").attempts == 1
        api.script["send"] = []
        reset_limits()
        # Reads retry 5xx and lost connections; writes never replay an ambiguous outcome.
        api.script["read"] = ["html502", "lost"]
        before = len(api.calls)
        assert run(config, reading)["count"] == 2 and api.kinds()[before:] == ["read"] * 3
        for scripted, code in (({"status": 503, "body": {"message": "Service Unavailable", "code": 0}},
                                "provider_unavailable"), ("html502", "provider_unavailable"), ("lost", "network"),
                               ("oversized", "protocol"), ("gzip", "protocol")):
            api.script["send"] = [scripted]
            before = len(api.calls)
            outcome = failure(lambda: run(config_of(api, max_response_bytes=65536), "x"), code)
            assert outcome.uncertain and api.kinds()[before:] == ["send"], scripted
        api.script["read"] = ["oversized"]
        assert not failure(lambda: run(config_of(api, max_response_bytes=65536, max_retries=0), reading),
                           "protocol").uncertain
        api.script["send"] = [{"status": 400, "body": {"message": "Invalid Form Body", "code": 50035, "errors": {
            "content": {"_errors": [{"code": "BASE_TYPE_MAX_LENGTH", "message": "Must be 2000 or fewer in length."}]}}}}]
        form = failure(lambda: run(config, "x"), "bad_request")
        assert form.provider_errors == ["content: Must be 2000 or fewer in length."] and not form.uncertain
        # Cancellation and the deadline shut the socket down while Discord holds the request.
        api.script["send"] = ["hold"]
        started = time.monotonic()
        cancelled = failure(lambda: run(config, "x", cancel=lambda: api.holding >= 1), "cancelled")
        assert cancelled.uncertain and time.monotonic() - started < 2.5
        api.release.set()
        time.sleep(0.2)
        api.release.clear()
        api.script["read"] = ["hold"]
        quiet = failure(lambda: run(config, reading, cancel=lambda: api.holding >= 1), "cancelled")
        assert not quiet.uncertain
        api.release.set()
        time.sleep(0.2)
        api.release.clear()
        api.script["send"] = ["hold"]
        started = time.monotonic()
        timed = failure(lambda: run(config_of(api, timeout_sec=5), "x"), "timeout")
        assert timed.uncertain and 4 <= time.monotonic() - started < 8
        api.release.set()
        time.sleep(0.2)
        assert not [thread for thread in threading.enumerate() if thread.name == transport.THREAD_NAME]
        assert not failure(lambda: run(config_of(api, api_base_url="http://127.0.0.1:9", max_retries=0), reading),
                           "network").uncertain
        before = len(api.calls)
        failure(lambda: run(config, "x", cancel=lambda: True), "cancelled")
        assert len(api.calls) == before
    with discord_api(tls=True) as api:
        assert run(config_of(api), "over TLS")["operation"] == "send_message"
        before = len(api.calls)
        untrusted = failure(lambda: run(config_of(api, ca_file=""), "x"), "tls")
        assert not untrusted.uncertain and len(api.calls) == before
    print("[ok] Discord buckets, 429 retry_after, retries, uncertainty, cancellation, deadline and TLS", flush=True)


def watch_protocol():
    """# - FB5 - snowflake cursors: no flood on first start, each message once, bot filter, bounds, errors, One Shot poll."""
    reset_limits()
    with discord_api() as api, tempfile.TemporaryDirectory() as folder:
        node = Path(folder).resolve()
        config = config_of(api, watch=True, watch_channels=[GENERAL, RANDOM], page_size=3, watch_max_items=4)

        def poll(document, conf=config):
            return watch.poll(transport.Session(conf, TOKEN), conf, document, int(time.time() * 1000))

        newest = api.channels[GENERAL][-1]["id"]
        before = len(api.calls)
        document, found, summary = poll(logic.empty_state())
        assert found == [] and summary["started"] and summary["count"] == 0 and summary["caught_up"]
        assert [call["query"] for call in api.calls[before:]] == [{"limit": ["1"]}, {"limit": ["1"]}]
        cursors = document["watches"][logic.watch_key(config)]["channels"]
        assert cursors == {GENERAL: newest, RANDOM: api.channels[RANDOM][-1]["id"]}
        assert [item["cursor"] for item in summary["channels"]] == [newest, api.channels[RANDOM][-1]["id"]]
        random_cursor = api.channels[RANDOM][-1]["id"]
        first = api.add(GENERAL, "new one")
        mine = api.add(RANDOM, "from the bot", author=BOT)
        second = api.add(RANDOM, "new two")
        document, found, summary = poll(document)
        assert [item["id"] for item in found] == [first["id"], second["id"]] and not summary["started"]
        assert found[0]["content"] == "new one" and found[1]["channel_id"] == RANDOM
        assert summary["channels"][1]["skipped"] == 1, "bot and webhook messages are skipped by default"
        assert api.calls[-1]["query"] == {"after": [random_cursor], "limit": ["3"]} and mine["author"]["bot"]
        assert poll(document)[1] == [], "each message is handed over once"
        with_bots = config_of(api, watch=True, watch_channels=[RANDOM], watch_include_bots=True)
        state, _found, _summary = poll(logic.empty_state(), with_bots)
        bot_line = api.add(RANDOM, "bot again", author=BOT)
        assert [item["id"] for item in poll(state, with_bots)[1]] == [bot_line["id"]]
        # A burst is handed over in bounded polls (watch_max_items across channels), oldest first, none skipped.
        burst = [api.add(GENERAL, f"burst {index}")["id"] for index in range(9)]
        batches = []
        for _ in range(3):
            document, found, summary = poll(document)
            batches.append([item["id"] for item in found])
        assert [len(batch) for batch in batches] == [4, 4, 1] and sum(batches, []) == burst, batches
        assert summary["caught_up"] and summary["channels"][1]["skipped"] == 1, "the bot line is skipped, not emitted"
        narrow = config_of(api, watch=True, watch_channels=[GENERAL], page_size=2, max_pages=1, watch_max_items=100)
        state, _found, _summary = poll(logic.empty_state(), narrow)
        lines = [api.add(GENERAL, f"page {index}")["id"] for index in range(5)]
        handed = []
        for _ in range(3):
            state, found, summary = poll(state, narrow)
            handed.extend(item["id"] for item in found)
        assert handed == lines and summary["caught_up"]
        # emit_existing: the first start reads the recent window once, bounded per poll.
        recent = config_of(api, watch=True, watch_channels=[GENERAL], emit_existing=True, existing_since_hours=1,
                           watch_max_items=100)
        fresh, found, summary = poll(logic.empty_state(), recent)
        window = [item["id"] for item in api.channels[GENERAL] if not item["content"].startswith("history")]
        assert [item["id"] for item in found] == window and summary["started"]
        assert "history 1" not in [item["content"] for item in found], "older messages stay outside the window"
        assert poll(fresh, recent)[1] == []
        # A channel without access is reported; the others are still polled and keep their cursors.
        mixed = config_of(api, watch=True, watch_channels=[GENERAL, PRIVATE])
        state, _found, summary = poll(logic.empty_state(), mixed)
        assert summary["channels"][1]["error"] == "forbidden" and not summary["caught_up"]
        assert set(state["watches"][logic.watch_key(mixed)]["channels"]) == {GENERAL}
        late = api.add(GENERAL, "still watched")
        state, found, summary = poll(state, mixed)
        assert [item["id"] for item in found] == [late["id"]] and summary["channels"][1]["http_status"] == 403
        failure(lambda: watch.poll(transport.Session(mixed, OTHER_TOKEN), mixed, state, 0), "unauthorized")
        # One Shot or a request: one poll with cursors persisted in node storage under a lock.
        first = run(config, {"operation": "watch", "request_id": "w-1"}, storage_dir=lambda: node)
        assert first["messages"] == [] and first["started"] and first["source"] == "request"
        assert first["request_id"] == "w-1" and first["operation"] == "watch"
        posted = api.add(RANDOM, "between polls")
        second = run(config, {"operation": "watch"}, storage_dir=lambda: node)
        assert [item["id"] for item in second["messages"]] == [posted["id"]] and not second["started"]
        assert run(config, {"operation": "watch"}, storage_dir=lambda: node)["messages"] == []
        state_text = (node / storage.STATE_NAME).read_text()
        assert TOKEN not in state_text and REF not in state_text and "between polls" not in state_text
        holder = storage.WatchStore(node)
        assert holder.acquire()
        try:
            failure(lambda: run(config, {"operation": "watch"}, storage_dir=lambda: node), "busy")
        finally:
            holder.close()
        failure(lambda: run(config, {"operation": "watch"}), "storage")
        blocked = config_of(api, watch=True, watch_channels=[PRIVATE])
        failure(lambda: run(blocked, {"operation": "watch"}, storage_dir=lambda: node), "forbidden")
        (node / storage.STATE_NAME).write_text('{"schema_version": 1, "watches": {"x": 1}}')
        failure(lambda: run(config, {"operation": "watch"}, storage_dir=lambda: node), "state_invalid")
    print("[ok] Discord watch cursors, no flood, bot filter, bounded polls, window, channel errors, One Shot poll and lock",
          flush=True)


def listener_protocol():
    """# - FB5 - the Active Runtime watcher persists before hand-off, never replays after restart and stops promptly."""
    reset_limits()
    with discord_api() as api, tempfile.TemporaryDirectory() as folder:
        node = Path(folder).resolve()
        # The 10 s minimum is a settings rule; the watcher object itself is driven faster here.
        fast = {**config_of(api, watch=True, watch_channels=[GENERAL]), "watch_interval_sec": 0.3}

        def start(emit, secret=lambda: TOKEN, directory=lambda: str(node)):
            stop = threading.Event()
            watcher = listener.ChannelWatcher(fast, resolve_secret=secret, storage_dir=directory, emit=emit,
                                              stop=stop.is_set)
            thread = threading.Thread(target=watcher.run, name="discord-test-watcher")
            thread.start()
            return stop, thread

        def finish(stop, thread):
            started = time.monotonic()
            stop.set()
            thread.join(3)
            assert not thread.is_alive() and time.monotonic() - started < 1.5, "The watcher must stop promptly"

        emitted = []
        stop, thread = start(lambda kind, value, stats: emitted.append((kind, value, stats)))
        try:
            until(lambda: any(kind == "status" for kind, _v, _s in emitted), label="first poll missing")
            assert not any(kind == "messages" for kind, _v, _s in emitted), "The first start never floods"
            live = api.add(GENERAL, "live message")
            until(lambda: any(kind == "messages" for kind, _v, _s in emitted), label="message not handed over")
            kind, result, stats = next(item for item in emitted if item[0] == "messages")
            assert [item["id"] for item in result["messages"]] == [live["id"]] and result["source"] == "watch"
            assert result["operation"] == "watch" and result["count"] == 1 and stats["state"] == "watching"
            assert stats["received"] == 1
            time.sleep(1)
            assert sum(1 for item in emitted if item[0] == "messages") == 1, "Each message is handed over once"
        finally:
            finish(stop, thread)
        offline = api.add(GENERAL, "posted while stopped")
        emitted.clear()
        stop, thread = start(lambda kind, value, stats: emitted.append((kind, value, stats)))
        try:
            until(lambda: any(kind == "messages" for kind, _v, _s in emitted), label="restart lost a message")
            result = next(v for k, v, _s in emitted if k == "messages")
            assert [item["id"] for item in result["messages"]] == [offline["id"]], "A restart never replays"
            failure(lambda: run(logic.configuration({**fast, "watch_interval_sec": 10}), {"operation": "watch"},
                                storage_dir=lambda: node), "busy")
        finally:
            finish(stop, thread)
        # A refused hand-off restores the stored cursor: the next Run hands the same message over again.
        refused_message = api.add(GENERAL, "refused once")
        refused = []

        def refusing(kind, value, stats):
            refused.append(kind)
            if kind == "messages":
                raise RuntimeError("runtime_listeners.queue_full")

        stop, thread = start(refusing)
        try:
            until(lambda: "messages" in refused, label="refused hand-off not attempted")
        finally:
            finish(stop, thread)
        emitted.clear()
        stop, thread = start(lambda kind, value, stats: emitted.append((kind, value, stats)))
        try:
            until(lambda: any(kind == "messages" for kind, _v, _s in emitted), label="refused message lost")
            result = next(v for k, v, _s in emitted if k == "messages")
            assert [item["id"] for item in result["messages"]] == [refused_message["id"]]
        finally:
            finish(stop, thread)
        # Failures are reported once per distinct code; Stop interrupts a held poll at once.
        errors = []
        stop, thread = start(lambda kind, value, stats: errors.append((kind, value)), secret=lambda: "short")
        try:
            until(lambda: errors, label="secret error missing")
            time.sleep(1)
            assert [value["code"] for kind, value in errors if kind == "error"] == ["secret"] and len(errors) == 1
            assert errors[0][1]["source"] == "watch" and errors[0][1]["operation"] == "watch"
        finally:
            finish(stop, thread)
        errors.clear()
        stop, thread = start(lambda kind, value, stats: errors.append((kind, value)),
                             directory=lambda: (_ for _ in ()).throw(RuntimeError("block_storage.missing_scope")))
        try:
            until(lambda: errors, label="storage error missing")
            assert errors[0][1]["code"] == "storage" and errors[0][1]["source"] == "watch"
        finally:
            finish(stop, thread)
        errors.clear()
        api.tokens.discard(TOKEN)
        stop, thread = start(lambda kind, value, stats: errors.append((kind, value)))
        try:
            until(lambda: errors, label="auth error missing")
            assert errors[0][1]["code"] == "unauthorized" and TOKEN not in json.dumps(errors)
        finally:
            finish(stop, thread)
            api.tokens.add(TOKEN)
        api.script["read"] = ["hold"]
        stop, thread = start(lambda kind, value, stats: None)
        try:
            until(lambda: api.holding == 1, label="held poll not reached")
        finally:
            finish(stop, thread)
            api.release.set()
        time.sleep(0.2)
        assert not [item for item in threading.enumerate() if item.name == transport.THREAD_NAME]
    print("[ok] Discord watcher hand-off, restart without replay, refused hand-off, errors and Stop", flush=True)


def value(data, port):
    raw = data.get("output_values", {}).get("chat:" + str(port), {}).get("value")
    return json.loads(raw) if raw else None


def wallet(server, api):
    http_json(server.base_url, "/api/application/secrets/init", method="POST", payload={"password": "disposable-discord-fixture"})
    http_json(server.base_url, "/api/application/secrets", method="POST", payload={"name": "discord_fixture", "value": TOKEN})
    http_json(server.base_url, "/api/application/secrets", method="POST",
              payload={"name": "discord_hook_fixture", "value": webhook_url(api)})


def graph_runs():
    """# - FB5 - # - FB6 - real graphs in One Shot and Active Runtime for bundled, managed and linked packages, with prompt Stop."""
    block = DiscordBlock()
    for mode in ("centralized", "zeromq_active"):
        for origin in (None, "managed", "linked"):
            full = origin is None
            reset_limits()
            with discord_api() as api, isolated_server() as server:
                share = server.root_dir / "share"
                share.mkdir()
                (share / "invoice.txt").write_bytes(b"invoice fixture")
                node = block.build_node_payload(node_id="chat", position={"x": 380, "y": 160}, config_overrides=settings(
                    api, allow_writes=True, watch=True, watch_channels=[GENERAL], emit_existing=True,
                    existing_since_hours=1, file_root=str(share), webhook_secret_ref=HOOK_REF, page_size=3))
                if origin:
                    model = install_test_package(server, "discord", origin=origin)
                    node["block_version"] = model["version"]
                    for surface in ("modal", "inspector_panel", "node_card"):
                        html = surface_payload(server, model, node, surface=surface)["html"]
                        assert "{{" not in html and TOKEN not in html
                        if surface != "node_card":
                            assert 'data-block-config-field="allowed_channels"' in html
                wallet(server, api)
                sinks = [display_node("inbox", "Messages", 760, 40), display_node("receipt", "Sent", 760, 200),
                         display_node("problem", "Problem", 760, 360)]
                edges = [data_edge("o1", "chat", 1, "inbox", 1), data_edge("o2", "chat", 2, "receipt", 1),
                         data_edge("o3", "chat", 3, "problem", 1)]

                def graph(text, port=1):
                    return graph_payload("Discord", [text_node("source", "Source", text, 60, 160), node, *sinks],
                                         [data_edge("in", "source", 1, "chat", port), *edges])

                if mode == "centralized":
                    def once(document, project=None):
                        project = project or create_project_api(server, document=document)["project"]
                        created = create_run_api(server, document, project_id=project["graph_id"], runtime_mode=mode)
                        data = wait_for_run_terminal(server, created["run_id"], timeout_sec=40)
                        assert data["status"] == "success", (origin, data.get("status"), data.get("logs"))
                        text = json.dumps(data)
                        assert TOKEN not in text and WEBHOOK_TOKEN not in text
                        return data, project

                    data, _ = once(graph("Build finished"))
                    assert value(data, 2)["operation"] == "send_message" and api.channels[GENERAL][-1]["content"] == "Build finished"
                    data, _ = once(graph(json.dumps({"operation": "read_messages", "limit": 4, "request_id": "q-1"})))
                    assert value(data, 1)["count"] == 4 and value(data, 1)["messages"][-1]["content"] == "Build finished"
                    # One Shot never starts the watcher: a watch request polls once per run, without replay across runs.
                    # The block's own message is a bot message, so the watch skips it.
                    api.add(GENERAL, "recent question")
                    document = graph(json.dumps({"operation": "watch"}))
                    data, project = once(document)
                    assert value(data, 1)["started"] and [item["content"] for item in value(data, 1)["messages"]] == \
                        ["recent question"]
                    api.add(GENERAL, "between runs")
                    data, _ = once(document, project=project)
                    assert [item["content"] for item in value(data, 1)["messages"]] == ["between runs"]
                    if full:
                        data, _ = once(graph(json.dumps({"path": "invoice.txt", "content": "Invoice"}), port=2))
                        assert value(data, 2)["attachments"][0]["filename"] == "invoice.txt"
                        data, _ = once(graph(json.dumps({"operation": "webhook_send", "content": "Hooked"})))
                        assert value(data, 2)["webhook_id"] == WEBHOOK_ID
                        before = len(api.calls)
                        data, _ = once(graph(json.dumps({"operation": "delete_message", "message_id": GENERAL})))
                        assert value(data, 3)["code"] == "delete_disabled" and len(api.calls) == before
                        assert data["results"]["chat"]["discord"]["code"] == "delete_disabled"
                        http_json(server.base_url, "/api/application/secrets/lock", method="POST", payload={})
                        data, _ = once(graph("locked"))
                        assert value(data, 3)["code"] == "secret" and len(api.calls) == before
                        http_json(server.base_url, "/api/application/secrets/unlock", method="POST",
                                  payload={"password": "disposable-discord-fixture"})
                        # Stop while Discord holds a read: the run ends promptly and nothing is retried.
                        api.script["read"] = ["hold"]
                        document = graph(json.dumps({"operation": "read_messages"}))
                        project = create_project_api(server, document=document)["project"]
                        rid = create_run_api(server, document, project_id=project["graph_id"], runtime_mode=mode)["run_id"]
                        until(lambda: api.holding == 1, 20, "held read not reached")
                        held, started = len(api.calls), time.monotonic()
                        stop_run_api(server, rid)
                        stopped = wait_for_run_terminal(server, rid, timeout_sec=20)
                        assert time.monotonic() - started < 10, stopped.get("logs")
                        assert stopped["status"] == "cancelled", (stopped["status"], value(stopped, 3), stopped.get("logs"))
                        assert value(stopped, 1) is None, "a stopped read publishes nothing"
                        assert len(api.calls) == held, "the held read is neither retried nor followed by another request"
                        assert "shutdown_timeout" not in json.dumps(stopped.get("logs"))
                        api.release.set()
                else:
                    document = graph("")
                    project = create_project_api(server, document=document)["project"]

                    def publish(rid, text):
                        http_json(server.base_url, f"/api/runs/{rid}/active/control", method="POST", payload={
                            "action": "publish_output", "node_id": "source", "port_id": 1, "value": text,
                            "content_type": "text/plain"})

                    def wait(rid, predicate, label):
                        data = wait_for_run_predicate(server, rid, lambda item: item.get("status") == "failed" or predicate(item),
                                                      label, timeout_sec=30)
                        assert data.get("status") != "failed", data.get("logs")
                        text = json.dumps(data)
                        assert TOKEN not in text and WEBHOOK_TOKEN not in text
                        return data

                    def stop(rid):
                        started = time.monotonic()
                        stop_run_api(server, rid)
                        stopped = wait_for_run_terminal(server, rid, timeout_sec=20)
                        assert time.monotonic() - started < 10, stopped.get("logs")
                        assert "shutdown_timeout" not in json.dumps(stopped.get("logs"))
                        return stopped

                    def watched(item):
                        result = value(item, 1) or {}
                        return result.get("source") == "watch" and result.get("operation") == "watch"

                    api.add(GENERAL, "recent before start")
                    rid = prepare_release_run(server, project["graph_id"], document)["run_id"]
                    http_json(server.base_url, f"/api/runs/{rid}/play", method="POST", payload={})
                    try:
                        data = wait(rid, watched, "watched messages missing")
                        assert [item["content"] for item in value(data, 1)["messages"]] == ["recent before start"]
                        publish(rid, json.dumps({"content": "Reply from graph", "request_id": "live-1"}))
                        data = wait(rid, lambda item: (value(item, 2) or {}).get("request_id") == "live-1", "send missing")
                        assert api.channels[GENERAL][-1]["content"] == "Reply from graph"
                        publish(rid, json.dumps({"operation": "read_messages", "limit": 2, "request_id": "live-2"}))
                        data = wait(rid, lambda item: (value(item, 1) or {}).get("request_id") == "live-2", "read missing")
                        assert value(data, 1)["messages"][-1]["content"] == "Reply from graph"
                        if full:
                            publish(rid, json.dumps({"request_id": "live-3", "operation": "watch"}))
                            data = wait(rid, lambda item: (value(item, 3) or {}).get("request_id") == "live-3", "busy missing")
                            assert value(data, 3)["code"] == "busy", "The listener owns the cursor while the Run is loaded"
                    finally:
                        stop(rid)
                    if full:
                        posted = api.add(GENERAL, "between active runs")
                        rid = prepare_release_run(server, project["graph_id"], document)["run_id"]
                        http_json(server.base_url, f"/api/runs/{rid}/play", method="POST", payload={})
                        try:
                            data = wait(rid, watched, "restart lost a message")
                            assert [item["id"] for item in value(data, 1)["messages"]] == [posted["id"]], \
                                "A restart never replays; the block's own reply is skipped as a bot message"
                            api.script["read"] = ["hold"]
                            publish(rid, json.dumps({"operation": "read_messages", "request_id": "live-4"}))
                            until(lambda: api.holding >= 1, 20, "held read not reached")
                        finally:
                            stop(rid)
                            api.release.set()
                assert TOKEN not in "\n".join(server.logs) and WEBHOOK_TOKEN not in "\n".join(server.logs)
            print("[ok] Discord graph " + mode + " " + str(origin), flush=True)


def main():
    write_protocol()
    read_protocol()
    upload_and_webhook_protocol()
    error_protocol()
    watch_protocol()
    listener_protocol()
    graph_runs()


if __name__ == "__main__":
    main()
