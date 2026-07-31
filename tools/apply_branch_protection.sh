#!/usr/bin/env bash
# Apply the branch ruleset for `main`. One command, idempotent.
#
# WHY THIS IS A SCRIPT
#
# Rulesets and the older branch-protection API are not available on every
# plan and visibility combination; where they are not, GitHub answers:
#
#   403  Upgrade to GitHub Pro or make this repository public to enable
#        this feature.
#
# So this is run when the repository can accept it, rather than assumed at
# setup time. Until then the only guard against a direct push to main is
# .githooks/pre-push, which is client-side and bypassable — it stops
# accidents, not intent.
#
#   git config core.hooksPath .githooks    # enable that hook, per clone
#   ./tools/apply_branch_protection.sh     # run this once, after going public
set -euo pipefail

REPO="${1:-FZ2000/da-cli}"

echo "Applying ruleset to $REPO ..."

# `ci-gate` is the single required check on purpose: the test job is a matrix
# reporting as "test (3.10)".."test (3.14)", and three jobs are conditional.
# Requiring the aggregate avoids editing this every time the matrix changes,
# and avoids a required-but-skipped check blocking merges forever.
if gh api "repos/$REPO/rulesets" --jq '.[].name' 2>/dev/null | grep -qx "main protection"; then
    echo "  ruleset 'main protection' already exists — delete it first to re-apply"
    exit 0
fi

gh api -X POST "repos/$REPO/rulesets" --input - <<'JSON'
{
  "name": "main protection",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": true,
        "automatic_copilot_code_review_enabled": false,
        "allowed_merge_methods": ["squash", "merge", "rebase"]
      }
    },
    { "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "do_not_enforce_on_create": false,
        "required_status_checks": [ { "context": "ci-gate" } ]
      }
    }
  ],
  "bypass_actors": []
}
JSON

echo "  done. Verify: gh api repos/$REPO/rulesets --jq '.[].name'"
echo
echo "Note: strict_required_status_checks_policy=true is the 'branch must be"
echo "up to date before merging' requirement. Without it two green PRs can"
echo "merge into a broken main with no textual conflict — a deletion PR is"
echo "the classic case: 'nothing uses this' is the one claim another merge"
echo "can invalidate without touching your diff."
