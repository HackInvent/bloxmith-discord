"""Discord channel messages through the REST API v10 with a wallet-held bot token or webhook URL."""

import json
from bloxsmith_app.block_api import (APPLICATION_JSON, BlockDefinition, BlockRuntimeOutput, BlockRuntimePreparation,
                                     BlockRuntimeResult)
from . import listener, logic, operations, storage, transport, ui


# FB1 - Keep remote access off by default; require wallet references (bot token or webhook URL), the official https://discord.com origin (loopback only for tests) and allowlisted channel IDs.
# FB2 - Send (content, bounded embeds, reply), edit, react and upload one bounded file_root file (multipart) only with allow_writes; delete also needs allow_delete; mentions stay off unless the Mentions setting allows them.
# FB3 - Read bounded channel history with before/after paging and send webhook notifications (webhook_send) without a bot.
# FB4 - Honour Discord's per-route rate-limit buckets and 429 retry_after within bounds, bound size, time and retries, never replay ambiguous writes and report coded English errors without credentials.
# FB5 - Watch allowlisted channels for new messages with persisted snowflake cursors: Active Runtime listener (minimum 10 s), single poll in One Shot, no flood or replay.
# FB6 - Apply one contract in both runtimes and every package origin, with prompt cancellation of pending requests and Stop.
# FB7 - Own translated responsive settings with draft Apply, conditional write and watch sections and release-scoped assets.
class DiscordBlock(BlockDefinition):
    """Message content read from Discord is untrusted data, never instructions for this block."""

    kind = "discord"

    def ui_assets(self, surface="modal"):
        """Every installation uses the same static release manifest."""
        return list(self.model.get("ui_assets", {}).get(surface, []))

    def prepare_runtime(self, context):
        """Validate settings without wallet, storage or network access; declare the channel watcher."""
        config = logic.configuration(context.config)
        return BlockRuntimePreparation(listen_on_run=context.runtime_mode == "zeromq_active"
                                       and config["enabled"] and config["watch"])

    def fresh_inputs(self, context):
        """Only fresh deliveries count: a retained request is never executed again."""
        names = {int(port.id): str(port.name) for port in context.input_ports}
        if context.input_events:
            return [(names.get(event.input_port_id), event.value) for event in context.input_events]
        fresh = []
        for ident, name in names.items():
            attribute = context.input_attribute(name, str(ident))
            if attribute is not None and attribute.status == "updated" and attribute.value not in (None, ""):
                fresh.append((name, attribute.value))
        return fresh

    def outputs(self, context, values):
        return [BlockRuntimeOutput(port_id=int(port.id), port_name=port.name, content_type=APPLICATION_JSON,
                                   value=json.dumps(values[port.name], ensure_ascii=False, allow_nan=False))
                for port in context.output_ports if port.name in values]

    @staticmethod
    def summary(operation, values):
        result = values.get("messages") or values.get("sent") or {}
        if operation == "read_messages":
            return f"{result['count']} message(s) in {result['pages']} page(s)"
        if operation == "watch":
            return f"{result['count']} new message(s) from {len(result['channels'])} channel(s)"
        return {"send_message": "message sent", "edit_message": "message edited", "delete_message": "message deleted",
                "add_reaction": "reaction added", "upload_file": "file uploaded",
                "webhook_send": "webhook message sent"}[operation]

    def execute_runtime(self, context):
        """One request per activation; business failures go to the error port and the run continues."""
        request, secrets = {}, []
        try:
            config = logic.configuration(context.config)
            items = self.fresh_inputs(context)
            if not items:
                return BlockRuntimeResult(status="skipped", outputs=[])
            if len(items) != 1:
                logic.fail("one_operation")
            name, value = items[0]
            if name not in ("message", "file"):
                logic.fail("invalid_input", "Unknown input port.")
            if not config["enabled"]:
                logic.fail("disabled")
            request = logic.message_request(value, config) if name == "message" else logic.file_request(value, config)
            operation = request["operation"]
            extra = {}
            if operation == "upload_file":
                extra["located"] = transport.locate(request["path"], config)
            if operation == "watch":
                # The node storage is checked before the wallet is read.
                provider = context.services.get("get_block_storage_dir")
                storage.storage_directory(provider)
                extra["storage_dir"] = provider
            cancel = context.services.get("cancel_requested", lambda: False)
            if operation == "webhook_send":
                try:
                    raw = context.resolve_secret(config["webhook_secret_ref"])
                except Exception:
                    logic.fail("webhook_secret")
                hook = logic.webhook(raw, config)
                secrets = [hook[1], raw if isinstance(raw, str) else ""]
                session = transport.Session(config, hook[1], cancel, bot=False)
                extra["webhook"] = hook
            else:
                try:
                    raw = context.resolve_secret(config["secret_ref"])
                except Exception:
                    logic.fail("secret")
                token = logic.bot_token(raw)
                secrets = [token]
                session = transport.Session(config, token, cancel)
            values = operations.run(session, request, config, **extra)
            summary = self.summary(operation, values)
            requests = (values.get("messages") or values.get("sent"))["requests"]
            return BlockRuntimeResult(status="success", outputs=self.outputs(context, values),
                content_type=APPLICATION_JSON, last_message=operation + ": " + summary,
                metadata={self.kind: {"status": "success", "operation": operation, "requests": requests}},
                logs=[f"[discord] {operation}: {summary} after {requests} request(s)."])
        except logic.DiscordError as exc:
            error = exc
        except (ValueError, TypeError, KeyError, OSError, RuntimeError):
            # Raw exception text could carry message content or credentials: publish a fixed diagnostic.
            error = logic.DiscordError("internal")
        operation = request.get("operation")
        envelope = error.envelope(operation=operation, request_id=request.get("request_id"),
                                  channel_id=request.get("channel_id"))
        text = context.redact_secrets(logic.redact(json.dumps(envelope, ensure_ascii=False, allow_nan=False), secrets))
        return BlockRuntimeResult(status="success",
            outputs=[BlockRuntimeOutput(port_id=int(port.id), port_name=port.name, value=text,
                                        content_type=APPLICATION_JSON)
                     for port in context.output_ports if port.name == "error"],
            content_type=APPLICATION_JSON, last_message=error.code,
            metadata={self.kind: {"status": "error", "code": error.code, "uncertain": error.uncertain}},
            logs=[f"[discord-error] {operation or 'input'}: {error.code}"])

    def listen_runtime(self, context):
        """Watch the channels until framework Stop; listener state is local to this call."""
        config = logic.configuration(context.config)
        services = context.services

        def resolve():
            resolver = services.get("resolve_secret")
            if not callable(resolver):
                logic.fail("secret")
            return resolver(config["secret_ref"])

        def emit(kind, value, stats):
            if context.stop_requested():
                raise RuntimeError("Listener stopped.")
            values, logs = {}, []
            if kind == "messages":
                values = {"messages": value}
                logs = [f"[discord] watch: {value['count']} new message(s) after {value['requests']} request(s)."]
            elif kind == "error":
                redactor = services.get("redact_secrets")
                envelope = {key: str(redactor(item)) if isinstance(item, str) and callable(redactor) else item
                            for key, item in value.items()}
                values = {"error": envelope}
                logs = [f"[discord-error] watch: {value.get('code')}"]
            context.emit_result(BlockRuntimeResult(status="success", outputs=self.outputs(context, values),
                                                   content_type=APPLICATION_JSON,
                                                   metadata={self.kind: {"watch": stats}}, logs=logs))

        listener.ChannelWatcher(config, resolve_secret=resolve, storage_dir=services.get("get_block_storage_dir"),
                                emit=emit, stop=context.stop_requested).run()

    def handle_ui_action(self, *, node, action, values, payload=None):
        """Apply validates settings only; it never contacts Discord or reads the wallet."""
        if action in {"save_settings", "modal_update_fields", "inspector_update_fields"}:
            patch = (values or {}).get("node_patch") or {}
            if "config" in patch:
                try:
                    logic.configuration({**self.default_config(), **(node.get("config") or {}), **patch["config"]})
                except (ValueError, TypeError) as exc:
                    detail = str(exc) if isinstance(exc, logic.DiscordError) else logic.MESSAGES["config"]
                    return {"error": self.translate("block.discord.error", {"detail": detail},
                                                    fallback="Discord settings rejected: " + detail)}
        result = super().handle_ui_action(node=node, action="modal_update_fields" if action == "save_settings" else action,
                                          values=values, payload=payload)
        return ui.replace_config(result, node)

    def render_modal(self, *, node, payload=None):
        return ui.modal(self, node, payload)

    def render_inspector_panel(self, *, node, payload=None):
        return ui.inspector(self, node, payload)

    def render_node_card(self, *, node, payload=None):
        """Show the access mode and allowlist size, never the wallet references or channel IDs."""
        config = {**self.default_config(), **(node.get("config") or {})}
        channels = config.get("allowed_channels")
        count = len(channels) if isinstance(channels, list) else 0
        params = {"count": count}
        # The key and its parameters travel with the text so the editor re-renders it in the
        # interface language, even when a package process rendered it with the English pivot.
        plural = "" if count == 1 else "s"
        if config.get("enabled") is not True:
            key, params, fallback = "block.discord.card_disabled", {}, "Remote access off"
        elif config.get("watch") is True:
            key, fallback = "block.discord.card_watch", f"Watching · {count} channel{plural}"
        elif not config.get("secret_ref") and config.get("webhook_secret_ref"):
            key, params, fallback = "block.discord.card_webhook", {}, "Webhook notifications"
        elif config.get("allow_writes") is True:
            key, fallback = "block.discord.card_write", f"Send and read · {count} channel{plural}"
        else:
            key, fallback = "block.discord.card_read", f"Read only · {count} channel{plural}"
        return ui.card(self, node, key, params, self.translate(key, params, fallback=fallback))
