# Security policy

## Supported versions

Security fixes are made for the **latest released minor version** of the 1.x series and for `main`. Older releases
are not patched: upgrade to the latest release (see the [deployment guide](docs/deployment.md#updating-and-rolling-back)).
No release has been published yet.

## Reporting a vulnerability

Please report privately. **Do not open a public issue** for a vulnerability.

1. Open the repository's **Security** tab and choose **Report a vulnerability** (GitHub private vulnerability reporting), or
   use <https://github.com/athegreat90/minecraft-neoforge-telegram-monitor/security/advisories/new>.
2. Describe the problem, the affected version or commit, and how to reproduce it.
3. **Never include a real bot token, a `.env` file or unredacted logs.** Use placeholders. If you accidentally exposed a
   token, revoke it first (send `/revoke` to [@BotFather](https://t.me/BotFather)).

This is a volunteer-maintained project; reports are handled on a best-effort basis. You will get an acknowledgement and,
for confirmed issues, a fix and a coordinated disclosure through a GitHub security advisory.

## If you find a leaked credential

If you notice a token or other secret in this repository, its history, an issue, a pull request or a published image, report it
as above. The owner must revoke the credential first, and only then clean the history; see
[docs/security.md](docs/security.md#if-a-token-leaks).

## Scope notes

In scope: the application code, the container image, the workflows and the documentation. Out of scope: vulnerabilities in
Telegram, Mojang, NeoForge, Docker or GitHub themselves (report those upstream).
