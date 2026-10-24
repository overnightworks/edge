"""The mail account of `home check` and the msmtp command line that sends through it.

The account lives in `~/.secrets/smtp/account.toml`, the password only in `~/.secrets/smtp/password`: msmtp, run as the
operator, reads the password at send time through `--passwordeval`, so hal never opens it. TLS is always on and the
server certificate is always checked against the system trust store; there is no option to change either.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from home_tools.tidy_home import Layout

ACCOUNT_KEYS = frozenset({"host", "port", "user", "to"})
IMPLICIT_TLS_PORT = 465
SEND_TIMEOUT_SECONDS = 30
HOSTNAME = re.compile(r"(?!-)[A-Za-z0-9.-]{1,253}")
ADDRESS = re.compile(r"[^\s,<>\"'@\x00-\x1f\x7f]+@[^\s,<>\"'@\x00-\x1f\x7f]+")
# The message reaches `sh` as a positional parameter, never as text of the script; msmtp's own argv follows it.
PIPE_MESSAGE_TO_COMMAND = 'printf %s "$1" | { shift; exec "$@"; }'


class AccountInvalid(ValueError):
    """The account file is present but unusable; the text names the key and the rule, never a value."""


@dataclass(frozen=True)
class MailAccount:
    host: str
    port: int
    user: str
    to: str


def account_file(layout: Layout) -> Path:
    return layout.secrets_dir / "smtp" / "account.toml"


def password_file(secrets_dir: Path) -> Path:
    return secrets_dir / "smtp" / "password"


def _checked_text(raw: dict[str, object], key: str, pattern: re.Pattern[str], rule: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise AccountInvalid(f"account.toml: {key} {rule}")
    return value


def _checked_port(raw: dict[str, object]) -> int:
    port = raw["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise AccountInvalid("account.toml: port muss eine Zahl von 1 bis 65535 sein")
    return port


def load_account(layout: Layout) -> MailAccount | None:
    """None while the file is absent (mail is not set up); a present file that is unusable raises AccountInvalid."""
    path = account_file(layout)
    if not path.exists():
        return None
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as error:
        raise AccountInvalid("account.toml: kein lesbares TOML") from error
    missing = ACCOUNT_KEYS - raw.keys()
    if missing:
        raise AccountInvalid(f"account.toml: Schlüssel fehlt: {', '.join(sorted(missing))}")
    if raw.keys() - ACCOUNT_KEYS:
        raise AccountInvalid("account.toml: unbekannter Schlüssel")
    return MailAccount(
        host=_checked_text(raw, "host", HOSTNAME, "muss ein Hostname sein (Buchstaben, Ziffern, Punkt, Minus; kein Minus am Anfang)"),
        port=_checked_port(raw),
        user=_checked_text(raw, "user", ADDRESS, "muss genau eine Adresse sein"),
        to=_checked_text(raw, "to", ADDRESS, "muss genau eine Adresse sein"),
    )


def compose(account: MailAccount, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = account.user
    message["To"] = account.to
    message["Subject"] = subject
    message.set_content(body)
    return message


def sendmail_command(account: MailAccount, secrets_dir: Path, message: EmailMessage) -> list[str]:
    """Everything is on the command line: `--host` makes msmtp read no config file, and no option loosens TLS."""
    starttls = "off" if account.port == IMPLICIT_TLS_PORT else "on"
    password_command = f"cat -- {shlex.quote(str(password_file(secrets_dir)))}"
    return [
        "sh", "-c", PIPE_MESSAGE_TO_COMMAND, "sh", message.as_string(),
        "msmtp",
        f"--host={account.host}",
        f"--port={account.port}",
        "--auth=on",
        f"--user={account.user}",
        f"--from={account.user}",
        "--tls=on",
        f"--tls-starttls={starttls}",
        f"--timeout={SEND_TIMEOUT_SECONDS}",
        f"--passwordeval={password_command}",
        "--",
        account.to,
    ]
