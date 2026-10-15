# Underwriting Intelligence pipeline (Phase 6, decisions D-034) and the BFF (D-048).
# BASE_IMAGE: replace with the Artifactory mirror, e.g.
#   --build-arg BASE_IMAGE=<artifactory-host>/docker-remote/python:3.14-slim
ARG BASE_IMAGE=python:3.14-slim

FROM ${BASE_IMAGE} AS build
WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
COPY bff ./bff
RUN pip install --no-cache-dir --upgrade pip \
 && pip wheel --no-cache-dir --wheel-dir /wheels ".[azure]" ./bff

FROM ${BASE_IMAGE} AS odbc
# Microsoft ODBC Driver 18 for Azure SQL (pyodbc). PLACEHOLDER: point these at the Artifactory
# apt mirror of packages.microsoft.com if the cluster has no internet egress.
ARG MS_REPO=https://packages.microsoft.com
# packages-microsoft-prod.deb carries the repository definition and its current signing key
# (Debian 13 is signed with a newer key than the old microsoft.asc). libgssapi-krb5-2 is a
# dependency of the driver library that --no-install-recommends would otherwise leave out.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates unixodbc \
 && . /etc/os-release \
 && curl -fsSL -o /tmp/packages-microsoft-prod.deb \
      ${MS_REPO}/config/debian/${VERSION_ID%%.*}/packages-microsoft-prod.deb \
 && dpkg -i /tmp/packages-microsoft-prod.deb && rm /tmp/packages-microsoft-prod.deb \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 libgssapi-krb5-2 \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# Test image with the same ODBC driver (scripts/test_mssql.sh): docker build --target test .
FROM odbc AS test
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
COPY bff ./bff
RUN pip install --no-cache-dir -e ".[dev,azure]" -e "./bff[dev]"

# BFF image (D-048): docker build --target bff . ; the pipeline image below stays the default.
FROM odbc AS bff
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
WORKDIR /app
COPY config ./config
RUN chgrp -R 0 /app && chmod -R g=u /app
ENV PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/app/config \
    BFF_DATABASE_BACKEND=azure_sql
USER 1001
EXPOSE 8080
CMD ["uvicorn", "p360_bff.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]

FROM odbc
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
