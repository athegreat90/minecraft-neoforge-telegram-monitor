# Contributing

Thanks for helping. This is a small project with a few strict rules, mostly about not leaking secrets.

## The one hard rule

**Never commit, paste or share your real `.env`, your Telegram bot token, or any file that contains credentials.**
Only `.env.example` (placeholders) belongs in Git. Stage files by explicit path, never with `git add .`, and read
`git status --short` before every commit. Details: [docs/security.md](docs/security.md).

## Development setup

You need Python 3.14 and [uv](https://docs.astral.sh/uv/) (`pip install uv`, or see the uv documentation).
On a machine where `uv` is not on `PATH`, use `python -m uv …` instead.

```sh
git clone https://github.com/athegreat90/minecraft-neoforge-telegram-monitor.git
cd minecraft-neoforge-telegram-monitor
uv sync                      # creates .venv with the locked runtime and dev dependencies
uv run pre-commit install    # optional but recommended: local secret and style checks on commit
```

`uv run …` uses the interpreter pinned in `.python-version`. The supported floor is the `requires-python` value in
`pyproject.toml`.

## Checks to run before opening a pull request

```sh
uv run ruff check
uv run ruff format --check
uv run mypy                                   # strict
uv run pytest -W error --cov --cov-fail-under=85
bash scripts/git-safety-check.sh              # no secret-bearing files tracked or staged
docker build -t release-monitor:dev .         # when you touch the Dockerfile, dependencies or src/
```

The same checks run in CI (`Lint`, `Type check`, `Tests`, `Docker build`, `Secret scan`, …) and are required before
merging. Tests must not use the real network or the real Telegram API: use `httpx.MockTransport` and the fixtures in
`tests/fixtures/`. Live tests, if you ever need one, must be opt-in with the `live` marker and never run in CI.

## Tests first

When a runnable, deterministic test is possible, write it first and watch it fail for the right reason, then make it
pass, then refactor. Bug fixes need a regression test. Fixtures should be trimmed copies of real upstream responses
(remove personal data such as e-mail addresses).

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `perf:`, `refactor:`, `docs:`, `test:`,
  `ci:`, `build:`, `chore:`, with an optional scope (`feat(storage): …`). Breaking changes use `!` or a `BREAKING CHANGE:` footer.
- Pull requests are squash-merged and the **pull request title becomes the commit message**, so the title must be a
  valid conventional commit. The `PR title` check enforces it.
- release-please derives the version and `CHANGELOG.md` from these messages (semantic versioning). Do not edit
  `CHANGELOG.md` by hand.
- Keep a pull request to one reviewable change (roughly 400 changed lines is a good ceiling); put tests and docs in the same
  pull request as the behaviour they cover.
- Do not add AI attribution trailers or `Co-Authored-By` lines to commits.

## Project layout

```text
src/release_monitor/   application code (see docs/architecture.md)
tests/                 pytest suite; tests/fixtures/ holds real, trimmed upstream responses
docs/                  guides
scripts/               git-safety-check.sh
.github/               workflows, Renovate config, templates
```

## Adding a new release source

1. Add the value to `Source` in `models.py`. **The database has `CHECK` constraints built from that enum**, so you also
   need a new, numbered migration in `storage/migrations.py` (never edit an existing migration) and a migration test.
2. Create `sources/<name>.py` following `minecraft.py` / `neoforge.py`: a pure parser, a `fetch_all()` that returns **every**
   release (the baseline must record all of them, independent of filters), `is_eligible(release, config)` for the filters, and a
   `fetch_details()` that returns `ReleaseDetails` with `ready=False` when official notes are missing. Never invent text.
3. Add configuration (channel id and filters) in `config.py`, with validation and tests.
4. Add the formatter in `notifier/formatting.py` (escape everything, no slicing rendered HTML) and wire the source into
   `app.py`.
5. Add fixtures from real responses, tests for parsing, ordering, filters, failure modes and the full poll cycle, then update the
   README and `docs/`.

## Reporting problems

Use the issue templates. For security problems use GitHub's private vulnerability reporting ([SECURITY.md](SECURITY.md)).
**Never include a token, `.env` content, or unredacted logs that might contain one.**
