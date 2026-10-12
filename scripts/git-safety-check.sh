#!/usr/bin/env bash
# Fail when secret-bearing files are tracked, staged, or not covered by .gitignore.
# Never prints file contents: only paths and verdicts.
#
# Allowed env-like files (placeholders only): .env.example and docs/env.example.
set -u

fail=0
report() {
  echo "SECURITY CHECK FAILED: $*" >&2
  fail=1
}

if ! repo_root=$(git rev-parse --show-toplevel 2>/dev/null); then
  echo "SECURITY CHECK FAILED: not inside a git repository" >&2
  exit 2
fi
cd "$repo_root" || exit 2

is_allowed() {
  case "$1" in
    .env.example | docs/env.example) return 0 ;;
  esac
  return 1
}

# Returns 0 when the path looks secret-bearing and is not an allowed template.
is_forbidden() {
  local path=$1 base lower
  if is_allowed "$path"; then
    return 1
  fi
  base=${path##*/}
  lower=$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')
  case "$lower" in
    .env | .env.* | *.pem | *.key | *.p12 | *.pfx | *.secret) return 0 ;;
    *.db | *.db-journal | *.sqlite | *.sqlite3) return 0 ;;
  esac
  case "/$path" in
    */secrets/* | */credentials/*) return 0 ;;
  esac
  return 1
}

# 1. Is .env tracked? `git ls-files --error-unmatch` exits 0 when it IS tracked.
git ls-files --error-unmatch .env >/dev/null 2>&1
rc=$?
case $rc in
  0) report ".env is tracked by git (run: git rm --cached .env; rotate the credentials if it was ever pushed)" ;;
  1) ;; # not tracked: expected
  *) report "could not determine whether .env is tracked (git exit code $rc)" ;;
esac

# 2. Is .env covered by .gitignore? `git check-ignore -q`: 0 ignored, 1 not ignored, 128 error.
git check-ignore -q .env
rc=$?
case $rc in
  0) ;; # ignored: expected
  1) report ".env is NOT covered by .gitignore" ;;
  *) report "git check-ignore failed (exit code $rc)" ;;
esac

# 3. Staged files (added, copied, modified, renamed).
while IFS= read -r -d '' path; do
  if is_forbidden "$path"; then
    report "forbidden file is staged: $path"
  fi
done < <(git diff --cached --name-only --diff-filter=ACMR -z)

# 4. Every tracked file.
while IFS= read -r -d '' path; do
  if is_forbidden "$path"; then
    report "forbidden file is tracked: $path"
  fi
done < <(git ls-files -z)

if [ "$fail" -ne 0 ]; then
  exit 1
fi
echo "git-safety-check: OK (no secret-bearing files tracked or staged; .env is ignored)"
