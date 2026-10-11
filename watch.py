"""New-message polling of allowlisted channels, bounded per poll.

A watch keeps one cursor per channel in node storage: the snowflake ID of the
last message already handed over (or skipped). Snowflakes grow with time
within a channel, so `GET /channels/{id}/messages?after=<cursor>` returns
exactly the messages that are new, and the cursor alone prevents a replay.

- First start of a channel: the cursor is the ID of the newest message, read
  from Discord (so the local clock does not matter), and nothing is emitted.
  With emit_existing, the cursor is the snowflake of `existing_since_hours`
  ago and those messages are emitted, watch_max_items per poll.
- Each poll reads at most max_pages pages per channel and emits at most
  watch_max_items messages in total; the next poll continues from the cursor.
- A channel that answers 403 or 404 keeps its cursor and is reported; the
  other channels are still polled. Other failures abort the poll unchanged.

Nothing is persisted here: the caller stores the returned document before it
hands the messages over, so a restart never replays them.
"""

from urllib.parse import urlencode

from . import logic

CHANNEL_ERRORS = ("forbidden", "not_found")


def start_cursor(session, config, channel, now_ms):
    """The first position of a channel: Discord's newest message, or the emit_existing window."""
    if config["emit_existing"]:
        return logic.snowflake_at(now_ms - config["existing_since_hours"] * 3600000)
    body = session.request("GET", logic.channel_path(channel), logic.route_key("GET", "watch", channel),
                           query=urlencode([("limit", "1")]), mutation=False)
    items, _raw = logic.page_messages(body)
    return items[-1]["id"] if items else "0"


def poll_channel(session, config, channel, cursor, budget):
    """Read new messages after cursor; returns (messages, cursor, pages, skipped, caught_up)."""
    found, pages, skipped = [], 0, 0
    size = config["page_size"]
    while True:
        if budget - len(found) <= 0:
            return found, cursor, pages, skipped, False
        if pages >= config["max_pages"]:
            return found, cursor, pages, skipped, False
        body = session.request("GET", logic.channel_path(channel), logic.route_key("GET", "watch", channel),
                               query=urlencode([("after", cursor), ("limit", str(size))]), mutation=False)
        pages += 1
        items, raw = logic.page_messages(body)
        fresh = [item for item in items if int(item["id"]) > int(cursor)]
        for index, item in enumerate(fresh):
            if len(found) >= budget:
                return found, cursor, pages, skipped, False
            cursor = item["id"]
            author = item["author"] or {}
            if author.get("bot") and not config["watch_include_bots"]:
                skipped += 1
                continue
            found.append(item)
        if raw < size or not fresh:
            return found, cursor, pages, skipped, True


def poll(session, config, document, now_ms):
    """One bounded poll of every watched channel; returns (new document, messages, summary)."""
    key = logic.watch_key(config)
    entry = document["watches"].get(key) or {"channels": {}, "updated_at": 0}
    cursors, found, channels = {}, [], []
    budget = config["watch_max_items"]
    started_any = False
    for channel in logic.watched(config):
        previous = entry["channels"].get(channel)
        report = {"channel_id": channel, "count": 0, "started": previous is None, "caught_up": True,
                  "pages": 0, "skipped": 0}
        try:
            cursor = previous if previous is not None else start_cursor(session, config, channel, now_ms)
            if previous is not None or config["emit_existing"]:
                items, cursor, pages, skipped, caught = poll_channel(session, config, channel, cursor,
                                                                     budget - len(found))
                found.extend(items)
                report.update(count=len(items), pages=pages, skipped=skipped, caught_up=caught)
        except logic.DiscordError as exc:
            if exc.code not in CHANNEL_ERRORS:
                raise
            report.update(error=exc.code, http_status=exc.http_status, caught_up=False)
            if previous is not None:
                cursors[channel] = previous
            channels.append(report)
            continue
        started_any = started_any or report["started"]
        cursors[channel] = cursor
        report["cursor"] = cursor
        channels.append(report)
    found.sort(key=lambda item: int(item["id"]))
    summary = {"count": len(found), "started": started_any, "caught_up": all(item["caught_up"] for item in channels),
               "channels": channels}
    return logic.with_watch(document, key, cursors, now_ms // 1000), found, summary
