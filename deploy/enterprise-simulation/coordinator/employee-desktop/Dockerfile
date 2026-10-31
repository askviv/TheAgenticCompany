ARG SELKIES_BASE_IMAGE=ghcr.io/selkies-project/selkies/desktop:2.1.0-ubuntu26.04
FROM ${SELKIES_BASE_IMAGE}

ARG SELKIES_BASE_IMAGE
ARG EMPLOYEE_DESKTOP_DOCKERFILE_SHA256
LABEL org.opencontainers.image.title="TAC Selkies employee desktop"
LABEL org.opencontainers.image.source="https://github.com/selkies-project/selkies"
LABEL org.opencontainers.image.base.name="${SELKIES_BASE_IMAGE}"
LABEL com.theagentcompany.employee-desktop.dockerfile-sha256="${EMPLOYEE_DESKTOP_DOCKERFILE_SHA256}"

USER 0
RUN apt-get clean \
    && apt-get update \
    && apt-get install --no-install-recommends -y netcat-openbsd procps socat x11-utils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/debconf/* /var/log/* /tmp/* /var/tmp/*
USER 1000
