# Minecraft + NeoForge Telegram release monitor

A small Docker service that polls official version sources and posts to **two separate Telegram channels**.

## What it detects

- **Minecraft Java Edition**: stable versions (optional: snapshots, pre-releases, release candidates) from the official Mojang `version_manifest_v2.json`. Uses Mojang's `releaseTime` and best-effort official Minecraft.net release notes. The post includes an official link when the page is available.
- **NeoForge**: every build with major version **26 or newer** (including beta unless disabled) from official Maven metadata. Extracts the **matching build's line** from its official, cumulative changelog. Dates are labeled **artifact modified** when Maven supplies a Last-Modified header; that is not a guaranteed official release-announcement timestamp.
- SQLite state persists across restarts to suppress duplicate notifications.

## Setup (Linux or Docker Desktop)

1. In Telegram, create two channels: e.g. `@YourMinecraftUpdatesChannel` and `@YourNeoForgeUpdatesChannel` (or use private channels).
2. Message **@BotFather**, issue `/newbot`, and copy the bot API token.
3. Open each channel's administrators settings, add the bot and allow **Post Messages**.
4. Copy `.env.example` to `.env`, then edit all three required values.
5. Run:

   ```sh
   chmod 600 .env
   docker compose build
   docker compose run --rm release-monitor python /app/monitor.py --test
   docker compose up -d
   docker compose logs -f release-monitor
   ```

### Why nothing posts initially?

The first regular run **baselines all existing versions**. New notifications start only when an additional release appears. The `--test` command sends an immediate proof-of-configuration post to each channel.

## Config

| Environment variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | Token from BotFather; keep secret |
| `MINECRAFT_CHANNEL_ID` | required | `@public_channel` or private `-100...` ID |
| `NEOFORGE_CHANNEL_ID` | required | `@public_channel` or private `-100...` ID |
| `MINECRAFT_SNAPSHOTS` | `false` | Include Mojang snapshot-type entries, including pre-releases |
| `NEOFORGE_BETAS` | `true` | Announce beta/pre-release NeoForge versions |
| `NEOFORGE_MIN_MAJOR` | `26` | Include NeoForge major >= this value |
| `POLL_SECONDS` | `300` | Checks every 5 minutes (minimum 60 seconds) |

To change configuration, edit `.env` then run `docker compose up -d --force-recreate`.

## How to check

- `docker compose run --rm release-monitor python /app/monitor.py --test`: sends two test posts.
- `docker compose logs --tail=100 release-monitor`: see startup and new notifications.
- `docker compose down`: stops service; Docker named volume persists.

## Sources

- Mojang manifest: https://piston-meta.mojang.com/mc/game/version_manifest_v2.json
- Minecraft release notes: https://www.minecraft.net/en-us/articles
- NeoForge metadata: https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml
- NeoForge changelog format example: https://maven.neoforged.net/releases/net/neoforged/neoforge/26.2.0.87/neoforge-26.2.0.87-changelog.txt

## Important limitations

- Notification delay is up to the polling interval plus publishing delay at source.
- Minecraft.net notes URL matching is best-effort: some snapshot articles use different titles/URLs. The monitor still posts the official version and date without inventing a changelog.
- Changelogs can be long: Telegram gets a short summary plus the official link, not a full text dump.
- NeoForge's Maven metadata has only a repository-level `lastUpdated`, not a reliably version-specific `releaseTime`; `Last-Modified` from a build's changelog file is described as such, never misrepresented as a guaranteed release date.
- This monitors **Minecraft Java Edition** only, not Bedrock.
- The site fetchers have not been network-tested in this workspace; test on your internet-connected Docker host.

## Self-test (does not call Mojang/NeoForge/Telegram)

```sh
python -m unittest -v test_monitor.py
```

## Note on credentials

Never publish your `.env` or Telegram bot token to a public Git repository.
