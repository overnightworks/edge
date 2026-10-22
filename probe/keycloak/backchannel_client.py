"""Runs inside the probe container on the back-channel network (probe.py, cell BC1): a wrong
password with a forged Forwarded header, then a plain GET of the admin console and of Keycloak's
management port. Prints what it saw as one JSON line."""

import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

token_url, form_json, forged = sys.argv[1:4]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments, **keywords):
        return None


opener = urllib.request.build_opener(NoRedirect)


def status(request):
    try:
        with opener.open(request, timeout=10) as reply:
            return str(reply.status)
    except urllib.error.HTTPError as error:
        return str(error.code)
    except OSError as error:
        return "no answer: " + type(error).__name__


form = urllib.parse.urlencode(json.loads(form_json)).encode()
headers = {"Forwarded": "for=" + forged}
forged_request = urllib.request.Request(token_url, data=form, headers=headers)
print(
    json.dumps(
        {
            "address": socket.gethostbyname(socket.gethostname()),
            "token": status(forged_request),
            "admin": status("http://keycloak:8080/admin/"),
            "management": status("http://keycloak:9000/health"),
        }
    )
)
