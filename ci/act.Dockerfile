FROM catthehacker/ubuntu:act-24.04@sha256:4f2d5083a9d10d018c1c511eb8665cd480553c11975e78fd903a46daa830768b

ARG ACT_VERSION=0.2.88
ARG BAZELISK_VERSION=1.29.0
ARG BAZELISK_SHA256=5a408715e932c0250d28bd84555f12edbf70117de42f9181691c736eacc4a992

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends clang lld \
    && find /var/lib/apt/lists -mindepth 1 -delete \
    && curl --fail --location --silent --show-error \
      "https://github.com/nektos/act/releases/download/v${ACT_VERSION}/act_Linux_x86_64.tar.gz" \
      --output /tmp/act.tar.gz \
    && tar -xzf /tmp/act.tar.gz -C /usr/local/bin act \
    && rm /tmp/act.tar.gz \
    && curl --fail --location --silent --show-error \
      "https://github.com/bazelbuild/bazelisk/releases/download/v${BAZELISK_VERSION}/bazelisk-linux-amd64" \
      --output /usr/local/bin/bazel \
    && echo "${BAZELISK_SHA256}  /usr/local/bin/bazel" | sha256sum --check --strict \
    && chmod +x /usr/local/bin/bazel \
    && act --version
