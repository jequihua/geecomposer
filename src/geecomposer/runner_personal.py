"""Personal CLI execution: explicit user EE credentials, Drive tasks, local state.

This is the ordinary CLI/local-image default. Institutional and hosted modes
never reach this module. Nothing here logs in, writes credentials, consults
ADC/metadata/key files, or lists/downloads Drive files. Earth Engine COMPLETED
means the export task finished; Drive files remain unverified.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlsplit
import uuid

from ee import oauth
from google.oauth2.credentials import Credentials
import google_auth_httplib2

from .recipe import CEILINGS, RunnerError, canonical, parse_json, require, validate_recipe
from .runner import Runner, public_error, run_id
from .runner_cloud import MAX_STATE, EarthEngine, EETransport

MAX_CREDENTIAL = 65_536
# Every key the installed EE client's saved-login flows write; nothing else.
CREDENTIAL_KEYS = {"refresh_token", "scopes", "client_id", "client_secret", "redirect_uri", "project"}
STATE_FORMAT = "geecomposer-personal-run-1"
CORRUPT = "local run state is corrupt or unrecognized; it was not modified"
POLL_SECONDS = 30
LOCK_ATTEMPTS = 50
LOCK_PAUSE = 0.1
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

if os.name == "nt":
    import msvcrt

    def _lock(fd):
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)


def default_state_dir():
    return Path.home() / ".geecomposer" / "runs"


def default_credentials():
    """The installed EE client's documented saved-login file (never ADC)."""
    return Path(oauth.get_credentials_path())


def settings(project):
    require(
        type(project) is str and re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project),
        "personal online commands require an explicit valid --project",
    )
    # Bound into each run: another project or ceiling set fails resume closed.
    return dict(
        version=1,
        mode="personal",
        project=project,
        limits=dict(CEILINGS),
        poll_seconds=POLL_SECONDS,
    )


def validate(recipe):
    recipe = validate_recipe(recipe)
    require(
        "drive_folder" in recipe["destination"],
        "personal runs export to a Drive folder; GCS runs need institutional policy",
    )
    return recipe


def separate(credentials, state):
    """The state directory must not contain, or sit under, the credential's directory."""
    credential, root = Path(credentials).resolve(), Path(state).resolve()
    require(
        root not in credential.parents and credential.parent not in root.parents,
        "state directory must be separate from the credential file",
    )


def load_credentials(path):
    """Read one explicitly selected EE saved-login file into user credentials.

    Mirrors the installed ee.oauth.get_credentials_arguments defaults, but with
    a caller-selected file, strict bounded JSON and no ADC fallback. Service
    account keys, external-account and gcloud ADC files are rejected.
    """
    try:
        file = Path(path)
        require(
            file.is_file(),
            "personal credential file missing; authenticate on the host or pass --credentials",
            "auth",
        )
        with file.open("rb") as stream:
            data = stream.read(MAX_CREDENTIAL + 1)
    except OSError:
        raise RunnerError("auth", "cannot read the personal credential file") from None
    try:
        value = parse_json(data, MAX_CREDENTIAL)
    except RunnerError:
        raise RunnerError("auth", "personal credential file is not bounded valid JSON") from None
    require(
        type(value) is dict and value.keys() <= CREDENTIAL_KEYS,
        "unsupported credential file; use an Earth Engine saved user login",
        "auth",
    )
    token = value.get("refresh_token")
    require(
        type(token) is str and 0 < len(token) <= 4096,
        "credential file has no refresh token",
        "auth",
    )
    for key in ("client_id", "client_secret", "redirect_uri", "project"):
        require(
            key not in value or (type(value[key]) is str and len(value[key]) <= 1024),
            "invalid credential file field",
            "auth",
        )
    scopes = value.get("scopes", oauth.SCOPES)
    require(
        type(scopes) is list
        and 1 <= len(scopes) <= 20
        and all(type(s) is str and s.startswith("https://www.googleapis.com/auth/") for s in scopes),
        "invalid credential scopes",
        "auth",
    )
    return Credentials(
        None,
        refresh_token=token,
        token_uri=oauth.TOKEN_URI,
        client_id=value.get("client_id", oauth.CLIENT_ID),
        client_secret=value.get("client_secret", oauth.CLIENT_SECRET),
        scopes=scopes,
    )


class PersonalEarthEngine(EarthEngine):
    """Same bounded EE boundary; token refresh may reach only the fixed OAuth host."""

    hosts = ("earthengine.googleapis.com", urlsplit(oauth.TOKEN_URI).netloc)

    def _verified(self, credentials):
        return type(credentials) is Credentials and bool(credentials.refresh_token)

    def initialize(self, credentials, project):
        super().initialize(credentials, project)
        self.auth_request = google_auth_httplib2.Request(EETransport(self.budget, self.hosts))


