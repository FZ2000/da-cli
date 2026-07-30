"""`http_bytes` was the one HTTP path that said nothing, ever.

Two commits built the debug tracing out: one added it to `http_json`, a
later one caught `http_post_json` up ("now logs its URL at debug like
http_json does"). `http_bytes` was missed by both, and nothing in the
code or those messages suggests it was meant to be silent — its own
docstring claims the "same retry contract as http_json".

The retry *semantics* really were identical; only the trace was missing.
That mattered more than it sounds, because the image path is the only one
with no other diagnostic: `_save_one` wraps the call in a blanket except
that turns any failure into a `fail:<Class>` string, and the per-page loop
printed only `ok` and `dry`. So a CDN image that 503'd three times burned
~4.5s of backoff and produced literally no output at any verbosity, `-v`
included — while `docs/reference/cli.md` advertises `-v` as emitting
"HTTP retry traces". The only evidence was `fail=1` in the summary, with
no id, no artist and no reason.

Fixing the silence first required fixing `_redact`, which is the security
half of this file: wixmp signs every CDN content URL with a bare
`?token=<JWT>`, and the redactor did not cover `token`.
"""

from __future__ import annotations

import argparse
import contextlib
import urllib.error
import urllib.parse
from collections.abc import Iterator
from unittest.mock import patch

import pytest

import dacli
from dacli.net import _redact


@contextlib.contextmanager
def verbosity(level: str) -> Iterator[None]:
    """Drive `log`'s module-level state directly.

    `_configure_output` is the production entry point but takes CLI flags;
    for a unit test the level itself is the thing under test.
    """
    saved = dacli._OUTPUT_STATE["verbosity"]
    dacli._OUTPUT_STATE["verbosity"] = level
    try:
        yield
    finally:
        dacli._OUTPUT_STATE["verbosity"] = saved


# The shape wixmp actually returns, from tests/integration/cassettes/.
JWT = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9"
    ".eyJzdWIiOiJ1cm46YXBwOjdlMGQxODg5ODIyNjQzNzNhNWYwZDQxNWVhMGQyNmUwIn0"
    ".MKohiGUsVT7sq3KKku5mdtAYcCHgS26GQ_hU_qQZdQ"
)
CDN_URL = f"https://images-wixmp-ed30a86b8c4ca887773594c2.wixmp.com/f/a/b.png?token={JWT}"


class TestRedactCoversTheCdnToken:
    """The redactor's docstring said no token reaches a query string.

    True of DA's API, which authenticates by header. Not true of the CDN,
    whose every URL is signed with one — and those are precisely the URLs
    http_bytes fetches.
    """

    def test_a_signed_cdn_url_does_not_survive_redaction(self):
        out = _redact(CDN_URL)
        assert JWT not in out, f"the CDN token is still in the redacted URL: {out}"
        assert "eyJ" not in out, f"a JWT fragment survived: {out}"
        assert "token=<redacted>" in out

    def test_the_rest_of_the_url_is_still_readable(self):
        """A redactor that eats the whole URL makes -v useless for the
        thing it exists for — seeing which request was made.
        """
        out = _redact(CDN_URL)
        assert out.startswith("https://images-wixmp-ed30a86b8c4ca887773594c2.wixmp.com/f/a/b.png")

    @pytest.mark.parametrize(
        ("key", "secret"),
        [
            ("access_token", "AT-SECRET-VALUE"),
            ("client_secret", "CS-SECRET-VALUE"),
            ("code", "CODE-SECRET-VALUE"),
            ("token", "TOKEN-SECRET-VALUE"),
        ],
    )
    def test_every_listed_key_is_stripped(self, key, secret):
        out = _redact(f"https://x/y?{key}={secret}&other=keep")
        assert secret not in out
        assert "other=keep" in out, "a non-secret parameter was collateral damage"

    def test_access_token_is_not_half_matched(self):
        """The ordering control, and the reason `token` goes last.

        Alternation is ordered and re.sub consumes what it matches, so
        `access_token=` must be claimed whole. A pattern that reached
        `token` first would leave `access_` dangling in the output.
        """
        out = _redact("https://x/y?access_token=SECRET")
        assert out == "https://x/y?access_token=<redacted>", out

    def test_a_url_with_no_secret_is_unchanged(self):
        plain = "https://x/y?limit=24&offset=0"
        assert _redact(plain) == plain


