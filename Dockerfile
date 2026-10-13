# Underwriting Intelligence pipeline (Phase 6, decisions D-034).
# BASE_IMAGE: replace with the Artifactory mirror, e.g.
#   --build-arg BASE_IMAGE=<artifactory-host>/docker-remote/python:3.14-slim
ARG BASE_IMAGE=python:3.14-slim

FROM ${BASE_IMAGE} AS build
WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip \
 && pip wheel --no-cache-dir --wheel-dir /wheels ".[mssql]"

FROM ${BASE_IMAGE}
# Microsoft ODBC Driver 18 for Azure SQL (pyodbc). PLACEHOLDER: point these at the Artifactory
# apt mirror of packages.microsoft.com if the cluster has no internet egress.
ARG MS_REPO=https://packages.microsoft.com
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg ca-certificates unixodbc \
 && curl -fsSL ${MS_REPO}/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg] ${MS_REPO}/debian/${VERSION_ID}/prod ${VERSION_CODENAME} main" \
    > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
 && apt-get purge -y curl gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

WORKDIR /app
COPY config ./config
# OpenShift runs containers with an arbitrary UID in group 0; /data must be group-writable.
RUN mkdir -p /data && chgrp -R 0 /app /data && chmod -R g=u /app /data

ENV PYTHONUNBUFFERED=1 \
    LOG_FORMAT=json \
    CONFIG_DIR=/app/config \
    DATA_DIR=/data \
    DATABASE_URL=sqlite:////data/uw.db

USER 1001
ENTRYPOINT ["uw-pipeline"]
CMD ["run", "--job", "nightly"]
