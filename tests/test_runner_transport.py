"""EE transport content-coding negotiation and initialization error mapping.

Only the urllib opener (the HTTP server boundary) and credential issuance are
faked. EETransport, _read_url, urllib Request/HTTPError, the installed Google
API JsonModel, google-auth refresh and ee.Initialize with its algorithm loading
run for real against the installed EE test fixtures. No socket is opened.
"""

from __future__ import annotations

import copy
import gzip
import json
from http.client import HTTPMessage
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
import zlib

import ee
import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.model import JsonModel

from geecomposer import runner_cloud
from geecomposer.recipe import RunnerError, canonical, validate_policy
from geecomposer.runner_cloud import MAX_STATE, EarthEngine, EETransport, attached_identity
from geecomposer.runner_personal import PersonalEarthEngine
from runner_fakes import Clock, policy_value

pytestmark = pytest.mark.usefixtures("runner_offline")

EE = "https://earthengine.googleapis.com"
TOKEN = "https://oauth2.googleapis.com/token"
PROJECT = "offline-tests"
SECRET = "SECRET-7f3a"
FIXTURES = Path(ee.__file__).resolve().parent / "tests"


def message(pairs):
    headers = HTTPMessage()
    for key, value in pairs:
        headers[key] = value  # HTTPMessage keeps duplicate headers
    return headers


class Body(BytesIO):
    """Response body that records how much was read and whether it was closed."""

    consumed = 0

    def read1(self, size=-1):
        chunk = super().read1(size)
        self.consumed += len(chunk)
        return chunk


class Reply:
    """Successful urllib response shape (code, headers, read1, context manager)."""

    def __init__(self, code, body, pairs):
        self.code, self.headers, self.fp = code, message(pairs), body

    def read1(self, size):
        return self.fp.read1(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fp.close()
        return False


class Server:
    """Fake HTTP boundary behind build_opener; real urllib objects cross it.

    negotiate=True: gzip-encodes every body unless the request asked for identity.
    negotiate=False: always answers with the given codings (a misbehaving server).
    """

    def __init__(self, routes, *, negotiate=True, codings=()):
        self.routes, self.negotiate, self.codings = routes, negotiate, list(codings)
        self.requests, self.bodies = [], []

    def open(self, request, timeout):
        assert timeout <= 10
        self.requests.append(request)
        url = urlsplit(request.full_url)
        code, payload = self.routes[(request.get_method(), url.netloc, url.path)]
        pairs = [("Content-Type", "application/json")]
        if self.negotiate:
            if request.get_header("Accept-encoding") != "identity":
                payload = gzip.compress(payload)
                pairs.append(("Content-Encoding", "gzip"))
        else:
            pairs += [("Content-Encoding", c) for c in self.codings]
            if self.codings == ["deflate"]:
                payload = zlib.compress(payload)
            elif self.codings and self.codings[0].strip().lower() not in ("identity", ""):
                payload = gzip.compress(payload)
        body = Body(payload)
        self.bodies.append(body)
        if code >= 400:  # mirror urllib's default HTTPErrorProcessor
            raise HTTPError(request.full_url, code, "remote", message(pairs), body)
        return Reply(code, body, pairs)


@pytest.fixture
def serve(monkeypatch):
    def install(routes, **kwargs):
        server = Server(routes, **kwargs)
        monkeypatch.setattr(runner_cloud, "build_opener", lambda *handlers: server)
        return server

    return install


def api_headers():
    """Headers the installed Google API JsonModel attaches to EE API calls."""
    headers, _, _, _ = JsonModel().request({}, {}, {}, None)
    return headers


ALGORITHMS = ("GET", "earthengine.googleapis.com", f"/v1/projects/{PROJECT}/algorithms")
# The installed EE discovery fixture describes v1alpha; real ee.Initialize follows it.
LISTING = ("GET", "earthengine.googleapis.com", f"/v1alpha/projects/{PROJECT}/algorithms")
PAYLOAD = {"algorithms": [], "note": "plain"}


def test_installed_json_model_cannot_decode_gzip_but_decodes_plain():
    """Reproduction of the recorded failure mode: compressed bytes reach the model."""
    model = JsonModel()
    response = runner_cloud._Response(200, {"content-type": "application/json"})
    with pytest.raises(UnicodeDecodeError):
        model.response(response, gzip.compress(canonical(PAYLOAD)))
    assert model.response(response, canonical(PAYLOAD)) == PAYLOAD
    assert api_headers()["accept-encoding"] == "gzip, deflate"


def test_api_call_negotiates_identity_and_real_model_decodes(serve):
    server = serve({ALGORITHMS: (200, canonical(PAYLOAD))})
    sent = api_headers()
    before = copy.deepcopy(sent)
    response, data = EETransport(Clock().budget()).request(
        f"{EE}/v1/projects/{PROJECT}/algorithms", headers=sent
    )
    assert JsonModel().response(response, data) == PAYLOAD
    assert sent == before
    assert server.requests[0].get_header("Accept-encoding") == "identity"


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {},
        {"accept-encoding": "gzip, deflate"},
        {"Accept-Encoding": "gzip", "ACCEPT-ENCODING": "br", "accept-encoding": "deflate"},
        {"aCcEpT-eNcOdInG": "*", "authorization": "Bearer " + SECRET, "content-type": "x/y"},
    ],
)
def test_every_accept_encoding_casing_is_replaced_without_mutating_input(serve, headers):
    server = serve({("POST", "earthengine.googleapis.com", "/v1/x"): (200, b"{}")})
    before = copy.deepcopy(headers)
    EETransport(Clock().budget()).request(f"{EE}/v1/x", "POST", b"{}", headers)
    assert headers == before
    items = server.requests[0].header_items()
    assert [v for k, v in items if k.lower() == "accept-encoding"] == ["identity"]
    others = {k.lower(): v for k, v in items if k.lower() != "accept-encoding"}
    expected = {k.lower(): v for k, v in (headers or {}).items() if k.lower() != "accept-encoding"}
    assert others == expected


