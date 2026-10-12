# Telegram setup

You need one bot and two channels. Nothing here requires a server: it is all done in the Telegram app.

## 1. Create the bot

1. Open [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Choose a display name and a username ending in `bot`.
3. BotFather replies with a token like `123456789:AA…`. **Treat it like a password.** Do not paste it into chats,
   issues, screenshots or commits. If it leaks: send `/revoke` to BotFather (see [security.md](security.md)).

One bot serves both channels.

## 2. Create two channels

Create *Minecraft updates* and *NeoForge updates* (names are yours).

| Kind | Channel id to use |
|---|---|
| **Public** (has a `t.me/<name>` link) | `@name`, 5-32 characters: letters, digits, underscore |
| **Private** | The numeric chat id, `-100` followed by digits, for example `-1001234567890` |

A public channel is the easiest: no numeric id is needed.

## 3. Make the bot an administrator

In **each** channel: *Manage channel → Administrators → Add administrator →* select your bot, and enable
**Post Messages**. Other rights can stay off. Without this, Telegram answers with 400/403 errors.

## 4. Find the numeric id of a private channel (only if needed)

Post any message in the channel (the bot must already be an administrator), then ask the Bot API for recent updates.
The token is part of the URL, so avoid typing it on a command line that lands in your shell history or on screen. This
bash example reads it silently and gives it to curl through a config stream; it was not run against a real bot while writing
this document:

```sh
read -rs TOKEN                      # paste the token, nothing is echoed or stored in history
curl -s --config <(printf 'url = "https://api.telegram.org/bot%s/getUpdates"\n' "$TOKEN")
unset TOKEN
```

In the JSON look for `"chat":{"id":-100…}` of your channel post and use that value. If the response is empty,
post again in the channel and retry. Do not share the output without checking it first.

## 5. Configure and test

Put the values in `.env` (see the [README](../README.md#configuration)):

```dotenv
TELEGRAM_BOT_TOKEN=REPLACE_WITH_YOUR_BOT_TOKEN
MINECRAFT_CHANNEL_ID=@YourMinecraftChannel
NEOFORGE_CHANNEL_ID=@YourNeoForgeChannel
```

Then:

```sh
docker compose run --rm release-monitor test-telegram --no-send   # token check only
docker compose run --rm release-monitor test-telegram             # also posts a labelled test message to each channel
```

Expected output:

```text
Bot token accepted (bot: @your_bot_username).
Test message delivered to the Minecraft channel (@YourMinecraftChannel).
Test message delivered to the NeoForge channel (@YourNeoForgeChannel).
```

The test messages change no state. The first regular poll afterwards records existing versions silently, so channels
stay quiet until a new release appears.

## Common errors

| Message from the tool | Meaning | Fix |
|---|---|---|
| `Telegram check failed: … 401 …` | Token rejected | Re-copy the token, or `/revoke` for a new one |
| `… 400 … chat not found` | Wrong id, typo, or the bot is not in the channel | Check `@name` / `-100…`; add the bot as administrator |
| `… 403 …` | Bot kicked or lacks **Post Messages** | Fix the administrator rights |
| `… 429 …` | Rate limit | Wait; the monitor does this automatically with Telegram's `retry_after` |
| `Invalid configuration: MINECRAFT_CHANNEL_ID must be '@name' …` | Wrong id format | Use `@name` (5-32 chars) or a numeric id |

`@` ids are case-insensitive for Telegram, but the form must be `@` followed by 5-32 letters, digits or underscores.
