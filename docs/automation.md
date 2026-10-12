# Automation: CI, publishing, releases and updates

> **Status.** All workflows and the Renovate configuration are written and were linted locally (`actionlint`,
> `zizmor`, `hadolint`, `renovate-config-validator`). **None of them has run on GitHub yet.** Treat the first runs as the
> real test, and expect small fixes. Nothing below should be read as "CI is green".

## Overview

| File | Trigger | Purpose |
|---|---|---|
| `.github/workflows/ci.yml` | pull requests, manual, called by other workflows | Lint, types, tests, Docker build and smoke tests, compose validation, secret scan, audits, image scan, workflow lint, PR title |
| `.github/workflows/docker-publish.yml` | push to `main`, tags `v*.*.*`, manual, called by `release.yml` | Runs CI first, then builds, scans and publishes the multi-arch image to GHCR |
| `.github/workflows/release.yml` | push to `main` | release-please: maintains the release pull request, tags the release, calls the publish workflow |
| `.github/workflows/security.yml` | weekly (Mondays 05:17 UTC), manual | Full-history gitleaks, `pip-audit`, `zizmor`, Trivy scan of the latest published image |
| `.github/renovate.json5` | Renovate app | The only dependency updater |

CI itself has no `push` trigger on purpose: pushes to `main` go through `docker-publish.yml`, which calls CI first, so
the checks run once instead of twice.

All workflows start with `permissions: contents: read`; `packages: write`, `attestations: write`, `id-token: write`,
`pull-requests: write` and `contents: write` are granted only on the individual jobs that need them. No workflow uses
`pull_request_target`, none needs a secret other than the job-scoped `GITHUB_TOKEN`, and nothing is published from a pull
request. Every third-party action is pinned to a commit SHA.

## Required status checks

Make these required on `main` (names are the job names in `ci.yml`):

`Lint`, `Type check`, `Tests (floor)`, `Tests (current)`, `Docker build`, `Compose validate`, `Secret scan`,
`Dependency audit`, `Image scan`, `Workflow lint`, `PR title`.

`Tests (floor)` runs the oldest supported Python (`requires-python`, currently 3.14); `Tests (current)` runs the version in
`.python-version`. Both enforce `-W error` and an 85 % coverage gate.

## One-time repository setup

The commands below are **documentation only: they were not executed**. They assume `gh` is authenticated as the repository
owner. Check each against the current GitHub REST documentation before running it.

```sh
REPO=athegreat90/minecraft-neoforge-telegram-monitor

# Merge policy: squash only, PR title becomes the commit message (feeds release-please)
gh api -X PATCH repos/$REPO \
  -F allow_squash_merge=true -F allow_merge_commit=false -F allow_rebase_merge=false \
  -F allow_auto_merge=true -F delete_branch_on_merge=true \
  -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY

# Secret scanning and push protection (free on public repositories)
gh api -X PATCH repos/$REPO \
  -f 'security_and_analysis[secret_scanning][status]=enabled' \
  -f 'security_and_analysis[secret_scanning_push_protection][status]=enabled'

# Private vulnerability reporting, Dependabot alerts on, Dependabot update PRs off (Renovate owns updates)
gh api -X PUT    repos/$REPO/private-vulnerability-reporting
gh api -X PUT    repos/$REPO/vulnerability-alerts
gh api -X DELETE repos/$REPO/automated-security-fixes

# Workflows get a read-only token by default and may not approve pull requests
gh api -X PUT repos/$REPO/actions/permissions/workflow \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
```

### Branch and tag rules