@pytest.mark.parametrize(
    "codings, accepted",
    [
        ((), True),
        (("identity",), True),
        ((" IDENTITY ",), True),
        (("Identity",), True),
        (("gzip",), False),
        (("deflate",), False),
        (("br",), False),
        (("x-unknown",), False),
        (("gzip, identity",), False),
        (("identity, identity",), False),
        (("identity", "identity"), False),
        (("",), False),
        (("   ",), False),
        (("identity;q=1",), False),
    ],
)
def test_response_codings_other_than_identity_are_refused_unread(serve, codings, accepted):
    server = serve({ALGORITHMS: (200, canonical(PAYLOAD))}, negotiate=False, codings=codings)
    transport = EETransport(Clock().budget())
    if accepted:
        _, data = transport.request(f"{EE}/v1/projects/{PROJECT}/algorithms")
        assert json.loads(data) == PAYLOAD
    else:
        with pytest.raises(RunnerError) as exc:
            transport.request(f"{EE}/v1/projects/{PROJECT}/algorithms")
        assert (exc.value.status, str(exc.value)) == (
            "uncertain",
            "unsupported EE response encoding",
        )
        assert server.bodies[0].consumed == 0  # compressed bytes never reach the client
    assert server.bodies[0].closed
    assert len(server.requests) == 1  # no retry


def test_malformed_plain_utf8_reaches_real_json_decoding(serve):
    """The installed JsonModel returns unparsable UTF-8 text unchanged (no dict)."""
    serve({ALGORITHMS: (200, b'{"algorithms": [')})
    response, data = EETransport(Clock().budget()).request(f"{EE}/v1/projects/{PROJECT}/algorithms")
    assert data == b'{"algorithms": ['
    assert JsonModel().response(response, data) == '{"algorithms": ['


@pytest.mark.parametrize("code", [401, 403])
@pytest.mark.parametrize("codings", [(), ("gzip",)])
def test_permission_status_stays_auth_even_when_compressed(serve, code, codings):
    server = serve(
        {ALGORITHMS: (code, b'{"error": "' + SECRET.encode() + b'"}')},
        negotiate=False,
        codings=codings,
    )
    with pytest.raises(RunnerError) as exc:
        EETransport(Clock().budget()).request(f"{EE}/v1/projects/{PROJECT}/algorithms")
    assert (exc.value.status, str(exc.value)) == ("auth", "EE permission denied")
    assert server.bodies[0].closed and len(server.requests) == 1


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
@pytest.mark.parametrize("codings", [(), ("gzip",)])
def test_transient_status_keeps_existing_behavior(serve, code, codings):
    server = serve({ALGORITHMS: (code, b"{}")}, negotiate=False, codings=codings)
    with pytest.raises(TimeoutError):
        EETransport(Clock().budget()).request(f"{EE}/v1/projects/{PROJECT}/algorithms")
    assert server.bodies[0].closed and len(server.requests) == 1


