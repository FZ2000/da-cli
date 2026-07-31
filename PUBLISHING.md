# Publishing checklist

This repository starts **private**. Everything below must be done before it
is made public, and a few items only *work* once it is public — so the order
matters.

This file exists because several things in this repo are deliberately
configured for the private state and would be wrong, or silently useless,
once it is public. Each one is listed with what breaks if it is missed.

## The sequence

Ordered, because three of these only work in one direction.

1. **Flip to public.** Settings → General → Danger Zone → Change
   visibility. Everything below becomes possible at this point and not
   before.
2. **Enable the security features** (all free on a public repo, all
   refused on a private one — the API returns 422 / 404):
   Settings → Code security → turn on **secret scanning**, **push
   protection**, and **private vulnerability reporting**. The third one
   is what makes `SECURITY.md`'s "Report a vulnerability" link work
   rather than 404.
3. **Apply branch protection**: `./tools/apply_branch_protection.sh`.
   One command. It requires `ci-gate` and sets
   `strict_required_status_checks_policy`, i.e. "branch must be up to
   date before merging".
4. **Delete the `lychee.toml` self-link exclusion** (see below) and open
   a PR for it. Leaving it hides genuinely dead self-links.
5. **Verify the two gated workflows ran**: Actions → CodeQL should show
   a green run for both `python` and `actions`; Security → Code scanning
   should be populated (empty is fine, "not enabled" is not).

Optional, any time after:

6. **Social preview image** — Settings → General → Social preview.
   1280×640 PNG under 1 MB. Without one, link unfurls on X, Slack,
   Discord and LinkedIn use GitHub's auto-generated card.
7. **Cut a release.** See CONTRIBUTING.md §Releases. Note the ordering
   trap recorded there: tag first, *then* add the CHANGELOG link
   definitions. The file previously carried four such links for tags that
   were never created, and CI could not catch it because the link check
   runs `--offline`.

## Before flipping to public

### 1. Confirm nothing in this tree is private

Two scans, because they answer different questions:

```sh
gitleaks detect --no-git --redact          # the tree as it will be published
gitleaks git   --redact --exit-code 1 .    # the committed history
```

Both should report no leaks. This repository was created as a fresh
history rather than a filtered clone of the repo it was developed in —
the tracked files were copied and committed once — specifically so that
the development history, which contained a credential, is not present to
be scanned in the first place. Verified: the value appears in none of the
objects here, and the commit that introduced it does not exist in this
repository.

Worth a human pass for the things a scanner cannot recognise: internal
hostnames, absolute paths containing your username, personal email
addresses in `CITATION.cff` / `pyproject.toml` / commit metadata, and
screenshots showing a window title or sidebar you did not intend.

`tools/check_discoverability.py` asserts the mechanical half of this on
every CI run — no private-host URL in any tracked file, no `/Users/<name>/`
paths outside the `you` placeholder.

### 2. Check the About panel

Already set, and worth knowing why it matters: GitHub injects the
description verbatim into the page `<title>`, `<meta name="description">`,
and every `og:`/`twitter:` tag, and its default repository search matches
**only** name, description and topics — not the README. Those two fields
are the entire discovery surface.

- **Description** — set, and mirrored in `pyproject.toml` so PyPI agrees.
- **Topics** — 20 set, the documented maximum.
- **Website** — leave blank until GitHub Pages is live, then point it there.

## Activates automatically on publication — verify, do not edit

Two workflows are gated on `github.event.repository.private == false`:

| Workflow | Why it is gated |
| --- | --- |
| `.github/workflows/codeql.yml` | Code scanning is free for public repos; on a private repo it needs GitHub Advanced Security and the upload step fails |
| `.github/workflows/dependency-review.yml` | Needs the dependency graph, which also needs Advanced Security on a private repo |

They are written as job-level `if:` conditions rather than commented-out
files so that `actionlint` still checks them and the intent stays visible.
Nothing to uncomment — but **do** confirm after publishing:

- Actions → CodeQL shows a green run
- Security → Code scanning alerts is populated (empty is fine; "not enabled"
  is not)

## Must be removed after publication

### `lychee.toml` self-link exclusion

```toml
"^https://github\\.com/FZ2000/da-cli",
```

GitHub returns 404 to anonymous requests for a private repo, so the weekly
external link check cannot verify the repo's own URLs and would fail for a
reason that is not a broken link. **Delete this entry once public.**

Leaving it in is not harmless. The equivalent exclusion for the old private
forge host is exactly why four dead `CHANGELOG.md` release-tag links survived
unnoticed — the link checker was configured never to look at them.

## Enable in repository settings

| Setting | Where | Why |
| --- | --- | --- |
| Secret scanning | Settings → Code security | Free for public repos. Catches a credential in a future commit. |
| Push protection | Settings → Code security | Rejects the push *before* the secret reaches the remote, which is the only cheap moment to catch one. |
| Private vulnerability reporting | Settings → Code security | `SECURITY.md` tells people to use it; without this the link 404s. |
| Branch protection on `main` | Settings → Rules / Branches | Require the CI checks **and** "Require branches to be up to date before merging". |

That last one is not boilerplate. Without the up-to-date requirement, two
PRs that are each green can merge into a broken `main` with no textual
conflict. That happened on the previous forge: one PR removed an import as
unused while another added the first use of it. Both green, no conflict, and
`main` raised `NameError` on a path users hit.

## Renovate

`renovate.json` is configured for `pip` and `github-actions`, so **do not add
Dependabot** — the two would file duplicate PRs against the same manifests.
Renovate needs its GitHub App installed on the repo to do anything; until then
the config file is inert.

## Not blocking, worth doing

- **Tag a release.** `CHANGELOG.md` version headings are deliberately
  unlinked because no tag has ever existed. `CONTRIBUTING.md` §Releases has
  the step that adds the links along with the first tag — do it in that order,
  or the links 404 again.
- **Social preview image.** Settings → General → Social preview. Affects
  link-unfurl click-through on every platform that renders `og:image`.
- **OpenSSF Scorecard.** Publishes a supply-chain posture score and badge.
  Only meaningful on a public repo; add the workflow after publication rather
  than gating another one.
