#!/bin/sh
set -eu

mkdir -p /opt/ai-clis/bin
printf '{"name":"capsem-ai-clis","version":"1.0.0","dependencies":{}}\n' > /opt/ai-clis/package.json

install_docker_compose() {
    COMPOSE_VERSION="v2.29.7"
    case "$(uname -m)" in
        aarch64|arm64)
            compose_asset="docker-compose-linux-aarch64"
            compose_sha256="6e9fbd5daa20dca5d7d89145081ae8155d68ef2928b497d9f85b54fe0f9dbb2c"
            ;;
        x86_64|amd64)
            compose_asset="docker-compose-linux-x86_64"
            compose_sha256="383ce6698cd5d5bbf958d2c8489ed75094e34a77d340404d9f32c4ae9e12baf0"
            ;;
        *)
            echo "unsupported docker-compose architecture: $(uname -m)" >&2
            exit 1
            ;;
    esac
    mkdir -p /usr/libexec/docker/cli-plugins
    curl -fsSL --retry 5 --retry-all-errors \
        "https://github.com/docker/compose/releases/download/$COMPOSE_VERSION/$compose_asset" \
        -o /usr/local/bin/docker-compose-v2.real
    printf '%s  /usr/local/bin/docker-compose-v2.real\n' "$compose_sha256" | sha256sum -c -
    chmod 755 /usr/local/bin/docker-compose-v2.real
}

# === Nested Docker & Compose Runtime Setup for Capsem MicroVMs ===

install_docker_compose

cat >/usr/local/bin/patch_compose.py <<'EOF'
import sys
import yaml

for path in sys.argv[1:]:
    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            continue
        modified = False
        if "services" in data and isinstance(data["services"], dict):
            for sname, sconfig in data["services"].items():
                if not isinstance(sconfig, dict):
                    continue
                for key in ["cpus", "nano_cpus", "mem_limit", "memswap_limit"]:
                    if key in sconfig:
                        del sconfig[key]
                        modified = True
                if "deploy" in sconfig and isinstance(sconfig["deploy"], dict):
                    deploy = sconfig["deploy"]
                    if "resources" in deploy and isinstance(deploy["resources"], dict):
                        for sub in ["limits", "reservations"]:
                            if sub in deploy["resources"] and isinstance(deploy["resources"][sub], dict):
                                for key in ["cpus", "nano_cpus", "memory"]:
                                    if key in deploy["resources"][sub]:
                                        del deploy["resources"][sub][key]
                                        modified = True
                if sconfig.get("network_mode") != "host":
                    sconfig["network_mode"] = "host"
                    modified = True
                if "networks" in sconfig:
                    del sconfig["networks"]
                    modified = True
                if "build" in sconfig:
                    if isinstance(sconfig["build"], str):
                        sconfig["build"] = {"context": sconfig["build"], "network": "host"}
                        modified = True
                    elif isinstance(sconfig["build"], dict):
                        if sconfig["build"].get("network") != "host":
                            sconfig["build"]["network"] = "host"
                            modified = True
        if "networks" in data:
            del data["networks"]
            modified = True
        if modified:
            with open(path, "w") as f:
                yaml.dump(data, f)
    except Exception as e:
        print(f"patch_compose warning for {path}: {e}", file=sys.stderr)
EOF
chmod 755 /usr/local/bin/patch_compose.py

cat >/usr/libexec/docker/cli-plugins/docker-compose <<'EOF'
#!/bin/bash
export DOCKER_BUILDKIT=0

if [ "$1" = "docker-cli-plugin-metadata" ]; then
    exec /usr/local/bin/docker-compose-v2.real "$@"
fi

files=()
prev=""
for arg in "$@"; do
    if [ "$prev" = "-f" ] || [ "$prev" = "--file" ]; then
        [ -f "$arg" ] && files+=("$arg")
    fi
    prev="$arg"
done