def test_other_status_with_coding_is_refused_but_plain_is_returned(serve):
    serve({ALGORITHMS: (404, b"{}")}, negotiate=False, codings=("gzip",))
    with pytest.raises(RunnerError, match="unsupported EE response encoding"):
        EETransport(Clock().budget()).request(f"{EE}/v1/projects/{PROJECT}/algorithms")
    serve({ALGORITHMS: (404, b"{}")}, negotiate=False)
    response, data = EETransport(Clock().budget()).request(f"{EE}/v1/projects/{PROJECT}/algorithms")
    assert (response.status, data) == (404, b"{}")


def test_plain_body_size_limit_and_closure_are_preserved(serve):
    limit = 16 * MAX_STATE
    server = serve({ALGORITHMS: (200, b"x" * (limit + 5))}, negotiate=False)
    with pytest.raises(RunnerError, match="size limit") as exc:
        EETransport(Clock().budget()).request(f"{EE}/v1/projects/{PROJECT}/algorithms")
    assert exc.value.status == "uncertain"
    assert server.bodies[0].consumed == limit + 1 and server.bodies[0].closed


def test_deadline_precedes_any_request(serve):
    clock = Clock()
    budget = clock.budget(120)
    server = serve({ALGORITHMS: (200, b"{}")})
    clock.sleep(61)
    with pytest.raises(RunnerError) as exc:
        EETransport(budget).request(f"{EE}/v1/projects/{PROJECT}/algorithms")
    assert exc.value.status == "deadline" and server.requests == []


def test_operation_discovery_negotiates_identity_and_refuses_codings(serve):
    marker = "gc-" + "a" * 64
    route = ("GET", "earthengine.googleapis.com", "/v1/projects/test-project/operations")
    engine = EarthEngine(Clock().budget())
    engine.project = "test-project"
    engine.credentials = UserCredentials("token")
    server = serve({route: (200, canonical({"operations": []}))})
    assert engine.discover(marker) is None
    assert server.requests[0].get_header("Accept-encoding") == "identity"
    serve({route: (200, canonical({"operations": []}))}, negotiate=False, codings=("gzip",))
    with pytest.raises(RunnerError) as exc:
        engine.discover(marker)
    assert (exc.value.status, str(exc.value)) == ("uncertain", "unsupported EE response encoding")


# ---------------------------------------------------------------- initialization


class MetadataFake:
    email = "worker@test-project.iam.gserviceaccount.com"

    def get(self, resource):
        if not resource:
            return {"email": self.email}
        return {"access_token": "ATTACHED", "expires_in": 3600, "token_type": "Bearer"}


def personal_engine():
    return PersonalEarthEngine(Clock().budget()), UserCredentials(
        None, refresh_token=SECRET, token_uri=TOKEN, client_id="cid", client_secret="cs"
    )


def institutional_engine():
    credentials = attached_identity(
        validate_policy(policy_value()), Clock().budget(), metadata=MetadataFake(), environ={}
    )
    return EarthEngine(Clock().budget()), credentials


ENGINES = {"personal": personal_engine, "institutional": institutional_engine}


def raising(error):
    def initialize(**kwargs):
        raise error

    return initialize


@pytest.mark.parametrize("kind", sorted(ENGINES))
@pytest.mark.parametrize(
    "error, status, text",
    [
        (
            UnicodeDecodeError("utf-8", SECRET.encode(), 0, 1, SECRET),
            "uncertain",
            "EE initialization response unavailable",
        ),
        (
            json.JSONDecodeError(SECRET, SECRET, 0),
            "uncertain",
            "EE initialization response unavailable",
        ),
        (ValueError(SECRET), "uncertain", "EE initialization response unavailable"),
        (TimeoutError(SECRET), "uncertain", "EE initialization response unavailable"),
        (ee.EEException(SECRET), "uncertain", "EE initialization response unavailable"),
        # Exception text alone is never refusal evidence (M004-S08-F1).
        (
            RefreshError("invalid_grant " + SECRET),
            "uncertain",
            "EE initialization response unavailable",
        ),
        (
            RunnerError("deadline", "application deadline reached"),
            "deadline",
            "application deadline reached",
        ),
        (RunnerError("auth", "EE permission denied"), "auth", "EE permission denied"),
        (
            RunnerError("uncertain", "unsupported EE response encoding"),
            "uncertain",
            "unsupported EE response encoding",
        ),
    ],
)
def test_initialize_maps_typed_errors_without_disclosure(monkeypatch, kind, error, status, text):
    monkeypatch.setattr(ee, "Initialize", raising(error))
    engine, credentials = ENGINES[kind]()
    with pytest.raises(RunnerError) as exc:
        engine.initialize(credentials, PROJECT)
    assert (exc.value.status, str(exc.value)) == (status, text)
    assert SECRET not in str(exc.value) and exc.value.__cause__ is None
    if not isinstance(error, RunnerError):
        assert exc.value.__suppress_context__ is True