def _regular(path):
    """lstat result of an existing regular file, None if absent; special files fail."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise RunnerError("uncertain", "local run state unreadable") from None
    # Symlinks, FIFOs, devices and directories are rejected before any open.
    require(stat.S_ISREG(info.st_mode), CORRUPT, "uncertain")
    return info


def _open_regular(path, flags, before=None):
    """Open without following links or blocking on special files, then validate the descriptor.

    The descriptor must still be a regular file (and the same file as the prior
    lstat, when given), which also covers replacement between check and open.
    """
    try:
        fd = os.open(path, flags | _NOFOLLOW | _NONBLOCK | _BINARY, 0o600)
    except OSError:
        raise RunnerError("uncertain", "local run state unreadable") from None
    try:
        info = os.fstat(fd)
        require(
            stat.S_ISREG(info.st_mode)
            and (before is None or (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino)),
            CORRUPT,
            "uncertain",
        )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class LocalStore:
    """Private per-run JSON records with the runner's generation contract.

    Each read/write holds a non-blocking OS lock (released by the kernel if the
    process dies), re-reads the generation, then atomically replaces the record
    from a same-directory temporary file. Unrecognized files are never replaced.
    """

    def __init__(self, directory):
        self.root = Path(directory)
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            raise RunnerError("validation", "state directory cannot be created") from None
        require(self.root.is_dir(), "state directory is not a directory")

    def _record(self, identifier):
        return self.root / (run_id(identifier) + ".json")

    @contextmanager
    def _locked(self, identifier):
        path = self.root / (run_id(identifier) + ".lock")
        fd = _open_regular(path, os.O_RDWR | os.O_CREAT, _regular(path))
        try:
            for _ in range(LOCK_ATTEMPTS):
                try:
                    _lock(fd)
                    break
                except OSError:
                    time.sleep(LOCK_PAUSE)
            else:
                raise RunnerError("conflict", "local run state is locked by another process")
            try:
                yield
            finally:
                _unlock(fd)
        finally:
            os.close(fd)

    def _read(self, identifier):
        path = self._record(identifier)
        before = _regular(path)
        if before is None:
            return None, 0
        require(0 < before.st_size <= MAX_STATE, CORRUPT, "uncertain")
        fd = _open_regular(path, os.O_RDONLY, before)
        try:
            require(0 < os.fstat(fd).st_size <= MAX_STATE, CORRUPT, "uncertain")
            chunks, size = [], 0
            while size <= MAX_STATE:
                chunk = os.read(fd, MAX_STATE + 1 - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b"".join(chunks)
        except OSError:
            raise RunnerError("uncertain", "local run state unreadable") from None
        finally:
            os.close(fd)
        try:
            record = parse_json(data, MAX_STATE)
        except RunnerError:
            raise RunnerError("uncertain", CORRUPT) from None
        require(
            type(record) is dict
            and record.keys() == {"format", "generation", "state"}
            and record["format"] == STATE_FORMAT
            and type(record["generation"]) is int
            and record["generation"] > 0
            and type(record["state"]) is dict
            and record["state"].get("run_id") == identifier,
            CORRUPT,
            "uncertain",
        )
        return record["state"], record["generation"]

    def read(self, identifier):
        if not os.path.lexists(self._record(identifier)):
            return None, 0
        with self._locked(identifier):
            return self._read(identifier)

    def write(self, identifier, state, generation):
        require(state.get("run_id") == identifier, "state belongs to another run", "conflict")
        data = canonical(dict(format=STATE_FORMAT, generation=generation + 1, state=state))
        require(len(data) <= MAX_STATE, "local state size limit exceeded", "artifact")
        with self._locked(identifier):
            _, current = self._read(identifier)
            require(current == generation, "conditional state conflict", "conflict")
            temporary = self.root / f".{identifier}.{uuid.uuid4().hex}.tmp"
            try:
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY,
                    0o600,
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self._record(identifier))
                _sync_directory(self.root)
            except OSError:
                raise RunnerError(
                    "uncertain", "local state write failed; inspect the state directory"
                ) from None
            finally:
                try:
                    temporary.unlink()
                except OSError:
                    pass
        return generation + 1


def public_status(state, verified=None, persisted=True):
    """Task identities and Drive names only; no invented file IDs or URLs.

    operation_evidence is "verified" only for an operation this invocation
    received from EE or confirmed by marker discovery; a name merely read from
    the local record (always the case for offline status) is "saved".
    """
    verified = verified or {}
    folder = state["recipe"]["destination"]["drive_folder"]
    return dict(
        run_id=state["run_id"],
        status=state["status"],
        mode="personal",
        project=state["project"],
        destination=dict(drive_folder=folder),
        tasks=[
            dict(
                name=t["name"],
                state=t["state"],
                request_id=t["request_id"],
                task_id=t["operation"].rsplit("/", 1)[-1] if t.get("operation") else None,
                operation_evidence=(
                    None
                    if not t.get("operation")
                    else "verified"
                    if verified.get(t["request_id"]) == t["operation"]
                    else "saved"
                ),
                file_name_prefix=f"{state['prefix']}_{t['name']}",
            )
            for t in state["tasks"]
        ],
        remote_pending=state.get("remote_pending", False),
        raster_verification="pending",
        error=public_error(state.get("error"), persisted),
    )


class PersonalRunner(Runner):
    """The institutional state machine with Drive-specific reservation/completion."""

    def _validate(self, recipe):
        return validate(recipe)

    def _reservation(self, recipe, identifier):
        return dict(mode="personal", prefix=f"{identifier}-{uuid.uuid4().hex}")

    def _valid_prefix(self, state, recipe, identifier):
        return re.fullmatch(re.escape(identifier) + r"-[0-9a-f]{32}", state["prefix"])

    def _verify_outputs(self):
        # No Drive listing/download: completion is EE task state only.
        pass

    def _stop_outputs(self):
        pass

    def _public(self, state):
        return public_status(state, self.verified, self._persisted(state))


def connect(settings, budget, *, credentials, state):
    """Local store first; credentials only for commands that contact EE."""
    store = LocalStore(state)
    if credentials is None:
        return store, None, None
    return store, PersonalEarthEngine(budget), load_credentials(credentials)