```sh
gh api -X POST repos/$REPO/rulesets --input - <<'JSON'
{
  "name": "protect-main",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "required_linear_history" },
    { "type": "pull_request", "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false } },
    { "type": "required_status_checks", "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [
          { "context": "Lint" }, { "context": "Type check" },
          { "context": "Tests (floor)" }, { "context": "Tests (current)" },
          { "context": "Docker build" }, { "context": "Compose validate" },
          { "context": "Secret scan" }, { "context": "Dependency audit" },
          { "context": "Image scan" }, { "context": "Workflow lint" },
          { "context": "PR title" } ] } }
  ]
}
JSON

gh api -X POST repos/$REPO/rulesets --input - <<'JSON'
{
  "name": "protect-release-tags",
  "target": "tag",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/tags/v*"], "exclude": [] } },
  "rules": [ { "type": "deletion" }, { "type": "update" } ]
}
JSON
```

Zero required approvals is deliberate for a single-maintainer repository (you cannot approve your own pull request);
the required checks are the gate. Raise it if you add maintainers. Create these rules **after** the first push, because an
active rule would also block the initial push of `main`.

## Publishing the container image

`ghcr.io/athegreat90/minecraft-neoforge-telegram-monitor` is published by `docker-publish.yml` only from the `main` branch or a
`v*.*.*` tag of the owner repository, and only after CI succeeds. The amd64 image is built and scanned with Trivy
(fixable CRITICAL/HIGH findings fail the run) **before** the multi-arch (`linux/amd64`, `linux/arm64`) push. The push
includes provenance (`mode=max`), an SBOM and a GitHub build attestation.

| Event | Tags written |
|---|---|
| push to `main` | `edge`, `sha-<commit>` (never `latest`) |
| tag `v1.2.3` | `1.2.3`, `1.2`, `1`, `latest`, `sha-<commit>` |
| tag `v1.2.3-rc.1` | `1.2.3-rc.1`, `sha-<commit>` (pre-releases never move `1.2`, `1` or `latest`) |

`latest`, `1.2` and `1` move **only** on stable release tags. Tags are not enforced immutable by GHCR, so the
`protect-release-tags` rule above and the practice of never re-pushing a version tag are what keep `1.2.3` fixed.

**Manual step: package visibility.** A package created from a user account may start as private. After the first
successful publish, open the package settings on GitHub and set the visibility to **Public** (the REST API cannot change
it). Until then anonymous `docker pull` fails.

## Releases

