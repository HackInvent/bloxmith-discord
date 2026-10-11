"""The Discord operations, built from Session calls and pure logic.

Every request is validated and sized before the first exchange. Reads page
through GET /channels/{id}/messages bounded by page_size, max_pages, the
requested limit and the output size. Writes are sent once; only HTTP 429 is
retried, and a create carries a nonce Discord deduplicates for a few minutes.
"""

import secrets
import time
from urllib.parse import urlencode

from . import logic, storage, watch


def send_message(session, request, config, **_):
    body = {**request["body"], "nonce": logic.nonce(request["channel_id"], request["request_id"], secrets.token_hex(12)),
            "enforce_nonce": True}
    message = session.request("POST", logic.channel_path(request["channel_id"]),
                              logic.route_key("POST", "send_message", request["channel_id"]), payload=body, mutation=True)
    return {"sent": logic.sent_receipt(message, request, session.requests)}


def edit_message(session, request, config, **_):
    message = session.request("PATCH", logic.channel_path(request["channel_id"], request["message_id"]),
                              logic.route_key("PATCH", "edit_message", request["channel_id"]),
                              payload=request["body"], mutation=True)
    return {"sent": logic.sent_receipt(message, request, session.requests)}


def delete_message(session, request, config, **_):
    """DELETE answers 204. Discord has no recycle bin for messages: the deletion is permanent."""
    session.request("DELETE", logic.channel_path(request["channel_id"], request["message_id"]),
                    logic.route_key("DELETE", "delete_message", request["channel_id"]), mutation=True, empty=True)
    return {"sent": {"request_id": request["request_id"], "operation": "delete_message",
                     "channel_id": request["channel_id"], "message_id": request["message_id"], "deleted": True,
                     "requests": session.requests}}


def add_reaction(session, request, config, **_):
    session.request("PUT", logic.channel_path(request["channel_id"], request["message_id"], "reactions",
                                              request["emoji_path"], "@me"),
                    logic.route_key("PUT", "add_reaction", request["channel_id"]), mutation=True, empty=True)
    return {"sent": {"request_id": request["request_id"], "operation": "add_reaction",
                     "channel_id": request["channel_id"], "message_id": request["message_id"],
                     "emoji": request["emoji"], "requests": session.requests}}


def read_messages(session, request, config, **_):
    """Newest first by default (pages move backward with before), or forward from after."""
    channel, limit = request["channel_id"], request["limit"]
    forward = request["after"] is not None
    position = request["after"] if forward else request["before"]
    collected, seen, pages, weight, full = [], set(), 0, 2, False
    stop = "end"
    while True:
        if pages >= config["max_pages"]:
            stop = "max_pages"
            break
        size = min(config["page_size"], limit - len(collected))
        query = [("limit", str(size))]
        if position is not None:
            query.append(("after" if forward else "before", position))
        body = session.request("GET", logic.channel_path(channel), logic.route_key("GET", "read_messages", channel),
                               query=urlencode(query), mutation=False)
        pages += 1
        items, raw = logic.page_messages(body)
        full = raw >= size
        fresh = [item for item in items if item["id"] not in seen]
        page_weight = sum(logic.size_of(item) for item in fresh)
        if weight + page_weight > logic.OUTPUT_LIMIT:
            if not collected:
                logic.fail("output_too_large")
            stop = "output_limit"
            break
        weight += page_weight
        for item in fresh:
            seen.add(item["id"])
            collected.append(item)
        if not items or not full:
            break
        position = items[-1]["id"] if forward else items[0]["id"]
        if len(collected) >= limit:
            stop = "limit"
            break
    collected.sort(key=lambda item: int(item["id"]))
    more = stop != "end" and bool(collected)
    result = {"request_id": request["request_id"], "operation": "read_messages", "channel_id": channel,
              "count": len(collected), "messages": collected, "direction": "forward" if forward else "backward",
              "has_more": more, "stop_reason": stop,
              "next_before": collected[0]["id"] if more and not forward else None,
              "next_after": collected[-1]["id"] if more and forward else None,
              "pages": pages, "requests": session.requests}
    return {"messages": result}


def upload_file(session, request, config, located=None, **_):
    """POST multipart/form-data: payload_json (with the attachment) and files[0], streamed from file_root."""
    filename = logic.upload_name(request, located)
    entry = {"id": 0, "filename": filename}
    if request["description"]:
        entry["description"] = request["description"]
    body = {**request["body"], "attachments": [entry],
            "nonce": logic.nonce(request["channel_id"], request["request_id"], secrets.token_hex(12)),
            "enforce_nonce": True}
    upload = {"root": located["root"], "parts": located["parts"], "size": located["size"], "filename": filename,
              "max_bytes": config["max_file_bytes"]}
    message = session.request("POST", logic.channel_path(request["channel_id"]),
                              logic.route_key("POST", "upload_file", request["channel_id"]), payload=body,
                              upload=upload, mutation=True)
    return {"sent": logic.sent_receipt(message, request, session.requests)}


def webhook_send(session, request, config, webhook=None, **_):
    """POST /webhooks/{id}/{token}?wait=true so Discord returns the created message."""
    webhook_id, token = webhook
    query = [("wait", "true")]
    if request["thread_id"]:
        query.append(("thread_id", request["thread_id"]))
    message = session.request("POST", logic.webhook_path(webhook_id, token),
                              logic.route_key("POST", "webhook_send", webhook_id), query=urlencode(query),
                              payload=request["body"], mutation=True)
    return {"sent": logic.sent_receipt(message, request, session.requests)}


def run_watch(session, request, config, storage_dir=None, now_ms=None, **_):
    """One Shot (or a request) runs one bounded poll; the listener runs the same poll on its interval."""
    store = storage.WatchStore(storage.storage_directory(storage_dir))
    if not store.acquire():
        logic.fail("busy")
    try:
        document = store.load()
        updated, found, summary = watch.poll(session, config, document,
                                             now_ms if now_ms is not None else int(time.time() * 1000))
        failed = [item for item in summary["channels"] if item.get("error")]
        if failed and len(failed) == len(summary["channels"]):
            # Nothing could be polled: report the first channel failure instead of an empty success.
            logic.fail(failed[0]["error"], http_status=failed[0].get("http_status"))
        store.save(updated)
    finally:
        store.close()
    result = {"request_id": request["request_id"], "operation": "watch", "source": "request", **summary,
              "messages": found, "requests": session.requests}
    return {"messages": result}


HANDLERS = {"send_message": send_message, "edit_message": edit_message, "delete_message": delete_message,
            "add_reaction": add_reaction, "read_messages": read_messages, "upload_file": upload_file,
            "webhook_send": webhook_send, "watch": run_watch}


def run(session, request, config, **extra):
    return HANDLERS[request["operation"]](session, request, config, **extra)
