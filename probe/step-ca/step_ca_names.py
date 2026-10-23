"""The names the step-ca probe (probe.py) and its songmaker route cells (mcp_networks.py)
share: the compose project, the origins and their URLs, and the curl exit codes the cells read."""

PROJECT = "edge-stepca-probe"
CA_URL = "https://step-ca:9000"
CA_HEALTH_URL = f"{CA_URL}/health"
CONTAINER_USER_ID = 1000  # the step user of the smallstep image
CONTAINER_USER = f"{CONTAINER_USER_ID}:{CONTAINER_USER_ID}"
CERTIFICATE_UNVERIFIED = 60  # curl exit codes
COULD_NOT_RESOLVE = 6
READY_SECONDS = 60
ROOT_CERTIFICATE = "root_ca.crt"
ROOT_IN_CLIENT = "/root.crt"
ORIGIN = "hello.mcp.internal"
ORIGIN_URL = f"https://{ORIGIN}/"
ORIGIN_ANSWER = f"Host: {ORIGIN}"  # the backend (whoami) echoes the request's Host header
# The backend speaks plain http by design, inside the compose network.
BACKEND_URL = "http://hello-mcp/"  # NOSONAR(S5332)
# songmaker's MCP origin: compose.yaml's `hal_mcp_songmaker`, moved into the project, and the
# stand-in for its web service (whoami on 8080, overlay compose.probe.yaml).
SONGMAKER_NETWORK = "mcp_songmaker"
SONGMAKER_ORIGIN = "songmaker.mcp.internal"
SONGMAKER_URL = f"https://{SONGMAKER_ORIGIN}/mcp"
STAND_IN = "songmaker-web"
STAND_IN_URL = f"http://{STAND_IN}:8080/"  # NOSONAR(S5332)
