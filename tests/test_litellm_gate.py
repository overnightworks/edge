"""The decision of LiteLLM's custom auth hook (agent-service/litellm/gate.py, #768): which request
the gateway lets through to a model, case by case and without LiteLLM. The probe
probe/agent-service/litellm.py proves only that LiteLLM calls the hook."""

import json
import secrets
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from support.script_module import loaded_script

ROOT = Path(__file__).resolve().parents[1]
PATH = "/v1/chat/completions"
GOOD_BODY = {"model": "anthropic/opus", "messages": [], "tools": []}


@pytest.fixture(scope="module")
def gate() -> Iterator[ModuleType]:
    yield from loaded_script("litellm_gate", ROOT / "agent-service" / "litellm" / "gate.py")


@pytest.fixture
def key() -> str:
    return secrets.token_urlsafe(24)


@pytest.fixture
def credential_file(tmp_path, key) -> Path:
    path = tmp_path / "credential.json"
    path.write_text(json.dumps({"shared_key": key}))
    return path


def request(gate, credential_file, key, **changes):
    arguments = {
        "method": "POST",
        "path": PATH,
        "query": "",
        "authorization": f"Bearer {key}",
        "content_types": ["application/json"],
        "header_names": ["host", "authorization", "content-type", "content-length"],
        "body": json.dumps(GOOD_BODY).encode(),
        "credential_file": credential_file,
    }
    return gate.decide(**{**arguments, **changes})


def test_the_one_route_with_the_shared_key_and_the_three_body_keys_passes(
    gate, credential_file, key
):
    assert request(gate, credential_file, key).allowed


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "PUT", "DELETE", "PATCH", "post"])
def test_any_other_method_is_refused(gate, credential_file, key, method):
    assert not request(gate, credential_file, key, method=method).allowed


@pytest.mark.parametrize(
    "path",
    [
        "/chat/completions",
        "/v1/chat/completions/",
        "//v1/chat/completions",
        "/V1/CHAT/COMPLETIONS",
        "/v1/chat/completions/x",
        "/v1/chat%2Fcompletions",
        "/key/generate",
        "",
    ],
)
def test_any_other_path_is_refused(gate, credential_file, key, path):
    assert not request(gate, credential_file, key, path=path).allowed


@pytest.mark.parametrize("query", ["model=x", "?", "a", "model=anthropic%2Fopus"])
def test_a_query_string_is_refused(gate, credential_file, key, query):
    assert not request(gate, credential_file, key, query=query).allowed


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer", "Bearer ", "Basic abc", "bearer {key}", "Bearer {key} ", "Bearer  {key}"],
)
def test_a_missing_or_malformed_bearer_is_refused(gate, credential_file, key, authorization):
    header = None if authorization is None else authorization.replace("{key}", key)
    assert not request(gate, credential_file, key, authorization=header).allowed


@pytest.mark.parametrize("wrong", ["{key}x", "x{key}", "swapcase", "other"])
def test_a_wrong_bearer_is_refused(gate, credential_file, key, wrong):
    token = key.swapcase() if wrong == "swapcase" else wrong.replace("{key}", key)
    assert not request(gate, credential_file, key, authorization=f"Bearer {token}").allowed


@pytest.mark.parametrize(
    "content_types",
    [
        [],
        ["application/json", "application/json"],
        ["application/x-www-form-urlencoded"],
        ["multipart/form-data; boundary=x"],
        ["text/plain"],
        ["application/json; charset=utf-8"],
        ["Application/JSON"],
        [" application/json"],
        ["application/json, application/x-www-form-urlencoded"],
    ],
)
def test_a_valid_json_body_not_sent_as_exactly_one_application_json_is_refused(
    gate, credential_file, key, content_types
):
    assert not request(gate, credential_file, key, content_types=content_types).allowed


@pytest.mark.parametrize(
    "header",
    [
        "x-litellm-num-retries",
        "x-litellm-timeout",
        "x-litellm-stream-timeout",
        "X-LiteLLM-Num-Retries",
        "X-LITELLM-TIMEOUT",
        "x-LiteLLM-stream-timeout",
        "x-litellm-api-key",
    ],
)
def test_any_x_litellm_header_in_any_letter_case_is_refused(gate, credential_file, key, header):
    names = ["host", "authorization", "content-type", header]
    assert not request(gate, credential_file, key, header_names=names).allowed


@pytest.mark.parametrize(
    "extra",
    [
        "api_base",
        "base_url",
        "api_key",
        "fallbacks",
        "num_retries",
        "mock_response",
        "metadata",
        "user",
        "model_list",
        "stream",
    ],
)
def test_a_body_with_an_extra_key_is_refused(gate, credential_file, key, extra):
    body = json.dumps({**GOOD_BODY, extra: "x"}).encode()
    assert not request(gate, credential_file, key, body=body).allowed


@pytest.mark.parametrize("missing", ["model", "messages", "tools"])
def test_a_body_without_one_of_the_three_keys_is_refused(gate, credential_file, key, missing):
    body = json.dumps({k: v for k, v in GOOD_BODY.items() if k != missing}).encode()
    assert not request(gate, credential_file, key, body=body).allowed


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{",
        b"[]",
        b'"x"',
        b"null",
        b"\xff",
        b"[" * 100_000,
        json.dumps(GOOD_BODY).encode("utf-16"),
    ],
)
def test_a_body_that_is_no_json_object_is_refused(gate, credential_file, key, body):
    assert not request(gate, credential_file, key, body=body).allowed


@pytest.mark.parametrize(
    "contents",
    [
        None,
        "",
        "not json",
        "[]",
        "{}",
        '{"shared_key": ""}',
        '{"shared_key": 1}',
        '{"shared_key": "{key}", "other": "x"}',
        '{"app_keys": {"songmaker": "{key}"}}',
        '{"shared_key": "{key}", "app_keys": []}',
        '{"shared_key": "{key}", "app_keys": "x"}',
        '{"shared_key": "{key}", "app_keys": null}',
        '{"shared_key": "{key}", "app_keys": {"songmaker": ""}}',
        '{"shared_key": "{key}", "app_keys": {"songmaker": {"nested": "x"}}}',
        '{"shared_key": "{key}", "shared_key": "{key}"}',
        '{"shared_key": "{key}", "app_keys": {"songmaker": "x", "songmaker": "y"}}',
        '{"shared_key": " {key}"}',
        '{"shared_key": "bad\\nkey"}',
        '{"shared_key": "key=middle"}',
    ],
)
def test_a_missing_or_malformed_credential_refuses_everything(gate, tmp_path, key, contents):
    path = tmp_path / "credential.json"
    if contents is not None:
        path.write_text(contents.replace("{key}", key))
    assert not request(gate, path, key).allowed


def test_a_credential_that_cannot_be_read_refuses_everything(gate, tmp_path, key):
    assert not request(gate, tmp_path, key).allowed


def test_a_credential_with_app_keys_beside_the_shared_key_still_passes_the_shared_key(
    gate, tmp_path, key
):
    path = tmp_path / "credential.json"
    path.write_text(json.dumps({"shared_key": key, "app_keys": {"songmaker": "x"}}))
    assert request(gate, path, key).allowed


def test_a_refusal_names_a_reason_that_holds_no_key(gate, credential_file, key):
    verdict = request(gate, credential_file, key, authorization=f"Bearer {key}x")
    assert verdict.reason and key not in verdict.reason
