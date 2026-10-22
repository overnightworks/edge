"""Runs inside the probe container on the back-channel network (backchannel_cell.py, cell BC1): a
wrong password with a forged Forwarded header, then a plain GET of the admin console and of
Keycloak's management port. Reads the token form as JSON from stdin; prints what it saw as one
JSON line."""

import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

KEYCLOAK = "http://keycloak"  # NOSONAR(S5332) the probe network's own address of Keycloak
TOKEN_URL = f"{KEYCLOAK}:8080/realms/hallucinai/protocol/openid-connect/token"

forged = sys.argv[1]
form_json = sys.stdin.read()  # the client secret travels on stdin, never in the process arguments


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
forged_request = urllib.request.Request(TOKEN_URL, data=form, headers=headers)
print(
    json.dumps(
        {
            "address": socket.gethostbyname(socket.gethostname()),
            "token": status(forged_request),
            "admin": status(f"{KEYCLOAK}:8080/admin/"),
            "management": status(f"{KEYCLOAK}:9000/health"),
        }
    )
)
