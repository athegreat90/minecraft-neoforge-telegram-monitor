# Deployment

Two ways to run the same service. Both use a named volume `releases-data` mounted at `/data`.

| | Development / build yourself | Production |
|---|---|---|
| File | `compose.yaml` | `compose.prod.yaml` |
| Image | built from the `Dockerfile` (`minecraft-neoforge-telegram-monitor:local`) | pulled from GHCR (`ghcr.io/athegreat90/minecraft-neoforge-telegram-monitor:${IMAGE_TAG:-1}`) |
| Needs the source tree | yes | no, only the compose file and `.env` |

> **Availability.** The GHCR image is published by the release workflow. At the time of writing no release
> exists and the workflows have not run on GitHub, so `compose.prod.yaml` cannot pull anything yet. Build
> locally with `compose.yaml` until the first release is out and its package is public.

Both files share the same hardening: non-root user (10001), read-only root file system, all capabilities
dropped, `no-new-privileges`, a 16 MB `noexec` tmpfs for `/tmp`, `mem_limit: 128m`, `pids_limit: 64`,
`init: true`, `stop_grace_period: 30s`, JSON log rotation (10 MB × 3 files) and `restart: unless-stopped`.

## Prerequisites

- Docker Engine with the Compose v2 plugin.
- A `.env` file next to the compose file, created from the template and protected (`chmod 600 .env` on Linux,
  macOS, Raspberry Pi and NAS). The real `.env` must never be committed or copied into an image.
- Outbound HTTPS to the hosts listed in the [README](../README.md#requirements).

## Development / build yourself

```sh
cp .env.example .env && chmod 600 .env       # edit the three required values
docker compose run --rm release-monitor test-telegram
docker compose up -d --build
docker compose logs -f release-monitor
```

`docker compose` reads `.env` from the project directory automatically. Be careful with commands that print
the resolved configuration (`docker compose config` without `-q`): they show the token.

## Production from GHCR

```sh
mkdir -p ~/release-monitor && cd ~/release-monitor
# copy compose.prod.yaml and create .env here (chmod 600 .env)
docker compose -f compose.prod.yaml run --rm release-monitor test-telegram
docker compose -f compose.prod.yaml up -d
```

- `IMAGE_TAG` (in `.env` or the shell) picks the tag: `1` follows the latest 1.x, `1.2.3` is exact,
  `sha-<commit>` is one commit, `latest` is the newest stable release, `edge` follows `main`.
  For servers you care about, **pin an exact version**.
- The package must be public for anonymous pulls. Making it public is a one-time manual step in the
  GitHub package settings (it cannot be done through the REST API).
- Verify provenance of a downloaded image: `gh attestation verify oci://ghcr.io/athegreat90/minecraft-neoforge-telegram-monitor:<tag> --owner athegreat90`
  (the attestation is produced by the publish workflow; this has not been exercised yet).

### Keeping the token out of `docker inspect`

Environment variables are visible in `docker inspect`. To avoid that, use a Docker secret. In
`compose.prod.yaml`: remove the `TELEGRAM_BOT_TOKEN` line, add `TELEGRAM_BOT_TOKEN_FILE: /run/secrets/telegram_bot_token`
under `environment`, uncomment `secrets: [telegram_bot_token]` and the top-level `secrets:` block, and store the
token in `./secrets/telegram_bot_token.txt` (git-ignored, `chmod 600`).

## Raspberry Pi 5

- Use a 64-bit OS (Raspberry Pi OS 64-bit or Ubuntu Server) and install Docker Engine from the official repository.
- The image has an `arm64` variant, so `docker compose -f compose.prod.yaml up -d` just works once a release is published.
  Until then, `docker compose up -d --build` builds natively on the Pi. An arm64 build of the Dockerfile was run
  locally under emulation during development and `version` ran inside it; a native Pi 5 run has not been done.
- Prefer an SSD or a good SD card; the application writes only a few rows per hour.

## NAS and Linux servers

- **Named volume (default)**: nothing to configure.
- **Bind mount**: change `releases-data:/data` to `/volume1/docker/release-monitor:/data` (path is an example) and make it
  writable by the container user: `sudo chown 10001:10001 /volume1/docker/release-monitor`.
- **Never place the database on NFS or SMB.** SQLite file locking is not reliable on network file systems, and
  the instance lock may silently fail. Use local disks.
- Make sure only **one** monitor uses a given data directory; a second instance exits with code 3.

## Docker Desktop on Windows (development)

- `chmod 600 .env` does not apply; keep `.env` in a folder only your account can read.
- Run the commands in PowerShell or Git Bash. In Git Bash, prefix commands that take absolute container paths
  (for example `--tmpfs /tmp`) with `MSYS_NO_PATHCONV=1`, otherwise Git Bash rewrites the path.
- The repository uses LF line endings (`.gitattributes`); keep `core.autocrlf` off for this repository.

## Health check

The image has a built-in `HEALTHCHECK` (every 60 s, 30 s start period, 3 retries) that runs
`python -m release_monitor healthcheck`. It needs no network and prints the reason it is unhealthy:

```sh
docker compose ps
docker compose exec release-monitor python -m release_monitor healthcheck
```

Right after start it reports `unhealthy: no heartbeat yet` until the first cycle finishes.

## Logs

```sh
docker compose logs -f --tail=100 release-monitor
```

Set `LOG_LEVEL=DEBUG` for more detail and `LOG_FORMAT=json` for machine-readable output. Timestamps are UTC.
The token is redacted from log output, but read logs before posting them publicly anyway.

## Updating and rolling back

```sh
docker compose -f compose.prod.yaml pull
docker compose -f compose.prod.yaml up -d
```

Rollback: set `IMAGE_TAG` to the previous version (or a `sha-…` tag) and run `up -d` again. The database is
backwards compatible within a schema version; a database written by a *newer* schema is refused by an older
image (exit code 4), so keep a backup before upgrading across schema versions.

## Backups

```sh
docker compose exec release-monitor python -m release_monitor db-backup
```

The copy is written inside the volume next to the database; copy it out, for example with a throwaway container:

```sh
docker run --rm -v <project>_releases-data:/data -v "$PWD":/out alpine sh -c 'cp /data/*.bak /out/'
```

(`<project>` is the compose project name, by default the directory name; check `docker volume ls`.)
After restoring a backup, start the service normally. If the restored file lacks recent versions, stop the
service and run `rebaseline --source <source> --yes` for each source first (it needs the instance lock, so
the service must not be running) so nothing old is announced. `test-telegram`, `healthcheck` and `db-backup`
do not need the lock and can run next to the service.

## Resource limits

`mem_limit: 128m` and `pids_limit: 64` are set in both compose files. They were chosen as generous bounds for a
process that sleeps between polls; they have not been tuned with measurements, so adjust them if your host
reports out-of-memory kills.
