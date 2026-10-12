## What and why

<!-- One short paragraph. Link the issue if there is one. -->

## Checklist

- [ ] The PR title is a valid Conventional Commit (it becomes the squash commit message), for example `fix(storage): …`
- [ ] **No secrets**: no `.env`, token, key or credential file, and none in the diff, fixtures, logs or screenshots
- [ ] `bash scripts/git-safety-check.sh` passes
- [ ] `uv run ruff check`, `uv run ruff format --check`, `uv run mypy` and `uv run pytest -W error --cov` pass locally
- [ ] Tests were added or updated (written first where practical); no test uses the real network or Telegram
- [ ] Docs and the `.env.example` / configuration table are updated if behaviour or settings changed
- [ ] If the database schema changed: a new numbered migration and a migration test exist (existing migrations untouched)

## Notes for the reviewer

<!-- Risks, trade-offs, anything you did not verify. -->
