# The sandbox image for product builds (M9, ADR 0013 decision 10; Node since issue #58): the
# Python image plus git, which the build runs through the sandbox, plus Node 22 for node-ts
# products (I-101). Both runtimes are in one image because the egress proxy sidecar runs
# scio/aci/sandbox/egress_proxy.py with this image's python whatever the product's stack.
# Node comes from the official node:22-slim image; both are Debian bookworm, so the binary's
# libraries match. The product environment (a venv, or node_modules) is created by the
# install phase under /work.
#
#   docker build -t scio-sandbox -f docker/sandbox.Dockerfile docker
#   SCIO_SANDBOX_DOCKER_IMAGE=scio-sandbox scio build --product <dir>
FROM node:22-slim AS node

FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version
WORKDIR /work
