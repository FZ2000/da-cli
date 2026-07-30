# Publishing checklist

This repository starts **private**. Everything below must be done before it
is made public, and a few items only *work* once it is public — so the order
matters.

This file exists because several things in this repo are deliberately
configured for the private state and would be wrong, or silently useless,
once it is public. Each one is listed with what breaks if it is missed.

## Blocking — do these before flipping to public

### 1. Rotate the DeviantArt client secret

**Status: not done. This is the hard blocker.**

Rotate the app credential at <https://www.deviantart.com/developers/apps>,
then `da config set client_secret <new>`.

Why, without the detail: the credential currently configured locally was
exposed outside this repository during earlier development and must be
treated as compromised. This repo does not contain it — the history is
fresh, the tree scans clean, and `.gitleaksignore` is empty — so publishing
*this* repo does not disclose it. But "the new repo is clean" and "the
secret is safe" are different claims, and only the second one matters.

The specifics of which credential and how it was exposed are deliberately
not written down here. This file ships in the repository and is not
export-ignored, so anything in it is public the moment the repo is — and a
file that names an app whose secret is known-leaked-and-unrotated is a
usable tip, not a checklist item. Keep that detail in your notes, not in
git.

### 2. Confirm nothing in this tree is private

Two scans, because they answer different questions:

```sh
gitleaks detect --no-git --redact          # the tree as it will be published
gitleaks git   --redact --exit-code 1 .    # the committed history
```

Both should report no leaks. Also worth a human pass for things a scanner
cannot recognise: internal hostnames, absolute paths containing your username,
personal email addresses in `CITATION.cff` / `pyproject.toml` / commit
metadata, and screenshots with a window title or sidebar that shows more than
you meant.

The previous forge's private hostname has been removed from all 13 files
that carried it, including the runtime `USER_AGENT` in `dacli/constants.py`
— that one was sending it to DeviantArt on every request.

### 3. Set the repository About panel

Not cosmetic: the description is what GitHub puts in the page's `og:description`
and what search engines show as the snippet. An empty About panel means the
snippet gets scraped from whatever text happens to be near the top of the
README.

- **Description**: already set on the repo, and mirrored in
  `pyproject.toml`'s `description` so PyPI and GitHub agree.
- **Topics**: set them. GitHub allows up to 20; they drive GitHub's own
  search and appear in the page markup.
- **Website**: leave blank until GitHub Pages is live, then point it there.

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
