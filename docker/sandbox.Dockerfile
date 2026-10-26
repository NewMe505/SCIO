# The sandbox image for product builds (M9, ADR 0013 decision 10): the Python image plus
# git, which the build runs through the sandbox. Nothing else: the product environment is a
# venv the install phase creates under /work/.scio/sandbox, and the egress proxy sidecar runs
# scio/aci/sandbox/egress_proxy.py from this same image.
#
#   docker build -t scio-sandbox -f docker/sandbox.Dockerfile docker
#   SCIO_SANDBOX_DOCKER_IMAGE=scio-sandbox scio build --product <dir>
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
