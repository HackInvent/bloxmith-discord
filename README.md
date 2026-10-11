# Discord

Send and read Discord channel messages with a bot or a webhook. The block talks to
the Discord REST API v10 with a bot token kept in the BloxSmith wallet: it sends
messages (text, embeds, replies), edits, deletes and reacts to them, uploads a
file, reads channel history and, in Active Runtime, watches channels for new
messages. A channel webhook URL, also kept in the wallet, sends notifications
without any bot. Remote access stays off until you enable it, nothing is sent
until you also allow writes, deleting needs a third switch, and nobody is pinged
unless the Mentions setting allows it.

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

[![Discord — send, read and watch channel messages](media/thumbnail.webp)](media/cover.png)

## When to use it

- Post a build, alert or report summary to a team channel, with an embed.
- Notify a channel through a webhook when no bot can be installed.
- Reply to a message, edit a status message as a job progresses, or react with ✅.
- Share a generated file (CSV, PDF, image) from a dedicated folder.
- Read the recent history of a channel to summarize or archive it.
- React to new messages: in Active Runtime the watch emits the messages posted in
  allowlisted channels since the last poll, every 10 seconds or more.

## Ports

| Direction | Port | Value |
| --- | --- | --- |
| Input | `message` | Plain text to send, or a JSON object naming one operation (see below) |
| Input | `file` | A path inside `file_root`, or a JSON object for `upload_file` |
| Output | `messages` | `read_messages` and `watch` results: a JSON object with the `messages` array |
| Output | `sent` | Receipt of a write: message ID, channel ID, timestamp, request ID |
| Output | `error` | Machine `code`, English `message`, `uncertain` flag and Discord details |

Each fresh input runs exactly one operation. A retained value is never sent again,
so a replayed or forced activation does not repeat a write. Both inputs are
optional, so a node that only watches needs no input wire. Business failures are
published on `error` and the run continues; they never stop an Active Runtime worker.

### `message` input

Plain text is sent with the operation chosen in **Plain text sends with**
(`send_message` through the bot by default, or `webhook_send`). A JSON object names
its operation; without `operation`, the same default applies.

| Operation | Fields | Needs |
| --- | --- | --- |
| `send_message` | `channel_id`, `content`, `embeds`, `reply_to`, `allowed_mentions`, `mention_replied_user`, `silent`, `suppress_embeds`, `request_id` | bot, `allow_writes` |
| `edit_message` | `channel_id`, `message_id`, `content`, `embeds`, `allowed_mentions`, `suppress_embeds`, `request_id` | bot, `allow_writes` |
| `delete_message` | `channel_id`, `message_id`, `request_id` | bot, `allow_writes`, `allow_delete` |
| `add_reaction` | `channel_id`, `message_id`, `emoji`, `request_id` | bot, `allow_writes` |
| `read_messages` | `channel_id`, `limit`, `before` or `after`, `request_id` | bot |
| `webhook_send` | `content`, `embeds`, `username`, `avatar_url`, `thread_id`, `allowed_mentions`, `silent`, `suppress_embeds`, `request_id` | webhook, `allow_writes` |
| `watch` | `request_id` | bot, `watch` |

- `channel_id` defaults to `default_channel` and must be listed in `allowed_channels`.
  IDs are Discord snowflakes such as `"1234567890123456789"`; copy them with
  Discord's Developer Mode. A `#name` is not accepted.
- `content`: 1–2000 characters. `send_message` and `webhook_send` need `content` or
  `embeds`. Unknown fields are refused.
- `embeds`: an array of 1–10 embeds (or one embed object) with `title` (256),
  `description` (4096), `url`, `timestamp` (ISO 8601), `color` (0–16777215),
  `footer` `{text, icon_url}`, `author` `{name, url, icon_url}`, `image` / `thumbnail`
  `{url}` and up to 25 `fields` `{name, value, inline}`; at most 6000 characters of
  embed text in total, as Discord documents. Other embed keys are refused.
- `reply_to`: the ID of a message of the same channel to reply to.
- `silent`: deliver without a push notification. `suppress_embeds`: hide link previews.
- `emoji`: a Unicode emoji such as `"👍"`, or a custom emoji as `name:id` or `<:name:id>`.
- `limit` (`read_messages`): 1 to `read_limit` (default 100). Without `before` or
  `after`, the newest messages are read; `before` reads older ones, `after` reads
  forward from a message ID.