Releases are driven by [release-please](https://github.com/googleapis/release-please) from conventional commits on `main`:

1. Merged `feat:`, `fix:`, `perf:`, `refactor:`, `docs:`, `ci:` and `build:` commits are collected; `chore:` and `test:` are hidden
   in the changelog.
2. release-please keeps one release pull request open ("chore(main): release …") with the version bump and the
   `CHANGELOG.md` update.
3. **You** merge that pull request. release-please then creates the tag and the GitHub release, and `release.yml`
   calls `docker-publish.yml` for that tag. The call is explicit because tags and releases created with the default
   `GITHUB_TOKEN` do not trigger other workflows.

**First release.** `release-please-config.json` contains `"release-as": "1.0.0"` so the first release is v1.0.0. After v1.0.0 is
released, **delete that line**, otherwise every later release would stay at 1.0.0.

**CI on release pull requests.** Pull requests created with `GITHUB_TOKEN` do not start the pull-request workflows, so the
required checks stay pending. Workarounds: run `gh workflow run ci.yml --ref release-please--branches--main` (documented,
not tested), close and reopen the pull request, or give release-please a GitHub App / personal access token. Check
which works for you on the first release.

## Dependency and Python updates (Renovate)

[Renovate](https://docs.renovatebot.com/) is the **only** updater. There is intentionally no `dependabot.yml` and no custom
Python-update workflow; Dependabot *alerts* may stay enabled, Dependabot update pull requests must stay off.

**Setup (manual):** install the [Renovate GitHub App](https://github.com/apps/renovate) on the repository. It then reads
`.github/renovate.json5`. Nothing happens until the app is installed.

| What | How it is updated |
|---|---|
| Python packages (`uv.lock`) | Weekly lock-file maintenance (automerge) and update pull requests. Runtime dependencies use `fix(deps)`, dev tooling is grouped. |
| GitHub Actions | Kept pinned to commit SHAs. Patch and digest updates automerge; minor and major need review. |
| Docker base images | Pinned by digest. Digest-only updates automerge for non-Python images. |
| `uv` | Pinned in the Dockerfile and workflows; updates always need review. |
| Tools downloaded in workflows (gitleaks, actionlint, hadolint, zizmor) | Version updates are labelled `needs-checksum-update` and **fail on purpose** until you update the SHA-256 next to the version. |
| Security fixes | Opened at any time, labelled `security`, never auto-merged, and subject to exactly the same required checks. |

Every update waits three days after release (`minimumReleaseAge`) before a pull request is opened, to give maintainers time to
pull bad releases.

### Automatic Python upgrades

The Python version is declared in two places that Renovate updates **together in one pull request** (group `python`):

- `.python-version` (Renovate's `pyenv` manager, Docker datasource) and
- the two `FROM python:X.Y.Z-slim@sha256:…` lines in the `Dockerfile`.

`requires-python = ">=3.14"` in `pyproject.toml` is the supported **floor** and is never changed automatically. The CI job
`Tests (current)` reads `.python-version`, so the upgrade pull request tests the new interpreter, builds the new image
and runs the smoke tests.

| Update | Behaviour |
|---|---|
| Patch (`3.14.8` → `3.14.9`) and digest refreshes | Pull request labelled `python-patch`; merges automatically once every required check passes (after the 3-day wait). |
| New feature release (`3.14` → `3.15`) | Pull request labelled `python-feature-upgrade`; **never automerged**, you review it (7-day wait). |
| Alpha, beta, release candidates | Never proposed (`ignoreUnstable` plus a version pattern). |
| Image does not exist yet | Never proposed: Renovate looks versions up in the Docker registry, so a version appears only once `python:X.Y.Z-slim` is published. |
| Incompatible upgrade | The pull request's CI fails and it stays open; the Dependency Dashboard issue lists it. Nothing is merged. |

Example from the day this was set up (2026-10-12): Python 3.15.0 had been released on 2026-10-09, but `python:3.15-slim`
returned HTTP 404 on Docker Hub, so the project stays on 3.14.8 and the 3.15 pull request will appear by itself once the image is
published. A Renovate dry run on a copy of the repository proposed nothing for Python, which matches that state. The dry run did
not exercise the GitHub Actions manager or the custom regex managers, so check the first real Renovate pull requests.

A custom workflow was judged unnecessary: the pyenv manager, the Dockerfile manager and one `groupName` cover version detection,
image verification, synchronisation and the merge policy. Dependabot cannot keep `.python-version`, the Dockerfile and CI
in step.

After an approved merge to `main`, `docker-publish.yml` rebuilds and publishes `edge` and `sha-<commit>`. The stable tags
(`1.x.y`, `1`, `latest`) move at the next release, i.e. when you merge the release-please pull request.

## Rollback

- **A bad dependency or Python update**: revert the merge commit with a pull request (`git revert -m 1 <sha>`). The revert goes
  through the same checks; a new `edge` image is published.
- **A bad release on a server**: set `IMAGE_TAG` to the previous version (for example `1.0.0`) or to a `sha-<commit>` tag and run
  `docker compose -f compose.prod.yaml up -d`. Old tags are not deleted.
- **A bad database migration**: stop the service, restore the backup (`releases.sqlite3.bak-v1` for the v1 to v2 migration, or a
  `db-backup` copy), and start the previous image tag.
- Do not delete or move a published version tag; publish a new patch release instead.

## Manual approvals needed from the owner

| Step | Why it is manual |
|---|---|
| Install the Renovate GitHub App | Grants an app access to the repository |
| Apply repository settings and rulesets (above) | Changes repository policy |
| Merge the release pull request | A release is a public, outward-facing action |
| Set the GHCR package to Public | Cannot be done through the API |
| Merge Python feature-release pull requests | Policy: new feature releases always get a human review |
