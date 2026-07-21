FROM node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d AS extension-build

WORKDIR /src
COPY vscode-extension/package.json vscode-extension/package-lock.json ./
RUN npm ci
COPY vscode-extension/tsconfig.json vscode-extension/.vscodeignore vscode-extension/README.md vscode-extension/LICENSE ./
COPY vscode-extension/src ./src
RUN npm run compile && npm run package

FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv

FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

ARG DEBIAN_FRONTEND=noninteractive
ARG NODE_VERSION=24.18.0
ARG NODE_SHA256=55aa7153f9d88f28d765fcdad5ae6945b5c0f98a36881703817e4c450fa76742
ARG RUST_VERSION=1.97.1
ARG CODELLDB_VERSION=1.12.2
ARG CODELLDB_SHA256=b85b45a8570051d535b0927c6c9da11c39f3a056c73559064647faf7f37f637d

RUN test "$(uname -m)" = "x86_64"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        libasound2t64 \
        libgbm1 \
        libgtk-3-0 \
        libnss3 \
        libsecret-1-0 \
        libcap2-bin \
        libxkbfile1 \
        libxss1 \
        pkg-config \
        procps \
        unzip \
        xauth \
        xz-utils \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

RUN curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
    && echo "${NODE_SHA256}  node-v${NODE_VERSION}-linux-x64.tar.xz" | sha256sum -c - \
    && mkdir -p /opt/node \
    && tar -xJf "node-v${NODE_VERSION}-linux-x64.tar.xz" \
        --strip-components=1 \
        -C /opt/node \
    && rm "node-v${NODE_VERSION}-linux-x64.tar.xz"

ENV PATH="/opt/node/bin:/root/.cargo/bin:${PATH}"
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
ENV UV_PYTHON=3.14.6
ENV UV_MANAGED_PYTHON=1
ENV CARGO_TARGET_DIR=/opt/pyrust-target
ENV PYRUST_CODELLDB=/opt/vscode-extensions/vadimcn.vscode-lldb-1.12.2/adapter/codelldb
ENV PYRUST_LIBLLDB=/opt/vscode-extensions/vadimcn.vscode-lldb-1.12.2/lldb/lib/liblldb.so
ENV PYRUST_PYTHON_LIBDIR=/opt/uv-python/cpython-3.14.6-linux-x86_64-gnu/lib
ENV DEBUGINFOD_URLS=""

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --profile minimal --default-toolchain "${RUST_VERSION}" \
    && rustc --version \
    && cargo --version

RUN uv python install --install-dir "${UV_PYTHON_INSTALL_DIR}" 3.14.6 \
    && uv python find 3.14.6 \
    && test -d "${PYRUST_PYTHON_LIBDIR}"

RUN codelldb_vsix=/tmp/codelldb-linux-x64.vsix \
    && curl -fL \
        "https://github.com/vadimcn/codelldb/releases/download/v${CODELLDB_VERSION}/codelldb-linux-x64.vsix" \
        -o "${codelldb_vsix}" \
    && echo "${CODELLDB_SHA256}  ${codelldb_vsix}" | sha256sum -c - \
    && mkdir -p \
        "/opt/vscode-extensions/vadimcn.vscode-lldb-${CODELLDB_VERSION}" \
    && unzip -q "${codelldb_vsix}" -d /tmp/codelldb \
    && cp -a /tmp/codelldb/extension/. \
        "/opt/vscode-extensions/vadimcn.vscode-lldb-${CODELLDB_VERSION}/" \
    && test -x "${PYRUST_CODELLDB}" \
    && test -f "${PYRUST_LIBLLDB}" \
    && rm -rf /tmp/codelldb "${codelldb_vsix}"

COPY --from=extension-build /src/pyrust-debugger.vsix /opt/pyrust-extension/pyrust-debugger.vsix

# Preinstall before VS Code Server first scans extensions. The attach helper can
# later refresh this same VSIX through VS Code's extension manager.
RUN unzip -q /opt/pyrust-extension/pyrust-debugger.vsix -d /tmp/pyrust-vsix \
    && extension_dir="$(node -p 'const manifest = require("/tmp/pyrust-vsix/extension/package.json"); `${manifest.publisher}.${manifest.name}-${manifest.version}`')" \
    && install_root="/root/.vscode-server/extensions/${extension_dir}" \
    && mkdir -p "${install_root}" \
    && cp -a /tmp/pyrust-vsix/extension/. "${install_root}/" \
    && test -f "${install_root}/out/extension.js" \
    && rm -rf /tmp/pyrust-vsix

RUN mkdir -p /workspaces /opt/pyrust-target \
    && git config --system --add safe.directory '*'

WORKDIR /workspaces/pyrust-debugger

CMD ["sleep", "infinity"]
