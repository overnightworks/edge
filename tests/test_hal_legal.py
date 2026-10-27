"""`hal legal KEY [DIR]` (#442): the checker and the assembly of `hal apply`, run on one app's files
over a temporary copy of the frame, the general part and the catalog."""

import shutil
from pathlib import Path

import pytest

from edge import compose, legal_section
from hal import cli

ROOT = Path(__file__).resolve().parents[1]
KEY = "hopin"
PRIVACY = "legal/datenschutz.html"
TERMS = "legal/nutzungsbedingungen.html"
ZONE = "hallucinai.de"
VALID = '<h2 id="hopin">HopIn</h2>\n<p>Wir sehen Ihren Namen.</p>\n'
VALID_TERMS = '<h2 id="hopin">HopIn</h2>\n<p>Nutzen Sie es fair.</p>\n'


@pytest.fixture
def checkout(tmp_path, monkeypatch) -> Path:
    """A copy of edge's catalog, frame and general part, as `compose.REPOSITORY`."""
    repository = tmp_path / "edge"
    (repository / "legal/sections").mkdir(parents=True)
    shutil.copy(ROOT / "fleet.yaml", repository)
    shutil.copytree(ROOT / "legal/frame", repository / "legal/frame")
    for unit in ("allgemein", "keycloak", "agent-service"):
        shutil.copytree(ROOT / "legal/sections" / unit, repository / "legal/sections" / unit)
    monkeypatch.setattr(compose, "REPOSITORY", repository)
    return repository


@pytest.fixture
def app(tmp_path) -> Path:
    directory = tmp_path / "app"
    (directory / "legal").mkdir(parents=True)
    return directory


def put(app: Path, relative: str, text: str | bytes = VALID) -> None:
    data = text if isinstance(text, bytes) else text.encode()
    (app / relative).write_bytes(data)


def run(capsys, *arguments: str | Path) -> tuple[int, str, str]:
    code = cli.main(["legal", *map(str, arguments)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def snapshot(*roots: Path) -> dict[str, bytes | None]:
    return {
        str(path): path.read_bytes() if path.is_file() else None
        for root in roots
        for path in sorted(root.rglob("*"))
    }


def test_both_files_are_valid_and_named(checkout, app, capsys):
    """L1."""
    put(app, PRIVACY)
    put(app, TERMS, VALID_TERMS)

    code, out, _ = run(capsys, KEY, app)

    assert (code, out) == (0, f"legal: {KEY}: {PRIVACY}, {TERMS} are valid\n")


def test_the_terms_are_optional(checkout, app, capsys):
    """L1."""
    put(app, PRIVACY)

    code, out, _ = run(capsys, KEY, app)

    assert (code, out) == (0, f"legal: {KEY}: {PRIVACY} are valid\n")


@pytest.mark.parametrize("terms", [False, True])
def test_a_directory_without_the_privacy_file_is_refused(checkout, app, capsys, terms):
    """L1."""
    if terms:
        put(app, TERMS, VALID_TERMS)

    code, _, err = run(capsys, KEY, app)

    assert code == 1
    assert err.startswith(f"error: legal: {PRIVACY} is missing; every app needs a privacy section")


def test_the_directory_defaults_to_the_current_one(checkout, app, capsys, monkeypatch):
    put(app, PRIVACY)
    monkeypatch.chdir(app)

    assert run(capsys, KEY)[0] == 0


def test_a_missing_directory_is_a_failure(checkout, tmp_path, capsys):
    code, _, err = run(capsys, KEY, tmp_path / "absent")

    assert code == 3
    assert "is not a directory" in err


@pytest.mark.parametrize("name", [PRIVACY, TERMS])
def test_a_symlink_or_a_directory_in_place_of_a_file_is_refused(checkout, app, capsys, name):
    put(app, PRIVACY)
    put(app, "target.html")
    (app / name).unlink(missing_ok=True)
    (app / name).symlink_to(app / "target.html")

    code, _, err = run(capsys, KEY, app)

    assert (code, err) == (1, f"error: legal: {name} is not a regular file\n")

    (app / name).unlink()
    (app / name).mkdir()
    assert run(capsys, KEY, app)[1:] == ("", f"error: legal: {name} is not a regular file\n")


@pytest.mark.parametrize(
    "text",
    [
        '<h2 id="hopin">HopIn</h2>\n<script>x</script>\n',
        '<h2 id="other">HopIn</h2>\n<p>a</p>\n',
        '<h2 id="hopin">HopIn</h2>\n<p>a<br/></p>\n',
        '<h2 id="hopin">HopIn</h2>\n<ul>\t<li>a</li></ul>\n',
        '<h2 id="hopin">HopIn</h2>\n<p><a href="http://example.org/">a</a></p>\n',
    ],
)
def test_a_refused_file_gets_the_line_the_parser_gives(checkout, app, capsys, text):
    """L7, L14: the line is the section parser's, the path is relative to the app."""
    put(app, PRIVACY, text)
    with pytest.raises(legal_section.LegalError) as parsed:
        legal_section.parse_fragment(PRIVACY, text.encode(), KEY, ZONE)

    code, out, err = run(capsys, KEY, app)

    assert (code, out, err) == (1, "", f"error: {parsed.value}\n")


def test_a_character_reference_is_refused_with_the_remedy_and_without_the_text(
    checkout, app, capsys
):
    """L14."""
    put(app, PRIVACY, '<h2 id="hopin">HopIn</h2>\n<p>a</p>\n<p>x</p>\n<p>"&quot;"</p>\n')

    _, _, err = run(capsys, KEY, app)

    assert f"{PRIVACY}: line 4: not in canonical form" in err
    assert err.endswith("write the character itself, not a reference such as &quot; or &nbsp;\n")
    assert err.count("&quot;") == 1


def test_an_id_owned_by_another_block_is_refused_naming_both(checkout, app, capsys):
    """The `anmeldung` anchor is keycloak's block."""
    put(app, PRIVACY, '<h2 id="anmeldung">A</h2>\n<p>a</p>\n')

    code, _, err = run(capsys, "anmeldung", app)

    assert code == 1
    assert PRIVACY in err
    assert "legal/sections/keycloak/datenschutz.html" in err


def test_a_link_to_a_block_that_does_not_exist_is_refused(checkout, app, capsys):
    put(app, PRIVACY, '<h2 id="hopin">HopIn</h2>\n<p><a href="#nowhere">a</a></p>\n')

    code, _, err = run(capsys, KEY, app)

    assert code == 1
    assert "nowhere" in err


def test_a_held_copy_for_the_same_key_does_not_clash(checkout, app, capsys):
    held = checkout / "legal/sections" / KEY
    held.mkdir()
    (held / "datenschutz.html").write_text(VALID)
    put(app, PRIVACY)

    assert run(capsys, KEY, app)[0] == 0


def test_a_run_writes_nothing(checkout, app, capsys):
    put(app, PRIVACY)
    before = snapshot(checkout, app)

    run(capsys, KEY, app)

    assert snapshot(checkout, app) == before
    assert not (checkout / "legal/built").exists()
