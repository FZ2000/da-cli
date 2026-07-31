"""Exception hierarchy for da-cli.

Extracted verbatim from the single-file module; see ADR 0007.
"""

from __future__ import annotations


# --------------------------------------------------------------------------
# Exception hierarchy
#
# da-cli's historical failure mode was sys.exit(2) with a message string —
# fine for the CLI itself, but opaque to any script that wraps `da` (a
# shell pipeline, a Python wrapper, a CI step). The hierarchy below lets
# callers catch by category; see DacliError's docstring for an example.
#
# Defined here so the cross-process lock's CommandLockedError can subclass
# DacliError naturally.
# --------------------------------------------------------------------------
class DacliError(Exception):
    """Base class for da-cli's own categorised failures.

    Scripts wrapping ``da`` programmatically can catch by category:

    .. code-block:: python

        try:
            token = dacli.access_token(dacli.load_config(), dacli.load_state())
        except dacli.AuthError:
            ...  # DeviantArt rejected the grant — re-auth needed
        except dacli.HttpError:
            ...  # could not reach DA; the grant is probably fine
        except dacli.ConfigError:
            ...  # no client_id, no refresh_token

    That example is now true. It previously appeared here as advice while
    nothing raised any of these, so an ``except dacli.AuthError`` could
    never fire and the real failure arrived as ``SystemExit``. What made
    them worth wiring up was a concrete need: ``access_token`` collapsed
    "DA rejected the grant" and "the network is down" into one
    ``sys.exit(2)``, so ``da auth status`` reported a dropped wifi
    connection as ``revoked`` and sent people to re-authenticate for
    nothing. Two exception types tell those apart; one exit code cannot.

    **The CLI contract is unchanged.** ``main()`` catches ``DacliError``
    and exits 2 with the message, so the shell still sees the codes
    documented in ``docs/reference/exit-codes.md``. The exceptions are for
    in-process callers, and for the places inside da-cli that need to know
    *why* something failed.
    """


class ConfigError(DacliError):
    """Missing or malformed configuration.

    Raised by ``access_token`` when there is no stored refresh_token or no
    configured client_id — both of which need the user to do something
    before any request can be made.
    """


class AuthError(DacliError):
    """DeviantArt rejected the credentials.

    Raised by ``access_token`` on a 4xx from the token endpoint, and by
    ``_cmd_auth_status``'s caller when ``/placebo`` refuses the access
    token. Means the grant is gone: the fix is ``da auth``.
    """


class HttpError(DacliError):
    """The request could not be completed, so nothing was learned.

    Raised by ``access_token`` for an unreachable DeviantArt or a 5xx from
    its auth server. Deliberately NOT an ``AuthError``: the credential is
    probably fine, and telling someone to re-authenticate because their
    network dropped costs them a browser round trip for nothing.
    """


class SyncError(DacliError):
    """Sync-loop failure that aborted the current walk.

    Reserved; nothing raises this yet. The sync walks report per-item
    failures through their status tuples and stop_reason instead.
    """