- `username` (`webhook_send`): 1–80 characters, without "discord" or "clyde";
  `avatar_url` must be HTTPS; `thread_id` posts into a thread of the webhook's channel
  (for example a forum post).
- `request_id`: optional correlation ID (1–128 of `A-Za-z0-9._:-`), echoed in `sent`,
  `messages` and `error`. For `send_message` and `upload_file`, the same `request_id`
  sent again to the same channel within a few minutes returns the first message
  instead of a duplicate (Discord's `nonce` with `enforce_nonce`).

Examples:

```json
{"content": "Nightly build passed", "embeds": [{"title": "Build 42", "color": 3066993,
  "fields": [{"name": "Tests", "value": "1 204 passed", "inline": true}]}]}
{"operation": "send_message", "channel_id": "1234567890123456789", "content": "Done", "reply_to": "1234567890123456790"}
{"operation": "edit_message", "message_id": "1234567890123456791", "content": "Deploy 3/3 complete"}
{"operation": "add_reaction", "message_id": "1234567890123456791", "emoji": "✅"}
{"operation": "read_messages", "limit": 50}
{"operation": "webhook_send", "content": "Backup finished", "username": "Backup"}
{"operation": "watch"}
```

### Mentions

Discord pings `@everyone`, `@here`, roles and users written in a message unless the
request restricts them. The block always sends an explicit `allowed_mentions`
object, derived from the **Mentions** setting:

| `mentions` | Who can be pinged |
| --- | --- |
| `none` (default) | Nobody; `<@id>` and `@everyone` are shown as text only |
| `users` | Users mentioned in the content |
| `users_roles` | Users and roles |
| `all` | Users, roles, `@everyone` and `@here` |

A request may narrow it with Discord's own `allowed_mentions` object
(`{"parse": [...], "users": [...], "roles": [...], "replied_user": bool}`) or set
`mention_replied_user` on a reply; anything wider than the setting is refused with
`mentions_not_allowed` and nothing is sent.

### `file` input

A path relative to `file_root` (or an absolute path inside it), or a JSON object:
`{"path", "channel_id", "content", "filename", "description", "spoiler", "reply_to",
"allowed_mentions", "mention_replied_user", "silent", "request_id"}`. The file must be
a regular, non-empty file inside `file_root`, reached without symbolic links and no
larger than `max_file_bytes`. `description` is the attachment's alternative text;
`spoiler` prefixes the file name with `SPOILER_`. The upload needs a bot and
`allow_writes`.

### `messages` output

`read_messages`:

```json
{"request_id": null, "operation": "read_messages", "channel_id": "1234567890123456789", "count": 2,
 "messages": [
   {"id": "1234567890123456790", "channel_id": "1234567890123456789", "guild_id": "1234567890123456700",
    "author": {"id": "1234567890123456711", "username": "ada", "global_name": "Ada", "bot": false},
    "content": "Is the build green?", "timestamp": "2026-01-02T10:00:00.000000+00:00", "edited_timestamp": null,
    "type": 0, "webhook_id": null, "reply_to": null, "thread_id": null, "mention_everyone": false,
    "mentions": [], "mention_roles": [], "pinned": false, "attachments": [], "embeds": [], "reactions": []}
 ],
 "direction": "backward", "has_more": true, "stop_reason": "limit",
 "next_before": "1234567890123456790", "next_after": null, "pages": 1, "requests": 1}
```

Messages are always listed oldest first. `stop_reason` is `end`, `limit`,
`max_pages` or `output_limit` (16 MiB of messages). When `has_more` is true, pass
`next_before` back as `before` (or `next_after` as `after` for a forward read) to
continue exactly where the read stopped. Attachments carry `id`, `filename`, `size`,
`content_type`, `url` and `description`; embeds keep their public fields; reactions
are `{"emoji", "count", "me"}`. Author profiles are reduced to ID, user name, display
name and the bot flag.

`watch`: `{"request_id", "operation": "watch", "source", "count", "messages",
"started", "caught_up", "channels", "requests"}`, where `source` is `watch` for the
Run listener and `request` for a watch request, and `channels` lists, per channel,
`channel_id`, `count`, `started`, `caught_up`, `pages`, `skipped` (bot messages),
`cursor`, and `error` / `http_status` when that channel could not be read.

### `sent` output

`{"request_id", "operation", "channel_id", "message_id", "guild_id", "timestamp",
"requests"}` plus `reply_to` for a reply, `edited_timestamp` for an edit, `attachments`
for an upload, and `webhook_id` (and `thread_id`) for `webhook_send`.
`delete_message` returns `{"deleted": true, ...}` and `add_reaction` returns the
`emoji`. Receipts never contain the token or the webhook URL.

### `error` output

`{"code", "message", "uncertain", "operation", "request_id", "channel_id"}` plus,
when Discord provides them, `http_status`, `retry_after` (seconds), `rate_limit_scope`
(`user`, `shared` or `global`), `provider_code` (Discord's JSON error code, for example
50001 Missing Access, 50013 Missing Permissions, 10008 Unknown Message, 50035 Invalid
Form Body), `provider_message`, `provider_errors` (up to five `field: reason` lines of
an invalid form body) and `attempts`. `uncertain: true` means a write may have been
applied: read the channel before sending it again, or resend with the same
`request_id`, which Discord deduplicates for a few minutes.

| Code | Meaning |
| --- | --- |
| `config`, `disabled`, `secret`, `webhook_secret`, `invalid_input`, `request_too_large`, `one_operation` | Refused locally; nothing sent |
| `writes_disabled`, `delete_disabled`, `watch_disabled`, `bot_not_configured`, `webhook_not_configured` | The operation is not enabled or has no credential |
| `channel_required`, `channel_not_allowed`, `mentions_not_allowed` | Refused by the allowlist or the Mentions setting |
| `file_root_required`, `file_not_found`, `file_not_allowed`, `file_too_large`, `file_empty`, `file_changed` | Upload refused before (or abandoned during) sending |
| `bad_request` (400), `unauthorized` (401), `forbidden` (403), `not_found` (404), `provider_too_large` (413), `rate_limited` (429), `provider_error` (other) | Discord refused the request |
| `provider_unavailable` (5xx), `network`, `timeout`, `cancelled`, `protocol` | Transport outcome; `uncertain` tells whether a write may have been applied |
| `output_too_large` | One page alone exceeds the 16 MiB output limit; lower `page_size` |
| `storage`, `busy`, `state_invalid` | The watch cursor could not be used: no project storage, another watcher holds it, or the stored state is unreadable |
| `tls` | Certificate verification failed; nothing sent |

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Remote access switch. While off, nothing reaches Discord and the wallet is not read |
| `secret_ref` | empty | Wallet reference of the bot token, such as `secret://workspace/discord_bot` |
| `webhook_secret_ref` | empty | Wallet reference of a webhook URL, such as `secret://workspace/discord_webhook` |
| `allowed_channels` | `[]` | Channel IDs the bot may use (up to 100); required with a bot token |
| `default_channel` | empty | Channel used when a request gives none; must be allowlisted |
| `default_operation` | `send_message` | Operation for plain text: `send_message` (bot) or `webhook_send` |
| `mentions` | `none` | `none`, `users`, `users_roles` or `all` (see Mentions) |
| `allow_writes` | `false` | Required to send, edit, react, upload and use the webhook |
| `allow_delete` | `false` | Additional switch for `delete_message`; needs `allow_writes` |
| `file_root` | empty | Folder the `file` input may upload from |
| `max_file_bytes` | 10485760 | Upload size bound, 1 KiB–100 MiB |
| `read_limit` | 100 | Messages per `read_messages`, 1–1000 |
| `watch` | `false` | Run the channel watch in Active Runtime |
| `watch_channels` | `[]` | Channels to watch, a subset of `allowed_channels`; empty watches all of them (at most 20) |
| `watch_interval_sec` | 30 | Seconds between two polls of the Run listener, 10–86400 |
| `emit_existing` | `false` | On the first start, emit the messages of the recent window instead of only recording the position |
| `existing_since_hours` | 1 | Recent window for `emit_existing`, 1–168 hours |
| `watch_max_items` | 100 | Messages emitted per poll, 1–1000; more wait for the next poll |
| `watch_include_bots` | `false` | Also emit messages written by bots and webhooks (including this block's own) |
| `page_size` | 100 | `limit` per read request, 1–100 |
| `max_pages` | 5 | Pages per read, and per channel in a watch poll, 1–20 |
| `timeout_sec` | 30 | Deadline for one request or poll, pages, rate-limit waits and retries included, 5–300 |
| `max_retries` | 2 | Retries, 0–5 (see Limits) |
| `max_retry_wait_sec` | 30 | Longest rate-limit or retry wait, 1–120 |
| `max_response_bytes` | 4194304 | Bound for each Discord response, 64 KiB–32 MiB |
| `api_base_url` | `https://discord.com` | API origin; plain HTTP only for a loopback test host |
| `ca_file` | empty | Optional absolute PEM file for a privately signed HTTPS origin |

The settings are validated on Apply and again before a Run starts: an enabled node
needs a bot token or a webhook reference, a bot token needs at least one allowlisted
channel, `default_operation` needs the matching credential, and `allow_delete` needs
`allow_writes`. Restart a loaded Run to use changed settings.

## Authentication

**Bot.** In the [Discord Developer Portal](https://discord.com/developers/applications),
create an application, add a bot, copy its token and store it in an unlocked wallet
secret (the bare token text, or `{"token": "YOUR_TOKEN"}`; an optional `Bot ` prefix
is removed). Invite the bot to the server with the permissions the node needs:
View Channel, Send Messages, Read Message History, Add Reactions, Attach Files,
Embed Links, and Manage Messages only to delete other people's messages. To read the
text of messages it did not write or that do not mention it, enable the privileged
**Message Content Intent** on the bot page; without it Discord returns those messages
with an empty `content`, empty `embeds` and no `attachments`. Bots in 100 or more
servers need Discord's approval for this intent.

**Webhook.** In the channel settings › Integrations › Webhooks, create a webhook,
copy its URL and store it in the wallet (bare URL text, or `{"url": "..."}`). The URL
contains the webhook token: treat it as a secret. Only the webhook ID and token are
taken from it; requests always go to `api_base_url`, so a wallet value cannot send
them elsewhere. A webhook can only send to its own channel (and its threads); it
cannot read, edit, react or upload in this version.

## Example

1. Create a bot, invite it to the server, store its token in the wallet as
   `discord_bot`, and copy the ID of a channel.
2. Set `enabled = true`, `secret_ref = secret://workspace/discord_bot`,
   `allowed_channels = ["<channel ID>"]`, `default_channel = <channel ID>` and
   `allow_writes = true`.
3. Wire a Text block with `Nightly build passed` to `message` and a Display to `sent`:
   One Shot posts the message and shows its ID.
4. To react to questions, enable `watch`, enable the Message Content intent, wire
   `messages` to the next step and start an Active Runtime Run. The first start
   records the newest message; each later message arrives once.

## How the operations work

All requests use `https://discord.com/api/v10` with `Authorization: Bot <token>`
(webhooks carry their token in the path instead) and the
`DiscordBot (https://github.com/HackInvent/bloxmith-discord, 0.1.0)` user agent.

- `send_message`: `POST /channels/{channel.id}/messages` with `content`, `embeds`,
  `message_reference`, `allowed_mentions`, `flags` (4 suppress embeds, 4096 silent),
  `nonce` and `enforce_nonce: true`.
- `edit_message`: `PATCH /channels/{channel.id}/messages/{message.id}`; Discord only
  lets a bot edit its own messages.
- `delete_message`: `DELETE /channels/{channel.id}/messages/{message.id}` (HTTP 204).
- `add_reaction`: `PUT /channels/{channel.id}/messages/{message.id}/reactions/{emoji}/@me` (HTTP 204).
- `read_messages`: `GET /channels/{channel.id}/messages?limit=&before=` or `after=`, page
  after page; the last page asks only for the messages still needed.
- `upload_file`: `POST /channels/{channel.id}/messages` as `multipart/form-data` with
  `payload_json` (including `attachments: [{"id": 0, "filename", "description"}]`) and
  `files[0]`, streamed from `file_root` without loading the file in memory.
- `webhook_send`: `POST /webhooks/{webhook.id}/{webhook.token}?wait=true[&thread_id=]`,
  so Discord returns the created message.
- `watch`: `GET /channels/{channel.id}/messages?after=<cursor>&limit=` per channel.

Every request is validated and sized before the first byte is sent.

## Watch

- **Active Runtime only** for continuous watching: with `watch` on, the Run starts a
  listener that polls the watched channels every `watch_interval_sec` (at least 10 s)
  until Stop. Stop ends a pending poll at once.
- **One Shot** never starts the listener: a `{"operation": "watch"}` request performs
  a single poll and returns. Successive Runs of the same project continue from the
  stored cursors.
- **Cursors:** one per channel, the ID of the last message handed over. Discord IDs
  grow with time, so `after=<cursor>` returns exactly the new messages. Cursors are
  written atomically to the node storage before the messages are handed over: a
  restart never replays them. If the runtime refuses the hand-off, the previous
  cursors are restored and the messages come again on the next Run.
- **First start:** without `emit_existing`, the cursor is the newest message of the
  channel, read from Discord (so the local clock does not matter), and nothing is
  emitted. With `emit_existing`, messages of the last `existing_since_hours` are
  emitted once (that window start is computed from the local clock).
- **Bounds:** each poll reads at most `max_pages` pages per channel and emits at most
  `watch_max_items` messages across channels; the next poll continues from the cursor,
  so nothing is skipped.
- **Own messages:** bot and webhook messages (including the block's own replies) are
  skipped unless `watch_include_bots` is on, so a reply cannot trigger itself.
- **Channel errors:** a channel answering 403 or 404 keeps its cursor and is reported
  once on `error`; the other channels are still polled in the same cycle. While any
  watched channel keeps failing, the next poll of **all** watched channels waits
  60 s (or `watch_interval_sec` if longer) instead of `watch_interval_sec`, to stay
  clear of Discord's invalid-request limit; remove or fix the failing channel to get
  the normal interval back. A rejected token (401) has the same effect.
- While the Run listener holds the cursors, a `watch` request returns `busy`. The
  stored state holds no token, webhook, wallet reference or message content: only
  channel IDs and message IDs.
- Only new messages are watched: edits, deletions and reactions are not. There is no
  webhook endpoint and no inbound listener; the watch only makes outbound requests.
  The Discord Gateway (WebSocket) is out of scope for this version.

## Limits and warnings

- **Rate limits.** Discord limits each route per channel (or webhook) and reports
  its buckets in `X-RateLimit-*` headers. The block remembers an exhausted bucket
  (`X-RateLimit-Remaining: 0`, `X-RateLimit-Reset-After`) and waits for its reset
  before the next request on that route, across nodes of this process using the same
  credential; routes sharing an `X-RateLimit-Bucket` share the wait. A 429 response is
  retried after its `retry_after` (global or per route) within `max_retries`,
  `max_retry_wait_sec` and the deadline; a longer wait is reported as `rate_limited`
  with `retry_after`, nothing sent. Separate Active Runtime workers do not share
  their bucket tables; Discord then answers 429, which is honoured.
- Reads are retried after HTTP 429, 5xx or a lost connection. Writes are retried only
  after HTTP 429 (Discord did not process them); a write that fails after it was sent
  is reported with `uncertain: true` and never replayed.
- Discord temporarily blocks clients that send too many invalid requests (401, 403,
  429): fix credential and permission errors rather than retrying them in a loop.
- **Deletion is permanent.** Discord has no recycle bin for messages; that is why
  `delete_message` needs its own switch.
- Uploads are limited by the server's boost level (10 MiB without boosts); a larger
  file is refused by Discord with `provider_too_large`.
- Message content read from Discord is untrusted data. Do not feed it to tools that
  execute commands.
- No HTTP proxy support; the host needs direct outbound HTTPS to `discord.com`.

## Dependencies

Python standard library only (`http.client`, `ssl`, `json`, `fcntl`); no
`requirements.txt`. Linux/POSIX is required for the watch state lock.

## Security

- The wallet values are read at run time, only after settings, the request, the file
  and the watch storage are validated. The bot token and the webhook URL never appear
  in settings, outputs, logs, run metadata, the watch state or error messages;
  provider messages are bounded and credentials are removed. Rate-limit buckets
  identify a credential by a digest kept in memory only.
- TLS certificates and host names are always verified; there is no insecure mode.
- Redirects, cookies, proxies and compressed responses are not accepted.
- An `api_base_url` other than the official origin receives the token: change it
  only for a local test double. Webhook URLs must point to `discord.com`,
  `discordapp.com`, `canary.discord.com` or `ptb.discord.com`.
- The block opens no listening port; it only makes outbound requests.

## Discord API usage

Discord REST API **v10** on `https://discord.com/api/v10`:

- `POST /channels/{channel.id}/messages` (JSON and multipart)
- `PATCH /channels/{channel.id}/messages/{message.id}`
- `DELETE /channels/{channel.id}/messages/{message.id}`
- `GET /channels/{channel.id}/messages` (`limit`, `before`, `after`)
- `PUT /channels/{channel.id}/messages/{message.id}/reactions/{emoji}/@me`
- `POST /webhooks/{webhook.id}/{webhook.token}` (`wait`, `thread_id`)

Errors are read from `{"code", "message", "errors"}`; rate limits from the
`X-RateLimit-Remaining`, `X-RateLimit-Reset-After`, `X-RateLimit-Bucket`,
`X-RateLimit-Global` and `X-RateLimit-Scope` headers and the 429 body
`{"retry_after", "global"}`.

**Real-provider qualification is pending.** All tests use a loopback double that
reproduces these documented request and response shapes; no Discord account, bot,
server, token or webhook was contacted. In particular, the order of a page read with
`after` (the block sorts by ID and does not rely on it), the exact bucket headers,
nonce deduplication, the effect of a missing Message Content intent and multipart
upload acceptance follow the documentation and have not been checked against the
live service.

## Framing decisions

- One block, two credentials: a bot token for every operation, or a webhook URL for
  `webhook_send` only. Both stay in the wallet; the block keeps references.
- Every write (send, edit, react, upload, webhook) needs `allow_writes`; delete also
  needs `allow_delete`, because Discord deletions are permanent.
- Mentions are off by default and widened only by the Mentions setting, never by the
  message text; requests may narrow, not widen.
- Bot operations are confined to `allowed_channels`; a webhook is bound to its own
  channel by Discord, so the allowlist does not apply to it.
- Plain text is a message to send; every other operation is an explicit JSON request.
- `send_message` and `upload_file` always send a `nonce` with `enforce_nonce`, derived
  from `request_id` when given, so a deliberate resend can be made idempotent.
- The watch polls REST `after` a persisted message ID per channel, at least every
  10 seconds, and skips bot messages by default. The Gateway WebSocket (real-time
  events, presence, edits) is out of scope for this version.
- Uploads send one file per activation; webhook uploads, threads creation, pins,
  slash commands and interactions are not covered.
- The API origin is fixed but overridable for tests; plain HTTP is accepted only for
  loopback hosts.

## Tests

`tests/F5.01_discord.py` checks settings, credentials (bot token, webhook URL
parsing and host rules), request contracts, embeds and mention limits, error mapping
and form-error flattening, rate-limit buckets, message normalization, the watch state
document and block hooks without a socket. `tests/F5.02_discord_runtime.py` runs the
real client against the loopback double (`tests/discord_fixture.py`, HTTP and HTTPS):
send, mentions, replies, embeds, nonce deduplication, edit, reactions and delete;
history pages with exact `before`/`after` continuation; multipart uploads from
`file_root` and their refusals; webhook execution with `wait` and `thread_id`;
bucket waits, 429 per route and global, 5xx, lost connections, oversized and
compressed bodies, cancellation, the deadline and TLS; the watch (no flood, each
message once, bot filter, bursts, recent window, channel errors, lock), the Active
Runtime listener (restart without replay, refused hand-off, errors, Stop); then real
graphs in One Shot and Active Runtime for bundled, managed and linked packages.
`tests/F8.01_discord_ui.py` covers the English and French forms at 1440, 800, 390 and
320 px wide, in managed and linked installations. They run in the private BloxSmith
workspace against a pinned framework; the initial development version does not imply
a release.

## License

Block code: Apache-2.0. Discord is an external service governed by its own terms.
No Discord SDK, logo, account data or credential is bundled.