class TestHttpBytesTracesItsRequest:
    def test_the_get_line_is_emitted_under_verbose(self, capsys):
        with verbosity("debug"):
            with patch("urllib.request.urlopen") as u:
                u.return_value.__enter__.return_value.read.return_value = b"IMG"
                dacli.http_bytes("https://cdn.example/x.png")

        combined = capsys.readouterr()
        assert "GET https://cdn.example/x.png" in combined.err + combined.out, (
            "http_bytes made a request and said nothing"
        )

    def test_the_get_line_carries_no_credential(self, capsys):
        """The two halves together: it must log, and what it logs must be
        safe. Before the fix this passed for the wrong reason — there was
        no output to leak.
        """
        with verbosity("debug"):
            with patch("urllib.request.urlopen") as u:
                u.return_value.__enter__.return_value.read.return_value = b"IMG"
                dacli.http_bytes(CDN_URL)

        out = capsys.readouterr()
        both = out.err + out.out
        assert "GET https://images-wixmp" in both, "no GET line was emitted"
        assert JWT not in both, "the CDN JWT was printed to the console"

    def test_nothing_is_emitted_at_default_verbosity(self, capsys):
        """The control: this is a debug line, not a new default-output line."""
        with patch("urllib.request.urlopen") as u:
            u.return_value.__enter__.return_value.read.return_value = b"IMG"
            dacli.http_bytes("https://cdn.example/x.png")
        out = capsys.readouterr()
        assert "GET" not in out.out + out.err

    def test_it_traces_on_the_same_stream_as_its_sibling(self, capsys):
        """Deliberately relative, not absolute.

        Which stream debug belongs on is a separate open question — a
        parallel change moves it to stderr so `-v` stops corrupting
        `--json` captures. Pinning "stderr" here would couple this test to
        the merge order of that one. The invariant this change owns is
        that the image path traces *like the JSON path does*, so assert
        exactly that and let the routing fix carry both together.
        """
        with verbosity("debug"):
            with patch("urllib.request.urlopen") as u:
                u.return_value.__enter__.return_value.read.return_value = b"{}"
                dacli.http_json("https://api.example/v1/thing")
            json_out = capsys.readouterr()

            with patch("urllib.request.urlopen") as u:
                u.return_value.__enter__.return_value.read.return_value = b"IMG"
                dacli.http_bytes("https://cdn.example/x.png")
            bytes_out = capsys.readouterr()

        def stream_of(cap) -> str:
            if "GET" in cap.err:
                return "stderr"
            return "stdout" if "GET" in cap.out else "nowhere"

        assert stream_of(bytes_out) == stream_of(json_out) != "nowhere", (
            f"http_json traces to {stream_of(json_out)} but "
            f"http_bytes traces to {stream_of(bytes_out)}"
        )

    def test_each_retry_is_traced_with_the_wait_it_actually_takes(self, capsys):
        """~4.5s of backoff used to happen with nothing on the console.

        The logged wait is the same value passed to sleep — logging a
        freshly-jittered number would print a duration that never
        happened.
        """
        err = urllib.error.HTTPError("https://cdn/x", 503, "boom", {}, None)
        slept: list[float] = []
        with verbosity("debug"):
            with (
                patch("urllib.request.urlopen", side_effect=err),
                patch("time.sleep", side_effect=slept.append),
                pytest.raises(urllib.error.HTTPError),
            ):
                dacli.http_bytes("https://cdn/x", retries=2, backoff=1.0)

        both = capsys.readouterr()
        lines = (both.err + both.out).splitlines()
        retries = [line for line in lines if "retry" in line]
        assert len(retries) == 2, f"2 retries happened, {len(retries)} were traced: {lines}"
        assert "HTTP 503" in retries[0]
        assert "retry 1/2" in retries[0] and "retry 2/2" in retries[1]
        for line, waited in zip(retries, slept, strict=True):
            assert f"{waited:.1f}s" in line, f"logged {line!r} but slept {waited}"

    def test_a_transport_error_retry_is_traced_by_type(self, capsys):
        with verbosity("debug"):
            with (
                patch("urllib.request.urlopen", side_effect=TimeoutError("slow")),
                patch("time.sleep"),
                pytest.raises(TimeoutError),
            ):
                dacli.http_bytes("https://cdn/x", retries=1, backoff=0.0)
        both = capsys.readouterr()
        assert "TimeoutError; retry 1/1" in both.err + both.out

    def test_tuning_arguments_are_keyword_only(self):
        """Both siblings mark them keyword-only; this one did not.

        No caller passed them positionally, so tightening it costs
        nothing and stops the three signatures drifting further apart.
        """
        with pytest.raises(TypeError):
            dacli.http_bytes("https://cdn/x", 2)  # type: ignore[misc]


