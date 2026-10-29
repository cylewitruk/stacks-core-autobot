# Build and pin this separately from the credential-bearing inference image.
ARG BASE_IMAGE=rust:1.98.0-bookworm@sha256:82150a52ec202c1b14d7817e14516c392bb7f5cfebd88f1ed531cb37ebd39922
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends python3 clang libclang-dev \
    && rm -rf /var/lib/apt/lists/* \
    && rustup component add rustfmt clippy \
    && cargo install cargo-nextest --version 0.9.145 --locked --jobs 2
COPY workspace_entry.py local.py policy.py /opt/autobot/
ENV HOME=/tmp/home CARGO_HOME=/cargo CARGO_TARGET_DIR=/target RUSTUP_AUTO_INSTALL=0
WORKDIR /workspace
ENTRYPOINT ["python3", "-I", "/opt/autobot/workspace_entry.py"]