@pytest.fixture
def ee_state(monkeypatch):
    monkeypatch.setattr(ee.deprecation, "_FetchDataCatalogStac", lambda: {})
    ee.Reset()
    yield
    ee.Reset()


def ee_routes(algorithms=None):
    discovery = (FIXTURES / "cloud_api_discovery_document.json").read_bytes()
    listed = (FIXTURES / "algorithms.json").read_bytes() if algorithms is None else algorithms
    token = {"access_token": "SYNTHETIC", "expires_in": 3600, "token_type": "Bearer"}
    return {
        ("POST", "oauth2.googleapis.com", "/token"): (200, canonical(token)),
        ("GET", "earthengine.googleapis.com", "/$discovery/rest"): (200, discovery),
        LISTING: (200, listed),
    }


@pytest.mark.parametrize("kind", sorted(ENGINES))
def test_real_initialize_loads_algorithms_through_negotiating_transport(serve, ee_state, kind):
    routes = ee_routes()
    server = serve(routes)
    engine, credentials = ENGINES[kind]()
    engine.initialize(credentials, PROJECT)
    assert ee.ApiFunction.lookup("Image.add").getSignature()["name"] == "Image.add"
    assert all(r.get_header("Accept-encoding") == "identity" for r in server.requests)
    paths = [urlsplit(r.full_url).path for r in server.requests]
    assert paths.count(LISTING[2]) == 1
    assert ("/token" in paths) is (kind == "personal")


@pytest.mark.parametrize("kind", sorted(ENGINES))
def test_real_initialize_refuses_server_ignoring_identity(serve, ee_state, kind):
    routes = ee_routes()
    serve(routes, negotiate=False, codings=("gzip",))
    engine, credentials = ENGINES[kind]()
    with pytest.raises(RunnerError) as exc:
        engine.initialize(credentials, PROJECT)
    assert exc.value.status == "uncertain"


def test_real_refresh_refusal_is_auth_without_disclosure(serve, ee_state):
    routes = ee_routes()
    routes[("POST", "oauth2.googleapis.com", "/token")] = (
        400,
        canonical({"error": "invalid_grant", "error_description": SECRET}),
    )
    server = serve(routes)
    engine, credentials = personal_engine()
    with pytest.raises(RunnerError) as exc:
        engine.initialize(credentials, PROJECT)
    assert (exc.value.status, str(exc.value)) == ("auth", "EE credential refresh refused")
    assert SECRET not in str(exc.value) and exc.value.__suppress_context__ is True
    assert [urlsplit(r.full_url).path for r in server.requests] == ["/token"]


@pytest.mark.parametrize("kind", sorted(ENGINES))
def test_real_initialize_transient_algorithms_failure_is_not_auth(serve, ee_state, kind):
    routes = ee_routes()
    routes[LISTING] = (503, b"{}")
    server = serve(routes)
    engine, credentials = ENGINES[kind]()
    with pytest.raises(RunnerError) as exc:
        engine.initialize(credentials, PROJECT)
    assert (exc.value.status, str(exc.value)) == (
        "uncertain",
        "EE initialization response unavailable",
    )
    assert [urlsplit(r.full_url).path for r in server.requests].count(LISTING[2]) == 1


@pytest.mark.parametrize("kind", sorted(ENGINES))
def test_real_initialize_malformed_algorithms_is_not_auth(serve, ee_state, kind):
    routes = ee_routes(b'{"algorithms": [')
    serve(routes)
    engine, credentials = ENGINES[kind]()
    with pytest.raises(RunnerError) as exc:
        engine.initialize(credentials, PROJECT)
    assert (exc.value.status, str(exc.value)) == (
        "uncertain",
        "EE initialization response unavailable",
    )


