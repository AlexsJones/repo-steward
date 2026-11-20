#!/usr/bin/env bash
# List, and with --approve run, the GitHub Actions workflow runs that sit in
# "awaiting approval" on open PRs across the steward's repos. GitHub holds
# runs from first-time fork contributors (and some bot-opened PRs) until a
# maintainer clicks "Approve and run"; the steward will not sign off a PR
# whose CI never ran, so these are the approved-but-unmerged backlog's biggest
# single cause. Only runs on the *current* head of an open PR are listed: held
# runs on superseded commits are left alone. Dry run by default. Approving a
# run executes the contributor's workflow changes on your runners: skim the PR
# list before you pass --approve.
#
#   ./approve_workflows.sh              # show held runs per repo and PR
#   ./approve_workflows.sh --approve    # approve them all
#   ./approve_workflows.sh --approve AlexsJones/llmfit   # one repo only
set -euo pipefail
cd "$(dirname "$0")"
ACT=0; ONLY=""
for a in "$@"; do case "$a" in --approve) ACT=1;; *) ONLY="$a";; esac; done
REPOS=$(sed -n 's/^  - name: *//p' config.yaml)
[[ -n "$ONLY" ]] && REPOS="$ONLY"
total=0; approved=0
for full in $REPOS; do
  heads=$(gh pr list -R "$full" --state open -L 200 --json headRefOid --jq '.[].headRefOid' 2>/dev/null | tr '\n' ' ')
  [[ -z "$heads" ]] && continue
  runs=$(gh api "repos/$full/actions/runs?status=action_required&per_page=100" \
    --jq '.workflow_runs[] | select(.pull_requests != null) | "\(.id)\t\(.head_sha)\t\(.name)\t\(.head_repository.owner.login // "?")\t\(.display_title)"' 2>/dev/null || true)
  [[ -z "$runs" ]] && continue
  echo "== $full"
  while IFS=$'\t' read -r id sha name owner title; do
    [[ " $heads " == *"$sha"* ]] || continue
    total=$((total+1))
    printf '  run %-13s %s  %-28s %-20s %s\n' "$id" "${sha:0:7}" "$name" "$owner" "${title:0:60}"
    if (( ACT )); then
      if gh api -X POST "repos/$full/actions/runs/$id/approve" >/dev/null 2>&1; then
        approved=$((approved+1)); echo "      approved"
      else
        echo "      approve failed (already approved, or no admin on $full?)"
      fi
    fi
  done <<<"$runs"
done
if (( ACT )); then echo "$approved of $total held runs approved"
else echo "$total held runs; re-run with --approve to start them"; fi
