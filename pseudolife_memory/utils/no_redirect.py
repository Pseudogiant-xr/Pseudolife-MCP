"""``urlopen`` for requests that carry a credential: redirects are refused.

urllib's default redirect handler re-issues a POST answered 301/302/303 as a
body-less GET to the ``Location`` target and copies every header except
Content-Length/Content-Type to it -- ``Authorization`` included -- whatever
host that target names. A request carrying a bearer key must not be able to
hand it to a host the operator never configured, so such requests open
through :func:`urlopen` here: any redirect surfaces as an ``HTTPError`` with
the redirect's own status code and nothing is sent to the target.

Stdlib only, so importing it adds nothing to the daemon's import cost beyond
``urllib.request`` itself.
"""
from __future__ import annotations

import urllib.error
import urllib.request


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    # 3.10 has no 308 handler (3.11 added it); route 308 through
    # redirect_request too so every redirect status reports the same refusal.
    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"{msg}; redirect to {newurl} refused -- configure the final URL "
            "directly", headers, fp)


def urlopen(req, timeout):
    """``urllib.request.urlopen(req, timeout=timeout)`` minus redirects. The
    opener keeps the stdlib defaults otherwise (proxy environment, TLS
    verification)."""
    return urllib.request.build_opener(NoRedirectHandler).open(
        req, timeout=timeout)
