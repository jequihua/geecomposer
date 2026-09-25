# Linux/amd64 only. Public registry provenance: container/base-image.json.
FROM python:3.11.16-slim-trixie@sha256:174bec68e0451bffabbb08c7d5d21c6b253f772d81d52b9558af97bb3159b761 AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY container/build-linux.lock container/runtime-linux.lock /locks/
COPY container/runtime_content.py /build/runtime_content.py
RUN python -m pip install --require-hashes --only-binary=:all: -r /locks/build-linux.lock
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels . \
    && python -m venv --without-pip /opt/venv \
    && python -m pip --python /opt/venv install --require-hashes --only-binary=:all: -r /locks/runtime-linux.lock \
    && python -m pip --python /opt/venv install --no-deps /wheels/geecomposer-*.whl \
    && python -m pip --python /opt/venv check \
    && /opt/venv/bin/python /build/runtime_content.py

FROM python:3.11.16-slim-trixie@sha256:174bec68e0451bffabbb08c7d5d21c6b253f772d81d52b9558af97bb3159b761 AS runtime
COPY --from=builder /opt/venv /opt/venv
COPY LICENSE /usr/share/doc/geecomposer/LICENSE
RUN --mount=type=bind,source=container/runtime_bootstrap.py,target=/tmp/runtime_bootstrap.py,readonly \
    python -m pip uninstall -y pip setuptools wheel \
    && python -I -B /tmp/runtime_bootstrap.py \
    && for interpreter in /usr/local/bin/python /opt/venv/bin/python; do \
         "$interpreter" -I -B -c "import importlib.util, pathlib, shutil, sysconfig; assert not (pathlib.Path(sysconfig.get_path('stdlib')) / 'ensurepip').exists(); assert not (pathlib.Path(sysconfig.get_path('stdlib')) / 'ensurepip').is_symlink(); assert all(importlib.util.find_spec(n) is None for n in ('ensurepip', 'pip', 'setuptools', 'wheel')); assert all(shutil.which(n) is None and all(not (pathlib.Path(d) / n).exists() and not (pathlib.Path(d) / n).is_symlink() for d in ('/usr/local/bin', '/opt/venv/bin')) for n in ('pip', 'pip3', 'pip3.11', 'easy_install', 'easy_install-3.11', 'wheel'))" || exit 1; \
       done \
    && mkdir -p /work /home/worker \
    && chown 65532:65532 /work /home/worker \
    && chmod -R a-w /opt/venv \
    && chmod 0555 /opt/venv /opt/venv/lib /opt/venv/lib/python3.11 /opt/venv/lib/python3.11/site-packages
ENV PATH=/opt/venv/bin:$PATH HOME=/home/worker PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /work
USER 65532:65532
ENTRYPOINT ["/opt/venv/bin/geecomposer"]
CMD ["--help"]

FROM runtime AS local
# Shared offline target: help, validate, run --dry-run. No bundled policy.

FROM runtime AS hosted
ARG POLICY_SHA256
USER 0:0
# BuildKit secret is an explicit external administrator input, never build context.
RUN --mount=type=secret,id=runtime_policy,required=true \
    /opt/venv/bin/python -m geecomposer.deployment --mode policy \
      --policy /run/secrets/runtime_policy \
      --output /opt/venv/lib/python3.11/site-packages/geecomposer/runtime_policy.json \
    && /opt/venv/bin/python -c "import sys; from geecomposer.recipe import digest,read_json; assert digest(read_json('/opt/venv/lib/python3.11/site-packages/geecomposer/runtime_policy.json')) == sys.argv[1]" "$POLICY_SHA256" \
    && chown 0:0 /opt/venv/lib/python3.11/site-packages/geecomposer/runtime_policy.json \
    && chmod 0444 /opt/venv/lib/python3.11/site-packages/geecomposer/runtime_policy.json \
    && chmod 0555 /opt/venv/lib/python3.11/site-packages/geecomposer
USER 65532:65532
LABEL org.geecomposer.policy-sha256=$POLICY_SHA256
