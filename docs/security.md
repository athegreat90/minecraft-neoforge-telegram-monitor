# Security

The one asset worth protecting is the **Telegram bot token**. Whoever holds it controls the bot and can post
to every channel the bot administers. The rest of this document is about keeping it, and anything like it,
out of Git, out of images and out of logs.

> **The rule:** the real `.env` file (and any file holding credentials) is never committed, pushed, uploaded,
> published or baked into an image. Only `.env.example` with placeholder values is allowed in the repository.

## Threat model in short

| Threat | Mitigation |
|---|---|
| Token committed to Git | `.gitignore`, local safety script, pre-commit, CI gitleaks over the full history, GitHub push protection |
| Token copied into a Docker image or build cache | `.dockerignore`, no build arguments for secrets, CI image scan and "no `.env` in the image" check |
| Token in logs or error messages | Redaction filter, quiet `httpx` loggers, errors that never carry the request URL, regression tests |
| Token visible in `docker inspect` | Optional `TELEGRAM_BOT_TOKEN_FILE` with a Docker secret |
| Compromised container | Non-root user, read-only root file system, no capabilities, `no-new-privileges`, memory and process limits |
| Malicious or compromised dependency or CI action | `uv.lock`, `pip-audit`, Renovate with a minimum release age, actions pinned to commit SHAs, least-privilege workflow permissions |
| Untrusted pull requests | No `pull_request_target`, no secrets in CI, publishing never runs for pull requests |
| Poisoned upstream data (Mojang, NeoForge) | Escaped HTML, size-capped responses, URLs shown only after verification, strict parsing |

## `.env` protection layers

1. **`.gitignore`** ignores `.env`, `.env.*` (except `.env.example`), `*.pem`, `*.key`, `*.p12`, `*.pfx`,
   `*.secret`, `secrets/`, `credentials/`, SQLite databases and local tooling state.
2. **`.dockerignore`** excludes the same files, plus `.git`, so a secret cannot enter the build context. The
   image only `COPY`s `pyproject.toml`, `uv.lock`, `.python-version`, `README.md`, `LICENSE` and `src/`.
3. **`scripts/git-safety-check.sh`**, run by hand and by the pre-commit hook `forbid-env-files`. It never prints
   file contents and interprets Git exit codes correctly:
   - `git ls-files --error-unmatch .env` exits **0 when `.env` is tracked: that is the failure case**; non-zero means untracked.
   - `git check-ignore -q .env` exits 0 when ignored, 1 when not ignored (failure), 128 on error (failure).
   - It also scans staged and tracked file names for forbidden names and exits non-zero with a clear message.
4. **pre-commit hooks**: `detect-private-key`, `check-added-large-files`, gitleaks on staged changes, the
   safety script, plus ruff and mypy. Install with `pre-commit install`.
5. **CI (`Secret scan`, `Image scan`)**: gitleaks over the full history, the safety script, a forbidden-file-name
   check, and a vulnerability and secret scan of the built image. These checks are designed to be required
   status checks on `main`.
6. **GitHub**: enable *secret scanning* and *push protection* in the repository settings. This is the only layer
   that stops a push from a machine where the local hooks are missing.

Layers 3 and 4 live on your machine and can be skipped (`git commit --no-verify`, hooks not installed). Layers 5 and 6
are what you can rely on.

### Day-to-day rules

- Stage files by explicit path (`git add path/to/file`), never `git add .` or `git add -A`, and read `git status --short`
  and `git diff --cached --name-only` before committing.
- Never force-add an ignored file (`git add -f`) and never bypass hooks with `--no-verify`.
- Do not print `.env` (`cat .env`, `docker compose config`) in a shared terminal, recording or CI log.
- If `.env` was ever tracked: `git rm --cached .env` stops tracking it **without deleting your local file**, but the
  token is then in history; see the next sections.
- Keep `.env` mode `600` on Linux/macOS (`chmod 600 .env`). On Windows keep it in a folder only you can read.

## Token handling

- Provide the token at **runtime** only: environment variable or `TELEGRAM_BOT_TOKEN_FILE`. Never as a build
  argument, never in the image, never in the compose file.
- Docker secret setup is documented in [deployment.md](deployment.md#keeping-the-token-out-of-docker-inspect).
- The token is part of every Bot API URL (`/bot<token>/method`). The code therefore (a) sets the `httpx` and
  `httpcore` loggers to WARNING because `httpx` logs full request URLs at INFO, (b) raises its own errors that
  never contain the URL, (c) filters anything shaped like `bot<digits>:<token>` and the exact configured token from every log
  record, message arguments and exception text, and (d) hides the token from `repr` of the configuration. Tests cover these.
- Configuration errors never echo values; `healthcheck` prints only a verdict.

## If a token leaks

Treat it as compromised immediately, in this order:

1. **Revoke it**: send `/revoke` to [@BotFather](https://t.me/BotFather) and pick the bot. This invalidates the old
   token and issues a new one.
2. Put the new token in `.env` (or the Docker secret) and recreate the container.
3. **Check the damage**: look at your channels for posts you did not make, and review the bot's administrators.
4. **Remove it from Git history** if it was pushed: rewriting history is a last resort, and it does not
   un-leak a token that was public. With [git-filter-repo](https://github.com/newren/git-filter-repo) remove the file or
   text from all commits, force-push every branch and tag, ask collaborators to re-clone, and ask GitHub support
   to purge cached views if the repository is public. Do this *after* revoking, never instead of it.
5. If it appeared in CI logs, delete the affected workflow runs and logs.

## Container hardening

| Control | Where |
|---|---|
| Non-root user and group 10001, no login shell | `Dockerfile` |
| Read-only root file system, `/tmp` as `noexec,nosuid` tmpfs | compose files |
| `cap_drop: [ALL]`, `no-new-privileges:true` | compose files |
| `mem_limit: 128m`, `pids_limit: 64`, log rotation | compose files |
| Base images pinned by version **and** digest, kept current by Renovate | `Dockerfile` |
| No compiler or package manager step in the runtime stage | multi-stage `Dockerfile` |
| Image scanned (vulnerabilities and secrets) before publishing | CI and publish workflows |

## Supply chain

- Dependencies are locked in `uv.lock` and installed with `--frozen`.
- `pip-audit` checks the locked runtime dependencies; dependency review runs on pull requests.
- Renovate waits three days before proposing a new release, which gives time for a bad or yanked release to be pulled.
- GitHub Actions are pinned to full commit SHAs, workflows start with `contents: read`, and elevated permissions
  are granted per job only. `actionlint` and `zizmor` check the workflows.
- Published images carry build provenance and an SBOM, and a GitHub build attestation.
  These parts of the pipeline have not run yet.
- Binary tools downloaded in CI (gitleaks, actionlint, hadolint) are verified against pinned SHA-256 checksums.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md). Use GitHub's private vulnerability reporting, not a public issue, and never
include a real token or `.env` content in a report.