if [ ${#files[@]} -gt 0 ]; then
    python3 /usr/local/bin/patch_compose.py "${files[@]}"
fi

exec /usr/local/bin/docker-compose-v2.real "$@"
EOF
chmod 755 /usr/libexec/docker/cli-plugins/docker-compose
ln -sf /usr/libexec/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose

if [ ! -x /usr/sbin/runc.real ]; then
    mv /usr/sbin/runc /usr/sbin/runc.real
fi
cat >/usr/sbin/runc <<'EOF'
#!/bin/bash
bundle_dir=""
proc_file=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "--bundle" ] || [ "$prev" = "-b" ]; then
        bundle_dir="$arg"
    fi
    if [ "$prev" = "-p" ] || [ "$prev" = "--process" ]; then
        proc_file="$arg"
    fi
    prev="$arg"
done

# Strip /dev/mqueue from bundle configuration
if [ -n "$bundle_dir" ] && [ -f "$bundle_dir/config.json" ]; then
    python3 -c "
import json
path = '$bundle_dir/config.json'
try:
    with open(path, 'r') as f:
        data = json.load(f)
    if 'mounts' in data:
        data['mounts'] = [m for m in data['mounts'] if m.get('destination') != '/dev/mqueue']
    with open(path, 'w') as f:
        json.dump(data, f)
except Exception:
    pass
"
fi

# Rewrite process.json for exec:
# 1. Reset host cwd to / so runc chdir does not fail before chroot
# 2. Wrap args to chroot into /newroot and cd into intended target directory inside the container
if [ -n "$proc_file" ] && [ -f "$proc_file" ]; then
    python3 -c "
import json
path = '$proc_file'
try:
    with open(path, 'r') as f:
        data = json.load(f)
    if 'args' in data and data['args']:
        orig_cwd = data.get('cwd') or '/'
        data['cwd'] = '/'
        data['args'] = ['chroot', '/newroot', 'sh', '-c', 'dir=\"\$1\"; shift; cd \"\$dir\" 2>/dev/null || cd /; exec \"\$@\"', '--', orig_cwd] + data['args']
    with open(path, 'w') as f:
        json.dump(data, f)
except Exception:
    pass
"
fi

exec /usr/sbin/runc.real --rootless true "$@"
EOF
chmod 755 /usr/sbin/runc

# === Harbor CLI & Terminal-Bench Suite ===
# Install into /opt/uv and /usr/local/bin so they survive rootfs /root cleanup.
export UV_PYTHON_INSTALL_DIR="/opt/uv/python"
export UV_TOOL_DIR="/opt/uv/tools"
export UV_TOOL_BIN_DIR="/usr/local/bin"
mkdir -p /opt/uv/python /opt/uv/tools
uv python install 3.12
uv tool install --python 3.12 harbor
find /opt/uv -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
chmod -R a+rX /opt/uv
harbor --version

for attempt in 1 2 3 4 5; do
    rm -rf /opt/terminal-bench
    if git clone --depth 1 https://github.com/harbor-framework/terminal-bench /opt/terminal-bench; then
        break
    fi
    sleep 2
done
rm -rf /opt/terminal-bench/.git /opt/terminal-bench/archive
chmod -R a+rX /opt/terminal-bench

# Deduplicate identical files across /opt and /usr via hardlinks (e.g. duplicate 63MB .tar.gz in terminal-bench)
# and strip unneeded debug symbols from large binaries so compressed EROFS fits under the 950 MB ceiling.
python3 -c '
import os, hashlib
from collections import defaultdict
by_size = defaultdict(list)
for root in ["/opt", "/usr"]:
    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                try:
                    sz = os.path.getsize(p)
                    if sz > 100000:
                        by_size[sz].append(p)
                except OSError:
                    pass
for sz, paths in by_size.items():
    if len(paths) > 1:
        by_hash = defaultdict(list)
        for p in paths:
            try:
                h = hashlib.sha256(open(p, "rb").read()).hexdigest()
                by_hash[h].append(p)
            except OSError:
                pass
        for h, hpaths in by_hash.items():
            if len(hpaths) > 1:
                first = hpaths[0]
                for dup in hpaths[1:]:
                    try:
                        os.unlink(dup)
                        os.link(first, dup)
                    except OSError:
                        pass
'
strip --strip-unneeded /usr/local/bin/docker-compose-v2.real /usr/bin/docker* /usr/bin/containerd* /usr/sbin/runc* /usr/local/lib/node/bin/node /opt/uv/python/*/lib/libpython*.so* /usr/local/bin/uv /usr/local/bin/uvx 2>/dev/null || true


# Helper script to bake Capsem MITM CA + UV_NATIVE_TLS into target base images once dockerd is running.
cat >/usr/local/bin/eval-bake-ca <<'EOF'
#!/bin/bash
set -euo pipefail
CA_CERT="/usr/local/share/ca-certificates/capsem-ca.crt"
if [ ! -f "$CA_CERT" ]; then
    echo "Error: $CA_CERT not found." >&2
    exit 1
fi

IMAGES=(
    "python:3.12-slim-bookworm"
    "python:3.13-slim-bookworm"
    "mcr.microsoft.com/playwright/python:v1.52.0-noble"
)
if [ $# -gt 0 ]; then
    IMAGES=("$@")
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
cp "$CA_CERT" "$TMP_DIR/capsem-ca.crt"

for img in "${IMAGES[@]}"; do
    echo "Baking Capsem MITM CA into $img..."
    cat >"$TMP_DIR/Dockerfile" <<INNER_EOF
FROM $img
COPY capsem-ca.crt /usr/local/share/ca-certificates/capsem-ca.crt
RUN update-ca-certificates
ENV UV_NATIVE_TLS=1
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
INNER_EOF
    docker build --network=host -t "$img" "$TMP_DIR"
    case "$img" in
        *-bookworm)
            short_tag="${img%-bookworm}"
            docker tag "$img" "$short_tag"
            ;;
    esac
done
echo "Done baking CA certificates into base images."
EOF
chmod 755 /usr/local/bin/eval-bake-ca
