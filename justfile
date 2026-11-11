# The gate recipes of Sextant's justfile (see ORIGINS.md), unchanged except `session-start`, which
# keeps the checks whose scripts ship here; Sextant's host-specific recipes are not included.
set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

# Install the locked Python dependencies.
sync:
    uv sync --frozen

# Everything the required `ci` check runs.
ci: sync lint test secrets

# Check the dsh profile lock and live profile, the ecosystem index, compaction and role definitions.
session-start:
    python3 scripts/dsh_profile_doctor.py
    python3 scripts/ecosystem_index.py --check
    python3 -I scripts/compact_context.py --check </dev/null && python3 scripts/role_launch.py --check </dev/null

lint:
    uv run --frozen ruff check .
    uv run --frozen ruff format --check .
    uv run --frozen pyright
    uv run --frozen zizmor --offline .github/workflows
    shellcheck .github/actions/ci-tools/install.sh
    mapfile -t workflows < <(find .github/workflows -maxdepth 1 -type f \( -name '*.yml' -o -name '*.yaml' \) ! -name '*.lock.yml' | sort); actionlint -shellcheck shellcheck "${workflows[@]}"

test path="":
    uv run --frozen pytest -n auto {{path}}

# Scan this PR's commits when CI gives a range, otherwise the working tree.
secrets:
    if [[ -n "${BASE_SHA:-}" && -n "${HEAD_SHA:-}" ]]; then gitleaks git --no-banner --redact --log-opts="$BASE_SHA..$HEAD_SHA" .; else gitleaks dir --no-banner --redact .; fi

# Run host proofs; container proofs run in the devcontainer build and GitHub-settings proofs need the admin token.
prove marker="not container and not github":
    uv run --frozen pytest tests/proofs -m "{{marker}}"

# Push a branch and open its PR; pass false as the third argument to leave auto-merge off.
pr title body_file automerge="true":
    bash scripts/pr-check.sh "{{body_file}}" && git push -u origin HEAD
    gh pr create --title "{{title}}" --body-file "{{body_file}}"
    if [[ "{{automerge}}" == true ]]; then gh pr merge --auto --squash; fi
