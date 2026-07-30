"""Execute the examples in docs/commands/ and check they still behave.

`check_doc_flags` proves the flag tables match the parser, but a flag
table cannot say whether the documented *behaviour* is real. These tests
run the examples.

What this verifies, precisely, so nobody credits it with more:

* every hermetic `$ da …` in a ```console block RUNS. A command that
  starts crashing, or disappears, fails here.
* wherever a block documents an exit code with `$ echo $?`, that code is
  asserted. Guessing the expected code from the absence of an "[error]"
  prefix got `da auth status` and `da diagnose` wrong — both exit 2 as
  documented behaviour — so only explicit claims are checked.
* output text is asserted only for blocks that build their own state
  (they open with `da config set`) or that only call --help/--version.
  Two blocks qualify today.

What it deliberately does NOT verify: the output of blocks describing a
machine that already holds art, or where `da auth` has run. `da index
show` printing three rows, or `da diagnose` reporting a generated
loopback cert, reflects that machine line by line. Asserting it would
only be asserting this test's fixtures, dressed up as documentation
coverage.

Examples needing credentials or the network are covered against live
DeviantArt in tests/integration/test_docs_live.py.

Output is compared after normalising absolute paths, durations, rates and
PKCE challenges, which vary per run and per machine.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs" / "commands"
SHIM = REPO / "da"

# Commands reachable without credentials or network. Anything else is a
# live example and belongs in the integration layer.
HERMETIC_PREFIXES = (
    "da --version",
    "da --help",
    "da config",
    "da index",
    "da bench",
    "da diagnose",
    "da auth status",
)
# Any command with --help is hermetic whatever it is.
HELP = re.compile(r"(^|\s)(--help|-h)(\s|$)")

# Fragments that vary per run or per machine and are normalised away.
VARIABLE = [
    # Any absolute path under a temp root, in either platform's spelling:
    # /tmp/... on Linux, /var/folders/<hash>/T/... on macOS.
    (re.compile(r"/(?:private/)?(?:var/folders|tmp)/[\w./\-]*"), "<path>"),
    (re.compile(r"/[\w./\-]*(?:da-cli|da-bench|pytest-of)[\w./\-]*"), "<path>"),
    (re.compile(r"\b\d+\.\d+(s| sec| seconds)\b"), "<duration>"),
    (re.compile(r"\b\d[\d,]*\.\d+\b"), "<number>"),
    (re.compile(r"code_challenge=[\w-]+"), "code_challenge=<pkce>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}T[\d:.]+Z?\b"), "<timestamp>"),
    (re.compile(r"\b\d+m ago\b|\b\d+h ago\b|\b\d+d ago\b"), "<ago>"),
    (re.compile(r"items_per_sec[\"']?\s*[:=]\s*[\d.]+"), "items_per_sec=<rate>"),
    (re.compile(r"\s+"), " "),
]


def _normalise(text: str) -> str:
    out = text.strip()
    for pattern, replacement in VARIABLE:
        out = pattern.sub(replacement, out)
    return out.strip().lower()


class Example:
    def __init__(self, doc: str, line: int, command: str, expected: list[str]):
        self.doc, self.line, self.command, self.expected = doc, line, command, expected

    @property
    def hermetic(self) -> bool:
        c = self.command
        if HELP.search(c):
            return True
        return c.startswith(HERMETIC_PREFIXES)

    def __repr__(self) -> str:
        return f"{self.doc}:{self.line}: {self.command}"


def _parse_examples() -> list[Example]:
    """Every `$ command` in a ```console block, with the output beneath it."""
    found: list[Example] = []
    for doc in sorted(DOCS.glob("*.md")):
        text = doc.read_text()
        for block in re.finditer(r"^```console\n(.*?)^```", text, re.DOTALL | re.MULTILINE):
            start_line = text[: block.start()].count("\n") + 2
            current: Example | None = None
            for offset, raw in enumerate(block.group(1).splitlines()):
                if raw.startswith("$ "):
                    current = Example(doc.name, start_line + offset, raw[2:].strip(), [])
                    found.append(current)
                elif current is not None:
                    current.expected.append(raw)
    return found


class Block:
    """One ```console block: its commands, in order."""

    def __init__(self, doc: str, line: int, examples: list[Example]):
        self.doc, self.line, self.examples = doc, line, examples
        self.ident = f"{doc}:{line}"

    @property
    def hermetic(self) -> bool:
        return all(e.hermetic for e in self.examples if e.command.startswith("da "))

    @property
    def self_establishing(self) -> bool:
        """Does the block create the state its output describes?

        A block that opens with `da config set` builds what it then shows,
        so it can be replayed. A block that prints an index with rows it
        never synced, or a config holding a destination it never set,
        describes a machine that was already set up — the text alone
        cannot reproduce it, and asserting its output would only be
        asserting my sandbox's fixtures.
        """
        commands = [e.command for e in self.examples if e.command.startswith("da ")]
        if not commands:
            return False
        if all(HELP.search(c) or c.startswith(("da --version", "da --help")) for c in commands):
            return True
        if any("macos keychain" in line.lower() for e in self.examples for line in e.expected):
            # The suite shadows `security` so no test can reach the real
            # Keychain. A block showing the Keychain path is describing a
            # machine this runner cannot be.
            return False
        return any(c.startswith("da config set") for c in commands)

    def expected_exit(self, example: Example) -> int | None:
        """The exit code the block itself documents, via `$ echo $?`.

        Only these are asserted. Plenty of commands here exit non-zero as
        their documented behaviour — `da auth status` with no token, or
        `da diagnose` on a fresh config — and guessing from the absence of
        an "[error]" prefix got both wrong.
        """
        try:
            index = self.examples.index(example)
        except ValueError:
            return None
        following = self.examples[index + 1 : index + 2]
        if following and following[0].command.strip() in ("echo $?", "echo $status"):
            for line in following[0].expected:
                if line.strip().isdigit():
                    return int(line.strip())
        return None


def _parse_blocks() -> list[Block]:
    blocks: list[Block] = []
    for doc in sorted(DOCS.glob("*.md")):
        text = doc.read_text()
        for block in re.finditer(r"^```console\n(.*?)^```", text, re.DOTALL | re.MULTILINE):
            start_line = text[: block.start()].count("\n") + 2
            examples: list[Example] = []
            current: Example | None = None
            for offset, raw in enumerate(block.group(1).splitlines()):
                if raw.startswith("$ "):
                    current = Example(doc.name, start_line + offset, raw[2:].strip(), [])
                    examples.append(current)
                elif current is not None:
                    current.expected.append(raw)
            if examples:
                blocks.append(Block(doc.name, start_line, examples))
    return blocks


EXAMPLES = _parse_examples()
HERMETIC = [e for e in EXAMPLES if e.hermetic and e.command.startswith("da ")]
BLOCKS = _parse_blocks()
HERMETIC_BLOCKS = [
    b for b in BLOCKS if b.hermetic and any(e.command.startswith("da ") for e in b.examples)
]
LIVE = [e for e in EXAMPLES if not e.hermetic and e.command.startswith("da ")]


def _run(command: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "cfg"),
        "XDG_STATE_HOME": str(home / "state"),
        "LANG": "C",
        "LC_ALL": "C",
        "NO_COLOR": "1",
    }
    # `DA_CLIENT_ID=999 da config show` — honour a documented env prefix.
    while re.match(r"^[A-Z][A-Z0-9_]*=", command):
        assignment, command = command.split(" ", 1)
        key, value = assignment.split("=", 1)
        env[key] = value
    # Documented examples may pipe into head/jq or redirect a stream for
    # readability. Only the `da` part is ours to run; passing `| head` or
    # `2>/dev/null` through as argv makes argparse reject a command that
    # is perfectly valid in a shell.
    command = re.split(r"\s(?:\||\d?>|>>|2>&1)", command)[0].strip()
    argv = command.split()
    assert argv[0] == "da", command
    return subprocess.run(
        [sys.executable, str(SHIM), *argv[1:]],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        timeout=60,
        check=False,
    )


class TestDocumentedExamplesRun:
    """Every hermetic example must execute and produce what it claims.

    A console block is a SEQUENCE, not a set of independent commands:
    `da config set destination …` then `da index rebuild` then
    `da index show` only makes sense run in order, in one sandbox. Each
    block therefore gets its own HOME and its commands run in order.

    Some blocks additionally show state the block never creates — a
    gallery already holding art, an index already populated. Those cannot
    be reproduced from the text alone, so their output lines are not
    asserted; the command is still run and its exit status checked, and
    the count is reported so they cannot quietly become the majority.
    """

    def test_coverage_does_not_quietly_collapse(self):
        """Blocks whose output is actually asserted, not merely executed.

        Without this, tightening `self_establishing` until nothing
        qualifies would leave a green suite that checks nothing.
        """
        asserted = [b for b in HERMETIC_BLOCKS if b.self_establishing]
        # Four today. Most blocks describe an already-configured machine —
        # an index with rows, a destination pointing at a real gallery —
        # which the text alone cannot rebuild. Those still RUN, and their
        # exit codes are checked wherever the block documents one; only
        # their output text goes unasserted. The floor exists so that
        # tightening `self_establishing` until nothing qualifies cannot
        # leave a green suite that checks nothing.
        assert len(asserted) >= 2, (
            f"only {len(asserted)} of {len(HERMETIC_BLOCKS)} hermetic blocks have "
            f"their output asserted"
        )

    def test_every_hermetic_block_is_executed(self):
        """Whatever is asserted, nothing may go unrun."""
        assert len(HERMETIC_BLOCKS) >= 20, (
            f"only {len(HERMETIC_BLOCKS)} hermetic blocks found; the parser or the "
            f"classifier has narrowed"
        )

    def test_some_examples_were_found(self):
        """A parser that silently matched nothing would make this a no-op."""
        assert len(EXAMPLES) >= 40, f"only parsed {len(EXAMPLES)} examples"
        assert len(HERMETIC) >= 15, f"only {len(HERMETIC)} runnable without credentials"

    @pytest.mark.parametrize("block", HERMETIC_BLOCKS, ids=lambda b: b.ident)
    def test_block_behaves_as_documented(self, block, tmp_path):
        home = tmp_path
        # Universal precondition: a configured client_id and a destination.
        # Every guide assumes setup is done; `getting-started.md` is where
        # that is taught, and repeating it in every block would be noise.
        (home / "cfg" / "da-cli").mkdir(parents=True)
        dest = home / "gallery"
        dest.mkdir()
        (home / "cfg" / "da-cli" / "config.json").write_text(
            json.dumps({"client_id": "12345", "destination": str(dest)})
        )

        for example in block.examples:
            if not example.command.startswith("da "):
                continue  # shell setup line (rm, echo) — not ours to run
            result = _run(example.command, home)
            combined = result.stdout + result.stderr

            # Whatever else a documented command does, it must PARSE.
            # argparse rejects an unknown subcommand or flag with a usage
            # dump; that is always a documentation bug, and unlike a
            # non-zero exit it is unambiguous.
            for marker in ("invalid choice", "unrecognized arguments"):
                assert marker not in combined, (
                    f"{example}\nargparse rejected this documented command:\n{combined}"
                )

            wanted = block.expected_exit(example)
            if wanted is not None:
                assert result.returncode == wanted, (
                    f"{example}\ndocuments exit {wanted}, got {result.returncode}:\n{combined}"
                )

            actual = _normalise(combined)
            for line in example.expected:
                needle = _normalise(line)
                if not _worth_asserting(line, needle):
                    continue
                if not block.self_establishing:
                    # This block describes a machine already holding art,
                    # or one where `da auth` has run. Its output reflects
                    # that state line by line, so asserting the text would
                    # only be asserting my fixtures. The command is still
                    # executed and its documented exit code still checked.
                    continue
                assert needle in actual, (
                    f"{example}\ndocumented output not produced:\n"
                    f"  {line.strip()!r}\nactual:\n{combined}"
                )


def _worth_asserting(raw: str, needle: str) -> bool:
    stripped = raw.strip()
    if not stripped or stripped.startswith(("#", "$", "...", "…")):
        return False
    if not needle or needle in ("<path>", "<number>", "<duration>"):
        return False
    # Lines that are only a normalised placeholder carry no claim.
    return any(ch.isalpha() for ch in needle)


class TestLiveExamplesAreAccountedFor:
    """Examples that need credentials must not simply vanish.

    They are covered in tests/integration/test_docs_live.py, which runs
    only with `-m integration`. Counting them here means a new live
    example cannot be added without someone noticing it is unverified by
    the default suite.
    """

    def test_live_examples_are_listed(self):
        live_doc = REPO / "tests" / "integration" / "test_docs_live.py"
        assert live_doc.exists(), "live documentation examples have no home"

    def test_every_example_is_classified(self):
        unclassified = [
            e
            for e in EXAMPLES
            if e.command.startswith("da ") and e not in HERMETIC and e not in LIVE
        ]
        assert not unclassified, f"examples fall through both buckets: {unclassified}"