def _sync_args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "limit": 24,
        "mature": True,
        "time_budget": 540,
        "delay_api": 0.0,
        "delay_image": 0.0,
        "jitter": 0.0,
        "offset": 0,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _one_page(dev: dict) -> object:
    def stub(url: str, *a: object, **kw: object) -> dict:
        if "deviation/metadata" in url:
            return {"metadata": []}
        offsets = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("offset")
        if not offsets:
            return {}
        return {"results": [dev], "has_more": False} if offsets[0] == "0" else {"results": []}

    return stub


class TestAFailedSaveIsReported:
    DEV = {
        "deviationid": "DEV-FAIL",
        "title": "Some Title",
        "author": {"username": "alice"},
        "content": {"src": "https://cdn.example/x.jpg"},
        "is_downloadable": False,
    }

    def _run_with_failing_cdn(self, exc: Exception) -> None:
        stub = _one_page(self.DEV)
        with (
            patch.object(dacli, "http_json", side_effect=stub),
            patch.object(dacli, "authed_http_json", side_effect=stub),
            patch.object(dacli, "http_bytes", side_effect=exc),
            patch("time.sleep"),
        ):
            dacli.cmd_sync_feed(_sync_args())

    def test_the_item_and_the_reason_are_named(self, authed_with_destination, capsys):
        self._run_with_failing_cdn(OSError("disk on fire"))
        both = capsys.readouterr()
        combined = both.out + both.err
        assert "fail=1" in combined, "the summary should still count it"
        assert "Some_Title" in combined, (
            "a deviation failed to save and its title was never printed — "
            f"the only trace was the summary count. Output:\n{combined}"
        )
        assert "OSError" in combined, "the failure reason was not reported"

    def test_it_survives_quiet(self, authed_with_destination, capsys):
        """--quiet is the scheduled-run setting, which is exactly where a
        silent failure does the most damage. warn survives it; info does
        not, which is why this is not just an info line.
        """
        with verbosity("quiet"):
            self._run_with_failing_cdn(OSError("disk on fire"))
        both = capsys.readouterr()
        assert "Some_Title" in both.out + both.err

    def test_a_deviation_with_no_image_is_reported(self, authed_with_destination, capsys):
        dev = dict(self.DEV, content=None)
        dev.pop("content")
        stub = _one_page(dev)
        with (
            patch.object(dacli, "http_json", side_effect=stub),
            patch.object(dacli, "authed_http_json", side_effect=stub),
            patch("time.sleep"),
        ):
            dacli.cmd_sync_feed(_sync_args())
        combined = "".join(capsys.readouterr())
        assert "no downloadable image" in combined, combined

    def test_a_clean_run_prints_no_failure_lines(self, authed_with_destination, capsys):
        """The control: the new branches must not fire on success."""
        stub = _one_page(self.DEV)
        with (
            patch.object(dacli, "http_json", side_effect=stub),
            patch.object(dacli, "authed_http_json", side_effect=stub),
            patch.object(dacli, "http_bytes", return_value=b"IMGDATA"),
            patch("time.sleep"),
        ):
            dacli.cmd_sync_feed(_sync_args())
        combined = "".join(capsys.readouterr())
        assert "  ! " not in combined
        assert "no downloadable image" not in combined
        assert "fail=0" in combined
