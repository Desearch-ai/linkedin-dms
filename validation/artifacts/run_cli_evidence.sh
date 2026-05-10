#!/usr/bin/env bash
set -u
DB="validation/local-test.sqlite"
run_ok() {
  local name="$1"; shift
  echo "$*" > "validation/artifacts/cli-${name}.cmd"
  "$@" > "validation/artifacts/cli-${name}.json" 2> "validation/artifacts/cli-${name}.stderr"
  local code=$?
  echo "exit_code=${code}" > "validation/artifacts/cli-${name}.exit"
  if [[ "$code" -ne 0 ]]; then
    echo "command ${name} failed with ${code}" >&2
    return "$code"
  fi
}
run_expected_nonzero() {
  local name="$1"; shift
  echo "$*" > "validation/artifacts/cli-${name}.cmd"
  "$@" > "validation/artifacts/cli-${name}.json" 2> "validation/artifacts/cli-${name}.stderr"
  local code=$?
  echo "exit_code=${code}" > "validation/artifacts/cli-${name}.exit"
  if [[ "$code" -eq 0 ]]; then
    echo "command ${name} unexpectedly succeeded" >&2
    return 1
  fi
  return 0
}
run_ok status uv run python -m apps.cli status --db-path "$DB"
run_ok auth-status uv run python -m apps.cli auth status --account-id 1 --db-path "$DB"
run_ok sync-dry-run uv run python -m apps.cli sync --account-id 1 --db-path "$DB" --limit-per-thread 25 --max-pages-per-thread 1 --delay-threads 0 --delay-pages 0 --dry-run
run_ok inbox uv run python -m apps.cli inbox --account-id 1 --db-path "$DB" --limit 10 --json
run_ok search uv run python -m apps.cli search --account-id 1 --db-path "$DB" --query Bittensor --direction in --limit 10 --json
run_ok threads-list uv run python -m apps.cli threads list --account-id 1 --db-path "$DB" --limit 10 --json
run_ok threads-show uv run python -m apps.cli threads show --account-id 1 --thread-id 1 --db-path "$DB" --include-messages --limit 10 --json
run_ok messages-list uv run python -m apps.cli messages list --account-id 1 --thread-id 1 --db-path "$DB" --limit 10 --json
run_ok draft-reply uv run python -m apps.cli draft-reply --account-id 1 --thread-id 2 --db-path "$DB" --text "Synthetic local draft from CLI validation; no external send." --campaign-id 73 --idempotency-key obj73-cli-draft --json
run_ok campaign-status uv run python -m apps.cli campaign status --campaign-id 73 --account-id 1 --db-path "$DB" --json
run_ok campaign-dry-run uv run python -m apps.cli campaign run --campaign-id 73 --account-id 1 --db-path "$DB" --dry-run --limit 10 --json
run_expected_nonzero send-no-approval uv run python -m apps.cli send --account-id 1 --recipient "urn:li:msg_conversation:(synthetic,ada)" --text "Refusal proof: missing approval" --idempotency-key obj73-refuse-missing --db-path "$DB"
run_expected_nonzero send-approved-mismatch uv run python -m apps.cli send --approved appr_20260510_000001 --account-id 1 --recipient "urn:li:msg_conversation:(synthetic,ada)" --text "Tampered text should fail approval hash before provider" --idempotency-key obj73-approved-synthetic --db-path "$DB"
