# managed-by: clud (docker_build_soldr.py)
ARG RUST_VERSION=1.94.1
FROM rust:${RUST_VERSION}-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git pkg-config build-essential cmake ninja-build \
        libssl-dev clang lld zstd \
 && rm -rf /var/lib/apt/lists/*

# Toolchain caches live in named volumes so cold rebuilds amortize
# across `clud tool run docker-build` invocations.
ENV HOME=/root \
    CARGO_HOME=/cargo-home \
    CARGO_TARGET_DIR=/target \
    RUSTUP_HOME=/rustup-home \
    CARGO_CHEF_LOCAL_DIR=/cargo-chef \
    SOLDR_TRUST_MODE=permissive \
    CARGO_TERM_COLOR=always \
    PATH=/cargo-home/bin:/usr/local/cargo/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

# Seed the toolchain into the same paths that later become named
# volumes. Docker copies this image content into a fresh named volume
# on first mount, so the first `docker exec` does not re-download Rust.
ARG RUST_VERSION=1.94.1
RUN mkdir -p /target /cargo-home /rustup-home /cargo-chef /root/.soldr /src \
 && rustup default "${RUST_VERSION}" \
 && rustup component add rustfmt clippy

# Bake soldr into the helper image instead of installing it during the
# first command. Keep its persistent daemon/cache state in /root/.soldr,
# which cmd_up mounts as a named volume.
ARG SOLDR_VERSION=0.8.0
RUN mkdir -p /opt/soldr-bin \
 && curl -fsSL \
        "https://github.com/zackees/soldr/releases/download/v${SOLDR_VERSION}/soldr-v${SOLDR_VERSION}-x86_64-unknown-linux-gnu.tar.zst" \
    | zstd -d --stdout \
    | tar -xf - -C /opt/soldr-bin \
 && for bin in soldr soldr-clang-shim cargo-chef crgx; do \
        [ -f "/opt/soldr-bin/$bin" ] && cp "/opt/soldr-bin/$bin" "/usr/local/bin/$bin" && chmod +x "/usr/local/bin/$bin"; \
    done \
 && soldr --version

# The soldr release currently ships its Rust cache integration but not the
# zccache CLI used by CMake's compiler launcher. Keep that executable in the
# image while its cache/daemon state remains on the named soldr volume.
ARG ZCCACHE_VERSION=1.12.17
RUN curl -fsSL \
        "https://github.com/zackees/zccache/releases/download/${ZCCACHE_VERSION}/zccache-v${ZCCACHE_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
    | tar -xz -C /opt/soldr-bin \
 && find /opt/soldr-bin -type f -name 'zccache*' -exec cp {} /usr/local/bin/ \; \
 && chmod +x /usr/local/bin/zccache* \
 && zccache --version

WORKDIR /src
CMD ["bash", "-l"]
