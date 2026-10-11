"""Active Runtime channel watch: one bounded poll per interval, persisted before hand-off.

The cursors are stored before new messages are handed to the runtime, so a
restart never replays them (at-most-once at the Stop boundary). If the
hand-off is refused, the previous cursors are restored and the messages come
again on the next Run. All state lives in this per-hook object; nothing is
kept on the block definition. The bot token is read from the wallet for each
poll.
"""

import time

from . import logic, storage, transport, watch


class Halt(Exception):
    """Stop processing after a refused hand-off or storage failure."""


class ChannelWatcher:
    def __init__(self, config, *, resolve_secret, storage_dir, emit, stop, poll=watch.poll,
                 monotonic=time.monotonic, clock=lambda: int(time.time() * 1000)):
        self.config, self.resolve_secret, self.storage_dir = config, resolve_secret, storage_dir
        self.emit_callback, self.stop, self.poll = emit, stop, poll
        self.monotonic, self.clock = monotonic, clock
        self.stats = {"state": "starting", "channels": len(logic.watched(config)), "polls": 0, "received": 0,
                      "errors": 0, "last_error": None, "caught_up": False}
        self.last_error = None
        self.store, self.document, self.due = None, None, 0.0

    def emit(self, kind, value=None):
        self.emit_callback(kind, value, dict(self.stats))

    def wait(self, seconds):
        end = self.monotonic() + max(0.0, seconds)
        while not self.stop() and self.monotonic() < end:
            time.sleep(min(0.1, max(0.0, end - self.monotonic())))

    def report(self, error, channel=None):
        """Emit each distinct failure once; recovery re-arms the report."""
        self.stats.update(state="backoff", last_error=error.code, errors=self.stats["errors"] + 1)
        if error.code != self.last_error and not self.stop():
            self.last_error = error.code
            try:
                self.emit("error", error.envelope(operation="watch", channel_id=channel, source="watch"))
            except Exception:
                raise Halt() from None

    def idle(self, error):
        """Fail closed: report once, then poll nothing until framework Stop."""
        try:
            self.report(error)
        except Halt:
            pass
        self.stats["state"] = "blocked"
        while not self.stop():
            time.sleep(0.1)

    def run(self):
        try:
            self.store = storage.WatchStore(storage.storage_directory(self.storage_dir))
        except Exception:
            self.idle(logic.DiscordError("storage"))
            return
        try:
            while True:
                try:
                    acquired = self.store.acquire()
                except logic.DiscordError as exc:
                    self.idle(exc)
                    return
                if acquired:
                    break
                try:
                    self.report(logic.DiscordError("busy"))
                except Halt:
                    pass
                self.wait(2)
                if self.stop():
                    return
            try:
                self.document = self.store.load()
            except logic.DiscordError as exc:
                self.idle(exc)
                return
            self.loop()
        except Halt:
            while not self.stop():
                time.sleep(0.1)
        finally:
            self.store.close()

    def loop(self):
        while not self.stop():
            now = self.monotonic()
            if now < self.due:
                self.wait(min(0.5, self.due - now))
                continue
            delay = self.cycle()
            self.due = self.monotonic() + max(self.config["watch_interval_sec"], delay or 0)

    def cycle(self):
        """One poll; returns an extra delay requested by Discord (retry_after) or after an auth failure."""
        try:
            token = logic.bot_token(self.resolve_secret())
        except logic.DiscordError as exc:
            self.report(exc)
            return None
        except Exception:
            self.report(logic.DiscordError("secret"))
            return None
        session = transport.Session(self.config, token, self.stop)
        try:
            updated, found, summary = self.poll(session, self.config, self.document, self.clock())
        except logic.DiscordError as exc:
            if self.stop():
                return None
            self.report(exc)
            # Repeated 401/403 count against Discord's invalid-request limit: slow down.
            return 60 if exc.code == "unauthorized" else min(exc.retry_after or 0, 3600)
        except Exception:
            if self.stop():
                return None
            self.report(logic.DiscordError("internal"))
            return None
        previous = self.document
        try:
            self.store.save(updated)
        except logic.DiscordError as exc:
            self.idle(exc)
            raise Halt() from None
        self.document = updated
        failed = [item for item in summary["channels"] if item.get("error")]
        self.stats.update(state="watching", polls=self.stats["polls"] + 1, caught_up=summary["caught_up"],
                          received=self.stats["received"] + len(found), last_error=None)
        result = {"request_id": None, "operation": "watch", "source": "watch", **summary, "messages": found,
                  "requests": session.requests}
        try:
            if found:
                self.emit("messages", result)
            else:
                self.emit("status")
        except Exception:
            if found:
                # Not handed off: restore the stored cursors so the next Run fetches them again.
                self.stats["received"] -= len(found)
                try:
                    self.store.save(previous)
                    self.document = previous
                except logic.DiscordError:
                    pass
            raise Halt() from None
        if failed:
            first = failed[0]
            self.report(logic.DiscordError(first["error"], http_status=first.get("http_status")), first["channel_id"])
            return 60
        self.last_error = None
        return None