# M004-S08-F1: RefreshError is auth only with structured credential-refusal evidence.
UNAVAILABLE = ("uncertain", "EE initialization response unavailable")
REFUSED = ("auth", "EE credential refresh refused")


def missing_token_error(data):
    """google-auth's own shape for a 2xx body without access_token."""
    try:
        data["access_token"]
    except KeyError as caught:
        return RefreshError("No access token in response.", data, retryable=False).with_traceback(
            None
        ), caught


def chained_missing_token(data):
    error, caught = missing_token_error(data)
    error.__cause__ = caught
    return error


@pytest.mark.parametrize(
    "error, expected",
    [
        (RefreshError("x", {"error": "invalid_grant"}), REFUSED),
        (RefreshError("x", {"error": "invalid_client"}), REFUSED),
        (RefreshError("x", {"error": "unauthorized_client"}), REFUSED),
        (RefreshError("x"), UNAVAILABLE),
        (RefreshError("x", None), UNAVAILABLE),
        (RefreshError("x", {}), UNAVAILABLE),
        (RefreshError("x", "invalid_grant"), UNAVAILABLE),
        (RefreshError("x", {"error": 5}), UNAVAILABLE),
        (RefreshError("x", {"error": ["invalid_grant"]}), UNAVAILABLE),
        (RefreshError("x", {"error": "INVALID_GRANT"}), UNAVAILABLE),
        (RefreshError("x", {"error": "invalid_request"}), UNAVAILABLE),
        (RefreshError("x", {"error": "server_error"}), UNAVAILABLE),
        (RefreshError("x", {"error": "invalid_grant"}, retryable=True), UNAVAILABLE),
        (RefreshError("x", {"error": "temporarily_unavailable"}, retryable=False), UNAVAILABLE),
        (RefreshError("invalid_grant invalid_client " + SECRET), UNAVAILABLE),
        (
            RefreshError(
                "x", {"error": "server_error", "error_description": "invalid_grant " + SECRET}
            ),
            UNAVAILABLE,
        ),
        (chained_missing_token({"error": "invalid_grant", "scope": SECRET}), UNAVAILABLE),
        (chained_missing_token({}), UNAVAILABLE),
    ],
)
@pytest.mark.parametrize("kind", sorted(ENGINES))
def test_refresh_error_classification_requires_structured_refusal(
    monkeypatch, kind, error, expected
):
    monkeypatch.setattr(ee, "Initialize", raising(error))
    engine, credentials = ENGINES[kind]()
    with pytest.raises(RunnerError) as exc:
        engine.initialize(credentials, PROJECT)
    assert (exc.value.status, str(exc.value)) == expected
    assert SECRET not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__ is True


PLAUSIBLE = {
    "expires_in": 3600,
    "token_type": "Bearer",
    "scope": "https://www.googleapis.com/auth/earthengine",
}


@pytest.mark.parametrize(
    "code, reply, expected, paths",
    [
        (200, {}, UNAVAILABLE, ["/token"]),
        (200, PLAUSIBLE, UNAVAILABLE, ["/token"]),
        (400, {"error": "invalid_grant", "error_description": SECRET}, REFUSED, ["/token"]),
        (200, dict(PLAUSIBLE, access_token="SYNTHETIC"), None, None),
    ],
)
def test_real_token_reply_classification(
    serve, ee_state, monkeypatch, code, reply, expected, paths
):
    monkeypatch.setattr(ee.data, "exportImage", lambda *a: pytest.fail("task submitted"))
    routes = ee_routes()
    routes[("POST", "oauth2.googleapis.com", "/token")] = (code, canonical(reply))
    server = serve(routes)
    engine, credentials = personal_engine()
    seen = lambda: [urlsplit(r.full_url).path for r in server.requests]  # noqa: E731
    if expected is None:
        engine.initialize(credentials, PROJECT)
        assert seen().count("/token") == 1 and seen().count(LISTING[2]) == 1
        return
    with pytest.raises(RunnerError) as exc:
        engine.initialize(credentials, PROJECT)
    assert (exc.value.status, str(exc.value)) == expected
    assert SECRET not in str(exc.value) and exc.value.__suppress_context__ is True
    assert seen() == paths  # stopped before discovery/algorithm loading
