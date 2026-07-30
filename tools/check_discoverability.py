#!/usr/bin/env python3
"""Assert the repo is actually discoverable, rather than hoping it is.

Most "SEO checklist" advice for GitHub repos is folklore. This script only
encodes rules that are either documented by GitHub/Google/PyPA, or that were
measured against live GitHub HTML and the search API. Each rule carries its
provenance so a future reader can tell a requirement from an opinion:

    DOC   documented by the platform; a violation is objectively wrong
    MEAS  measured empirically against live GitHub/PyPI
    INFER defensible reasoning, stated as such — argue with these freely

The single most important measured fact, which shapes half the rules:
**GitHub's default repository search matches only name, description and
topics — not the README.** ("deviantart sync" returns 1 result by default
and 782 with `in:readme`.) So the description and topics are the entire
GitHub-native discovery surface, and they are checked hardest.

    python3 tools/check_discoverability.py            # offline, files only
    python3 tools/check_discoverability.py --github   # also query the API

Offline mode needs nothing. `--github` uses `gh` if authenticated and skips
those rules otherwise, so this is safe to run in CI without a token.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import tomllib

REPO = Path(__file__).resolve().parent.parent
OWNER_REPO = "FZ2000/da-cli"
PRIMARY_KEYWORD = "deviantart"

# Terms that are genuine differentiators for this project. PyPI's search
# indexes the README body at a x5 boost (warehouse/search/queries.py
# SEARCH_BOOSTS), and Google's guidance is to place searched-for words in
# prominent locations — so a differentiator appearing zero times is a real
# loss on both surfaces, not a cosmetic one.
README_TERMS = (
    "deviantart",
    "sync",
    "cli",
    "oauth",
    "pkce",
    "sqlite",
    "launchd",
    "backup",
    "macos",
    "gallery",
)

# GitHub renders badge <img> through camo.githubusercontent.com with a
# content-hash URL, so alt text is the only text signal a badge contributes.
# These alts throw that away. (astral-sh/ruff ships alt="image" on 3 badges,
# so this is not widely observed practice.)
USELESS_ALT = {"", "image", "badge", "img", "logo", "screenshot", "icon"}

# Hosts that would be dead or private for every reader. Deliberately NOT a
# blanket loopback match: da-cli runs a loopback HTTPS listener for the OAuth
# redirect, so `https://localhost:8765/` is its documented default and appears
# legitimately in ~30 places. Flagging those was this script's own first false
# positive. What actually needs catching is the previous self-hosted forge:
# its Tailscale hostname, its `localhost:3000` API, and mDNS `.local` names.
PRIVATE_HOST = re.compile(
    # A real Tailscale hostname has a subdomain in front. Requiring one lets
    # prose *about* the migration say `*.ts.net` without tripping the rule —
    # PUBLISHING.md does exactly that.
    r"[\w-]+\.ts\.net"
    r"|localhost:3000"  # the old Gitea API
    r"|127\.0\.0\.1:3000"
    r"|[\w-]+\.local\b"  # mDNS names, unresolvable off-LAN
    # An absolute home path that names a REAL user. `/Users/you/` is the
    # correct way to write an example, and the docs use it 2x; flagging it was
    # this rule's second false positive.
    r"|/Users/(?!you/|user/|username/|me/|alice/|bob/|example/)[a-z][a-z0-9._-]*/",
    re.IGNORECASE,
)


@dataclass
class Result:
    """Tally of rule outcomes, rendered at the end."""

    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def check(self, rule: str, src: str, ok: bool, detail: str = "") -> None:
        """Record one rule outcome. `src` is DOC / MEAS / INFER."""
        line = f"{rule} [{src}]" + (f" — {detail}" if detail else "")
        (self.passed if ok else self.failed).append(line)

    def skip(self, rule: str, why: str) -> None:
        """Record a rule that could not run, with the reason."""
        self.skipped.append(f"{rule} — {why}")


# --------------------------------------------------------------------------
# README
# --------------------------------------------------------------------------
def check_readme(r: Result) -> None:
    p = REPO / "README.md"
    if not p.exists():
        r.check("README.md exists at root", "DOC", False)
        return
    text = p.read_text()

    # Not because "one H1 per page" is an SEO law — it is not, and a GitHub
    # repo page contains 4 <h1> elements regardless (3 are GitHub's own
    # dialogs). The reason is document outline / screen readers, plus this
    # is the one <h1> Google may choose as a title candidate.
    h1s = [ln for ln in text.splitlines() if ln.startswith("# ")]
    r.check("README has exactly one H1", "MEAS", len(h1s) == 1, f"found {len(h1s)}")

    # "any content beyond 500 KiB will be truncated" — GitHub About READMEs.
    # Deliberately NOT asserting a smaller "optimal length": no such number
    # is sourced anywhere.
    size = p.stat().st_size
    r.check(
        "README under the 500 KiB indexing ceiling",
        "DOC",
        size < 500 * 1024,
        f"{size / 1024:.1f} KiB",
    )

    imgs = re.findall(r"!\[([^\]]*)\]\(", text)
    bad = [a for a in imgs if a.strip().lower() in USELESS_ALT]
    r.check(
        "every README image has meaningful alt text",
        "DOC",
        not bad,
        f"{len(bad)} generic: {bad[:4]}" if bad else f"{len(imgs)} images",
    )

    lower = text.lower()
    missing = [t for t in README_TERMS if t not in lower]
    r.check(
        "README mentions every differentiator",
        "DOC+MEAS",
        not missing,
        f"missing: {missing}" if missing else f"all {len(README_TERMS)}",
    )

    head = "\n".join(text.splitlines()[:12]).lower()
    r.check(
        f"'{PRIMARY_KEYWORD}' appears in the H1 or opening lines", "DOC", PRIMARY_KEYWORD in head
    )

    # Google's spam policy names keyword stuffing as "keywords ... in a list
    # or group, unnaturally, or out of context", and hidden-text abuse covers
    # HTML comments. A tag dump at the bottom of a README is both.
    stuffed = re.search(r"^\s*(keywords?|tags)\s*:", text, re.MULTILINE | re.IGNORECASE)
    hidden = [c for c in re.findall(r"<!--(.*?)-->", text, re.DOTALL) if c.count(",") > 5]
    r.check("no keyword-stuffing block or hidden term list", "DOC", not stuffed and not hidden)


# --------------------------------------------------------------------------
# Anything published must not point at a private host
# --------------------------------------------------------------------------
def check_no_private_hosts(r: Result) -> None:
    tracked = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=False
    ).stdout.split()

    # A scanner that scanned nothing must not report success. `git ls-files`
    # returns empty outside a work tree, and this rule duly "passed" against a
    # `git checkout-index` extract — which is exactly where I ran it to gain
    # confidence before committing. Fail loudly instead.
    if not tracked:
        r.check(
            "no private-host URL in any tracked file",
            "MEAS",
            False,
            "git listed no files — not a work tree, so nothing was scanned",
        )
        return

    hits: list[str] = []
    for rel in tracked:
        # This file necessarily contains every pattern it searches for, in its
        # own regex and in the comments explaining it. Skipping it is the
        # standard exemption a linter needs for its own rule definitions.
        if rel == "tools/check_discoverability.py":
            continue
        f = REPO / rel
        if not f.is_file() or f.suffix in {".png", ".jpg", ".gif", ".webp", ".db"}:
            continue
        try:
            body = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(body.splitlines(), 1):
            if PRIVATE_HOST.search(line):
                hits.append(f"{rel}:{i}")
    r.check(
        "no private-host URL in any tracked file",
        "MEAS",
        not hits,
        f"{len(hits)}: {hits[:5]}" if hits else "",
    )


# --------------------------------------------------------------------------
# Files GitHub surfaces in its own UI
# --------------------------------------------------------------------------
def check_community_files(r: Result) -> None:
    # LICENSE must be at root: "You cannot create a default license file."
    r.check("LICENSE at repo root", "DOC", (REPO / "LICENSE").exists())

    # These three are resolved from .github/, root, or docs/.
    for name in ("CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md"):
        found = any((REPO / d / name).exists() for d in ("", ".github", "docs"))
        r.check(f"{name} present", "DOC", found)

    # SECURITY.md specifically: jekyll/jekyll's SECURITY.markdown is NOT
    # recognised, so the extension matters.
    r.check("SECURITY uses the .md extension", "MEAS", (REPO / "SECURITY.md").exists())

    forms = sorted((REPO / ".github/ISSUE_TEMPLATE").glob("*.yml"))
    real = [f for f in forms if f.name != "config.yml"]
    r.check("at least one .yml issue form", "DOC", bool(real), f"{len(real)} forms")
    for f in real:
        try:
            body = f.read_text()
            ok = "name:" in body and "description:" in body
        except OSError:
            ok = False
        r.check(f"{f.name} has name+description", "DOC", ok)

    r.check("PR template present", "DOC", (REPO / ".github/PULL_REQUEST_TEMPLATE.md").exists())

    # FUNDING.yml is the one health file with a single mandated location.
    for d in ("", "docs"):
        stray = REPO / d / "FUNDING.yml"
        if stray.exists():
            r.check("FUNDING.yml is in .github/", "DOC", False, f"found at {d or '.'}/")


def check_codeowners_paths(r: Result) -> None:
    """Every CODEOWNERS pattern must match something.

    GitHub silently ignores a pattern that matches no file, so a stale path
    reads as review protection while providing none. Two entries here were
    exactly that before this check existed: `dacli.py` (now the `dacli/`
    package) and `docs/security.md` (now under `docs/explanation/`).
    """
    p = REPO / ".github/CODEOWNERS"
    if not p.exists():
        r.skip("CODEOWNERS path checks", "no .github/CODEOWNERS")
        return
    dead: list[str] = []
    for raw in p.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        pattern = line.split()[0]
        if pattern == "*":
            continue
        target = REPO / pattern.lstrip("/")
        if not target.exists():
            dead.append(pattern)
    r.check(
        "every CODEOWNERS pattern matches something",
        "DOC",
        not dead,
        f"dead: {dead}" if dead else "",
    )


def check_citation(r: Result) -> None:
    p = REPO / "CITATION.cff"
    if not p.exists():
        r.skip("CITATION.cff checks", "no CITATION.cff (optional)")
        return
    text = p.read_text()
    # Exactly the four top-level `required: true` fields in the CFF schema.
    missing = [
        k
        for k in ("cff-version", "message", "title", "authors")
        if not re.search(rf"^{k}:", text, re.MULTILINE)
    ]
    r.check(
        "CITATION.cff has all 4 required fields",
        "DOC",
        not missing,
        f"missing: {missing}" if missing else "",
    )
    r.check("CITATION.cff URLs are not private hosts", "MEAS", not PRIVATE_HOST.search(text))


# --------------------------------------------------------------------------
# pyproject.toml — the PyPI surface
# --------------------------------------------------------------------------
def check_pyproject(r: Result) -> None:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    proj = cfg["project"]

    # PyPI hard-enforces summary <= 512 (forklift/metadata.py _LENGTH_LIMITS)
    # and renders it verbatim as the page's <meta name="description">.
    desc = proj.get("description", "")
    r.check(
        "description non-empty and <= 512 chars",
        "DOC",
        bool(desc) and len(desc) <= 512,
        f"{len(desc)} chars",
    )

    kw = proj.get("keywords", [])
    r.check("at least 5 keywords", "INFER", len(kw) >= 5, f"{len(kw)}")

    groups = {c.split("::")[0].strip() for c in proj.get("classifiers", [])}
    r.check(
        "classifiers span >= 5 facet groups",
        "DOC+INFER",
        len(groups) >= 5,
        f"{len(groups)}: {sorted(groups)}",
    )
    r.check(
        "'Environment :: Console' classifier",
        "DOC",
        any(c.startswith("Environment :: Console") for c in proj.get("classifiers", [])),
    )

    # setuptools >= 77 hard-errors when an SPDX `license` and a License::
    # classifier are both present (PEP 639).
    has_spdx = isinstance(proj.get("license"), str)
    has_lic_classifier = any(c.startswith("License ::") for c in proj.get("classifiers", []))
    r.check(
        "no License:: classifier alongside an SPDX license",
        "DOC",
        not (has_spdx and has_lic_classifier),
    )

    # Claiming Typing :: Typed without shipping the PEP 561 marker means a
    # downstream mypy ignores our annotations entirely.
    typed_claim = "Typing :: Typed" in proj.get("classifiers", [])
    marker = (REPO / "dacli" / "py.typed").exists()
    r.check(
        "'Typing :: Typed' iff py.typed ships",
        "DOC",
        typed_claim == marker,
        f"classifier={typed_claim} marker={marker}",
    )

    # Warehouse's link-icon macro is lowercase-match only with no punctuation
    # stripping, so only these exact labels get their icon.
    urls = proj.get("urls", {})
    allowed = {"Homepage", "Documentation", "Repository", "Issues", "Changelog"}
    unknown = set(urls) - allowed
    r.check(
        "project.urls uses PyPA's recognised labels",
        "DOC",
        not unknown,
        f"unrecognised: {sorted(unknown)}" if unknown else f"{sorted(urls)}",
    )
    too_long = [k for k in urls if len(k) > 32]
    r.check("every URL label <= 32 chars", "DOC", not too_long)
    r.check(
        "no private host in project.urls",
        "MEAS",
        not any(PRIVATE_HOST.search(v) for v in urls.values()),
    )


# --------------------------------------------------------------------------
# GitHub API (optional)
# --------------------------------------------------------------------------
def check_github(r: Result) -> None:
    def api(path: str) -> object | None:
        out = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout else None

    repo = api(f"repos/{OWNER_REPO}")
    if repo is None:
        r.skip("GitHub API rules", "gh unavailable or not authenticated")
        return

    desc = repo.get("description") or ""
    r.check("repo description is set", "MEAS+DOC", bool(desc))
    # 350 is GitHub's enforced cap. Secondary source (desktop/desktop#19465),
    # not official docs — hence INFER.
    r.check("description <= 350 chars", "INFER", len(desc) <= 350, f"{len(desc)}")

    # <title> is "GitHub - {owner}/{repo}: {desc} · GitHub". The prefix for
    # this repo is 24 chars, so at the ~60 visible chars a SERP typically
    # shows, only the first ~36 chars of the description survive.
    budget = 60 - len(f"GitHub - {OWNER_REPO}: ")
    r.check(
        f"primary keyword within the first {budget} description chars",
        "MEAS",
        PRIMARY_KEYWORD in desc[:budget].lower(),
        f"'{desc[:budget]}'",
    )
    r.check(
        "description does not open with an article",
        "INFER",
        not re.match(r"^(a|an|the)\s", desc, re.IGNORECASE),
    )

    topics = (api(f"repos/{OWNER_REPO}/topics") or {}).get("names", [])
    r.check("1..20 topics", "DOC", 1 <= len(topics) <= 20, f"{len(topics)}")
    bad = [t for t in topics if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", t)]
    r.check(
        "every topic is lowercase/digits/hyphens, <= 50 chars",
        "DOC",
        not bad,
        f"invalid: {bad}" if bad else "",
    )
    # No documented penalty exists below the cap of 20, and topic-match was
    # the only positively rank-correlated text signal measured. The popular
    # "5-8 topics is the sweet spot" advice is folklore.
    r.check("at least 8 topics", "MEAS", len(topics) >= 8, f"{len(topics)}")

    lic = (repo.get("license") or {}).get("spdx_id")
    r.check("GitHub detected the licence", "DOC+MEAS", lic not in (None, "NOASSERTION"), f"{lic}")
    r.check("issues are enabled", "MEAS", repo.get("has_issues") is True)
    r.check(
        "repo is neither archived nor a template",
        "INFER",
        not repo.get("archived") and not repo.get("is_template"),
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--github", action="store_true", help="also check the repo's GitHub API metadata"
    )
    args = ap.parse_args()

    r = Result()
    check_readme(r)
    check_no_private_hosts(r)
    check_community_files(r)
    check_codeowners_paths(r)
    check_citation(r)
    check_pyproject(r)
    if args.github:
        check_github(r)
    else:
        r.skip("GitHub API rules", "pass --github to include them")

    for line in r.failed:
        print(f"  FAIL  {line}")
    for line in r.skipped:
        print(f"  SKIP  {line}")
    print(f"\n  {len(r.passed)} passed, {len(r.failed)} failed, {len(r.skipped)} skipped")
    if r.failed:
        print(
            "\n  Rules are tagged DOC (documented — a violation is wrong), "
            "MEAS (measured), or\n  INFER (reasoned; argue with these)."
        )
    return 1 if r.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
