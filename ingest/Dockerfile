# Commit Lambda image. Build context is the repo root; see `make commit-image`.
# Pattern: uv "AWS Lambda" integration guide (dependencies layer, then project code).
FROM ghcr.io/astral-sh/uv:0.13 AS uv

FROM public.ecr.aws/lambda/python:3.14

ENV UV_COMPILE_BYTECODE=1 \
    UV_NO_INSTALLER_METADATA=1 \
    UV_LINK_MODE=copy

# Third-party dependencies only, from the lockfile (workspace members are not installed).
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=services/pyproject.toml,target=services/pyproject.toml \
    --mount=type=bind,source=ingest/pyproject.toml,target=ingest/pyproject.toml \
    --mount=type=bind,source=infra/pyproject.toml,target=infra/pyproject.toml \
    --mount=type=bind,source=sdk/pyproject.toml,target=sdk/pyproject.toml \
    uv export --frozen --no-dev --no-emit-workspace --package asteroidea-ingest \
        --no-hashes -o /tmp/requirements.txt && \
    uv pip install --no-cache -r /tmp/requirements.txt --target "${LAMBDA_TASK_ROOT}"

COPY ingest/src/asteroidea_ingest ${LAMBDA_TASK_ROOT}/asteroidea_ingest
# The catalog table specs (plain data, no aws_cdk): the commit names its row columns
# from them. `test_image_dependencies.py` checks this list.
COPY infra/src/asteroidea_infra/__init__.py infra/src/asteroidea_infra/schema.py ${LAMBDA_TASK_ROOT}/asteroidea_infra/

CMD ["asteroidea_ingest.commit.handler"]
