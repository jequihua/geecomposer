"""Finite, explicitly authenticated EE/GCS boundaries for the recipe runner.

No ADC search, credential file, persistent EE login, bucket creation or delete.
The only supported credential source is the platform's attached metadata identity.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

import ee
from google.auth.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage

from .recipe import RunnerError, allowed, canonical, parse_json, require

MAX_STATE = 1_048_576
MAX_OBJECTS = 512


class Budget:
    """One monotonic application budget, including a reserved cleanup interval."""

    def __init__(self, seconds, *, monotonic=time.monotonic, wall=time.time, sleep=time.sleep):
        self.monotonic, self.wall, self.sleep = monotonic, wall, sleep
        self.started = wall()
        self.end = monotonic() + seconds
        self.work_end = self.end - 60
        self.finalizing = False
        self.heartbeat = None

    def checkpoint(self):
        self.timeout()
        if self.heartbeat:
            self.heartbeat()

    def finalize(self):
        self.finalizing = True
        self.end = min(self.end, self.monotonic() + 60)

    def constrain(self, deadline):
        self.end = min(self.end, self.monotonic() + max(0, deadline - self.wall()))
        self.work_end = self.end - 60

    def timeout(self):
        left = (self.end if self.finalizing else self.work_end) - self.monotonic()
        require(left > 0, "application deadline reached", "deadline")
        return min(10.0, left)

    def pause(self, seconds):
        self.timeout()
        left = (self.end if self.finalizing else self.work_end) - self.monotonic()
        self.sleep(min(seconds, max(0, left)))
        self.timeout()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RunnerError("auth", "remote redirect refused")


def _read_url(url, budget, *, headers=None, method="GET", body=None, limit=16384):
    """Fixed-origin callers; ignore ambient proxy configuration and redirects."""
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(url, data=body, headers=headers or {}, method=method)
    try:
        response = opener.open(request, timeout=budget.timeout())
    except HTTPError as exc:
        response = exc
    with response:
        chunks, size = [], 0
        while True:
            budget.timeout()
            chunk = response.read1(min(65536, limit + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            require(size <= limit, "remote response size limit exceeded", "uncertain")
            chunks.append(chunk)
        data = b"".join(chunks)
        budget.timeout()
        return response.code, {k.lower(): v for k, v in response.headers.items()}, data


class Metadata:
    def __init__(self, budget):
        self.budget = budget

    def get(self, resource):
        require(resource in ("", "token"), "invalid metadata resource", "auth")
        status, headers, data = _read_url(
            "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/"
            + resource
            + ("?recursive=true" if not resource else ""),
            self.budget,
            headers={"Metadata-Flavor": "Google"},
        )
        require(
            status == 200 and headers.get("metadata-flavor") == "Google",
            "attached metadata identity unavailable",
            "auth",
        )
        try:
            return parse_json(data, 16384)
        except RunnerError:
            raise RunnerError("auth", "invalid metadata identity response") from None


class AttachedCredentials(Credentials):
    """Tokens and principal come from the same fixed attached-account endpoint.

    Refresh ignores the SDK's generic auth transport; it rechecks the attached
    principal and uses the deadline-bound, redirect/proxy-free metadata adapter.
    """

    def __init__(self, expected, metadata):
        super().__init__()
        self.expected = expected
        self.metadata = metadata
        self.service_account_email = None

    def refresh(self, request):
        try:
            info = self.metadata.get("")
            require(
                type(info) is dict and info.get("email") == self.expected,
                "attached principal differs from administrator policy",
                "auth",
            )
            token = self.metadata.get("token")
            require(
                type(token) is dict
                and type(token.get("access_token")) is str
                and 0 < len(token["access_token"]) <= 8192
                and token.get("token_type", "Bearer") == "Bearer"
                and type(token.get("expires_in")) is int
                and 60 <= token["expires_in"] <= 86400,
                "invalid attached token response",
                "auth",
            )
            self.service_account_email = info["email"]
            self.token = token["access_token"]
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                seconds=token["expires_in"]
            )
        except RunnerError:
            raise
        except Exception:
            raise RunnerError("auth", "attached identity verification failed") from None


def attached_identity(policy, budget, *, metadata=None, environ=None):
    environ = os.environ if environ is None else environ
    require(
        not any(
            environ.get(key)
            for key in (
                "GOOGLE_APPLICATION_CREDENTIALS",
                "GCE_METADATA_HOST",
                "GCE_METADATA_IP",
                "GCE_METADATA_ROOT",
                "GOOGLE_API_USE_CLIENT_CERTIFICATE",
                "STORAGE_EMULATOR_HOST",
                "GOOGLE_CLOUD_UNIVERSE_DOMAIN",
            )
        ),
        "credential discovery and endpoint overrides are forbidden",
        "auth",
    )
    credentials = AttachedCredentials(policy["service_account"], metadata or Metadata(budget))
    credentials.refresh(None)
    return credentials


class _Response(dict):
    def __init__(self, status, headers):
        super().__init__(headers)
        self.status = status
        self.reason = "remote response"


class EETransport:
    """httplib-compatible transport; checks deadline even inside science getInfo loops."""

    def __init__(self, budget, hosts=("earthengine.googleapis.com",)):
        self.budget = budget
        self.hosts = hosts
        self.timeout = 10

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        self.budget.checkpoint()
        url = urlsplit(uri)
        require(
            url.scheme == "https" and url.netloc in self.hosts,
            "unexpected EE endpoint",
            "auth",
        )
        if isinstance(body, str):
            body = body.encode("utf-8")
        status, headers, data = _read_url(
            uri,
            self.budget,
            headers=headers,
            method=method,
            body=body,
            limit=16 * MAX_STATE,
        )
        if status in (429, 500, 502, 503, 504):
            raise TimeoutError("transient EE response")
        if status in (401, 403):
            raise RunnerError("auth", "EE permission denied")
        return _Response(status, headers), data


def remote_error(exc, *, write=False):
    if isinstance(exc, RunnerError):
        return exc
    code = getattr(exc, "code", None)
    if code in (401, 403):
        return RunnerError("auth", "remote permission denied")
    if code in (409, 412):
        return RunnerError("conflict", "conditional state conflict")
    return RunnerError(
        "uncertain",
        "remote write outcome unknown" if write else "remote response unavailable",
    )


class GCS:
    def __init__(self, client, policy, budget):
        self.client, self.policy, self.budget = client, policy, budget

    def bucket_preflight(self, bucket):
        try:
            value = self.client.get_bucket(bucket, timeout=self.budget.timeout(), retry=None)
            return {
                "bucket": bucket,
                "location": value.location,
                "access": "accessible",
            }
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == 404:
                raise RunnerError("auth", "bucket absent according to metadata API") from None
            if code in (401, 403):
                raise RunnerError("auth", "bucket inaccessible; existence not inferred") from None
            raise RunnerError("uncertain", "bucket accessibility unknown") from None

    def _blob(self, run_id):
        loc = self.policy["control"]
        return self.client.bucket(loc["bucket"]).blob(f"{loc['prefix']}/{run_id}.json")

    def read(self, run_id):
        blob = self._blob(run_id)
        try:
            blob.reload(timeout=self.budget.timeout(), retry=None)
            generation = int(blob.generation)
            require(
                blob.size is not None and 0 < blob.size <= MAX_STATE,
                "invalid control object size",
                "uncertain",
            )
            data = blob.download_as_bytes(
                if_generation_match=generation,
                start=0,
                end=MAX_STATE,
                raw_download=True,
                timeout=self.budget.timeout(),
                retry=None,
            )
            return parse_json(data, MAX_STATE), generation
        except Exception as exc:
            if getattr(exc, "code", None) == 404:
                return None, 0
            raise remote_error(exc) from None

    def write(self, run_id, state, generation):
        data = canonical(state)
        require(len(data) <= MAX_STATE, "private manifest size limit exceeded", "artifact")
        blob = self._blob(run_id)
        try:
            blob.upload_from_string(
                data,
                content_type="application/json",
                if_generation_match=generation,
                timeout=self.budget.timeout(),
                retry=None,
            )
            require(blob.generation is not None, "state generation unavailable", "uncertain")
            return int(blob.generation)
        except Exception as exc:
            raise remote_error(exc, write=True) from None

    def read_recipe(self, uri, generation):
        parsed = urlsplit(uri)
        require(
            parsed.scheme == "gs" and parsed.netloc and not parsed.query and not parsed.fragment,
            "hosted input requires a pinned gs object",
        )
        name = parsed.path.removeprefix("/")
        require(
            len(name) <= 512
            and all(part not in ("", ".", "..") for part in name.split("/"))
            and all(c.isascii() and (c.isalnum() or c in "/_.-") for c in name),
            "invalid input object name",
        )
        require(
            allowed({"bucket": parsed.netloc, "prefix": name}, self.policy["inputs"]),
            "input object denied by policy",
            "auth",
        )
        require(type(generation) is int and generation > 0, "input generation required")
        blob = self.client.bucket(parsed.netloc).blob(name, generation=generation)
        limit = self.policy["limits"]["recipe_bytes"]
        try:
            blob.reload(
                if_generation_match=generation,
                timeout=self.budget.timeout(),
                retry=None,
            )
            require(
                blob.size is not None and 0 < blob.size <= limit,
                "recipe byte limit exceeded",
            )
            # Both a size check and a range bound precede allocation; raw prevents transcoding.
            data = blob.download_as_bytes(
                start=0,
                end=limit,
                raw_download=True,
                if_generation_match=generation,
                timeout=self.budget.timeout(),
                retry=None,
            )
            return parse_json(data, limit), {"uri": uri, "generation": generation}
        except Exception as exc:
            raise remote_error(exc) from None

    def inventory(self, bucket, prefix):
        result = []
        try:
            token = None
            for _ in range(MAX_OBJECTS + 1):
                iterator = self.client.list_blobs(
                    bucket,
                    prefix=prefix + "/",
                    max_results=MAX_OBJECTS + 1 - len(result),
                    page_size=100,
                    page_token=token,
                    timeout=self.budget.timeout(),
                    retry=None,
                )
                page = next(iterator.pages, [])
                for blob in page:
                    self.budget.timeout()
                    require(
                        len(result) < MAX_OBJECTS,
                        "output inventory exceeds bound",
                        "artifact",
                    )
                    require(
                        blob.name.startswith(prefix + "/"),
                        "output outside exact run prefix",
                        "artifact",
                    )
                    result.append(
                        dict(
                            name=blob.name,
                            generation=str(blob.generation),
                            size=blob.size,
                            crc32c=blob.crc32c,
                            md5=blob.md5_hash,
                        )
                    )
                token = iterator.next_page_token
                if not token:
                    break
            else:
                raise RunnerError("artifact", "output listing page limit exceeded")
            return result
        except Exception as exc:
            raise remote_error(exc) from None


class EarthEngine:
    # Fixed origins for the EE transport; a personal subclass adds the token host.
    hosts = ("earthengine.googleapis.com",)
    # Refresh request passed to credentials outside EE's own transport (discovery).
    auth_request = None

    def __init__(self, budget):
        self.budget = budget

    def _verified(self, credentials):
        return bool(
            type(credentials) is AttachedCredentials
            and credentials.service_account_email == credentials.expected
            and credentials.token
        )

    def initialize(self, credentials, project):
        require(self._verified(credentials), "verified attached credentials required", "auth")
        try:
            ee.Reset()
            ee.data.setMaxRetries(0)
            ee.Initialize(
                credentials=credentials,
                project=project,
                http_transport=EETransport(self.budget, self.hosts),
            )
            self.credentials, self.project = credentials, project
        except Exception as exc:
            if isinstance(exc, RunnerError):
                raise
            raise RunnerError("auth", "EE initialization or project access failed") from None

    def prepare(self, recipe, prefix, previous=None):
        from .runner_science import prepare

        self.budget.timeout()
        return prepare(recipe, prefix, self.budget, previous)

    def new_id(self):
        self.budget.timeout()
        return ee.data.newTaskId()[0]

    def submit(self, request_id, config):
        self.budget.timeout()
        # Native unstarted configuration; no Task.start or private-field mutation.
        return ee.data.exportImage(request_id, config)

    def status(self, request_id, operation=None):
        self.budget.timeout()
        require(operation is not None, "operation discovery required before status", "uncertain")
        return ee.data.getTaskStatus(operation.rsplit("/", 1)[-1])[0]

    def cancel(self, request_id, operation=None):
        self.budget.timeout()
        require(
            operation is not None, "operation discovery required before cancellation", "uncertain"
        )
        ee.data.cancelOperation(operation)

    def discover(self, marker):
        """Complete bounded enumeration; never equate a request UUID with an operation.

        ee.data.listOperations auto-paginates, so use its documented REST schema
        with our existing bounded transport and already verified credentials.
        """
        require(
            type(marker) is str and re.fullmatch(r"gc-[0-9a-f]{64}", marker),
            "invalid discovery marker",
            "uncertain",
        )
        matches, tokens, names = [], set(), set()
        token = None
        try:
            for _ in range(5):
                self.budget.checkpoint()
                query = {"pageSize": 100}
                if token:
                    query["pageToken"] = token
                url = f"https://earthengine.googleapis.com/v1/projects/{self.project}/operations?{urlencode(query)}"
                headers = {}
                self.credentials.before_request(self.auth_request, "GET", url, headers)
                status, _, data = _read_url(url, self.budget, headers=headers, limit=MAX_STATE)
                require(status not in (401, 403), "operation listing permission denied", "auth")
                require(status == 200, "operation listing inaccessible or unavailable", "uncertain")
                page = parse_json(data, MAX_STATE)
                require(
                    type(page) is dict and not page.get("unreachable"),
                    "incomplete operation listing",
                    "uncertain",
                )
                records = page.get("operations", [])
                require(
                    type(records) is list and len(records) <= 100,
                    "invalid operation listing page",
                    "uncertain",
                )
                for item in records:
                    require(
                        type(item) is dict
                        and type(item.get("name")) is str
                        and re.fullmatch(
                            r"projects/[a-zA-Z0-9_-]+/operations/[a-zA-Z0-9_-]+", item["name"]
                        )
                        and item["name"] not in names
                        and type(item.get("metadata")) is dict
                        and type(item["metadata"].get("description")) is str
                        and item["metadata"].get("state")
                        in ("PENDING", "RUNNING", "CANCELLING", "SUCCEEDED", "FAILED", "CANCELLED"),
                        "malformed or repeated operation record",
                        "uncertain",
                    )
                    names.add(item["name"])
                    if item["metadata"]["description"] == marker:
                        matches.append(item["name"])
                token = page.get("nextPageToken", "")
                require(
                    type(token) is str
                    and len(token) <= 4096
                    and (not token or token not in tokens),
                    "invalid or repeated operation page token",
                    "uncertain",
                )
                if not token:
                    require(len(matches) <= 1, "ambiguous operation discovery", "uncertain")
                    return matches[0] if matches else None
                tokens.add(token)
            raise RunnerError(
                "uncertain", "operation discovery page cap reached; enumeration incomplete"
            )
        except RunnerError as exc:
            if exc.status in ("auth", "uncertain", "deadline", "conflict", "cancelled"):
                raise
            raise RunnerError("uncertain", "operation listing could not be verified") from None
        except Exception:
            raise RunnerError(
                "uncertain", "operation listing unavailable; no automatic retry"
            ) from None


class StorageSession(AuthorizedSession):
    """Recheck time on every SDK HTTP request, including listing pages."""

    def __init__(self, credentials, budget):
        super().__init__(credentials, max_refresh_attempts=0)
        self.trust_env = False
        self.budget = budget

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        require(
            parsed.scheme == "https" and parsed.netloc == "storage.googleapis.com",
            "unexpected storage endpoint",
            "auth",
        )
        kwargs.update(timeout=self.budget.timeout(), allow_redirects=False)
        response = super().request(method, url, **kwargs)
        self.budget.timeout()
        require(not 300 <= response.status_code < 400, "storage redirect refused", "auth")
        return response


def connect(policy, budget):
    credentials = attached_identity(policy, budget)
    # Identity verification precedes constructing a storage client or initializing EE.
    gcs = GCS(
        storage.Client(
            project=policy["project"],
            credentials=credentials,
            _http=StorageSession(credentials, budget),
        ),
        policy,
        budget,
    )
    return gcs, EarthEngine(budget), credentials
