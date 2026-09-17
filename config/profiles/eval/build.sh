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
#
# The script is idempotent: every baked image carries the `capsem.ca.baked` label, and images already
# bearing the current CA fingerprint are skipped. This makes it safe to invoke unconditionally from
# automated template provisioning (e.g. before a golden-snapshot fork) without stacking a redundant
# `update-ca-certificates` layer on each run.
#
# Note that baking re-tags the upstream tag in place, so a later `docker pull` of the same tag silently
# reverts the image to an untrusted one. Re-run this script after any such pull.
cat >/usr/local/bin/eval-bake-ca <<'EOF'
#!/bin/bash
set -euo pipefail
CA_CERT="/usr/local/share/ca-certificates/capsem-ca.crt"
LABEL_KEY="capsem.ca.baked"
if [ ! -f "$CA_CERT" ]; then
    echo "Error: $CA_CERT not found." >&2
    exit 1
fi
CA_FINGERPRINT="$(openssl x509 -in "$CA_CERT" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2 | tr -d ':' | tr 'A-F' 'a-f')"
if [ -z "$CA_FINGERPRINT" ]; then
    CA_FINGERPRINT="$(sha256sum "$CA_CERT" | cut -d' ' -f1)"
fi

FORCE=0
CHECK_ONLY=0
IMAGES=()
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1 ;;
        --check) CHECK_ONLY=1 ;;
        --help|-h)
            echo "usage: eval-bake-ca [--force] [--check] [IMAGE...]"
            exit 0
            ;;
        -*)
            echo "Error: unknown flag $1" >&2
            exit 2
            ;;
        *) IMAGES+=("$1") ;;
    esac
    shift
done
if [ ${#IMAGES[@]} -eq 0 ]; then
    IMAGES=(
        "python:3.12-slim-bookworm"
        "python:3.13-slim-bookworm"
        "mcr.microsoft.com/playwright/python:v1.52.0-noble"
    )
fi

# Echo the label value baked into $1, or the empty string when absent or the image is missing locally.
baked_fingerprint() {
    docker image inspect --format "{{index .Config.Labels \"$LABEL_KEY\"}}" "$1" 2>/dev/null || true
}

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
cp "$CA_CERT" "$TMP_DIR/capsem-ca.crt"

# Echo the short alias for a `*-bookworm` tag (e.g. python:3.12-slim), or the
# empty string for images that have none.
short_alias_for() {
    case "$1" in
        *-bookworm) printf '%s' "${1%-bookworm}" ;;
        *) printf '' ;;
    esac
}

baked=0
skipped=0
missing=0
for img in "${IMAGES[@]}"; do
    current="$(baked_fingerprint "$img")"
    if [ "$current" = "$CA_FINGERPRINT" ] && [ "$FORCE" -eq 0 ]; then
        echo "Already baked, skipping: $img"
        skipped=$((skipped + 1))
        # The short alias is not implied by the long tag. It can be absent or
        # left pointing at a pre-bake image, and Harbor resolves the short form,
        # so a skip that returns early here would leave the alias unbaked.
        if [ "$CHECK_ONLY" -eq 0 ]; then
            short_tag="$(short_alias_for "$img")"
            if [ -n "$short_tag" ] && [ "$(baked_fingerprint "$short_tag")" != "$CA_FINGERPRINT" ]; then
                echo "Re-pointing stale alias: $short_tag -> $img"
                docker tag "$img" "$short_tag"
            fi
        fi
        continue
    fi
    if [ "$CHECK_ONLY" -eq 1 ]; then
        echo "Needs baking: $img"
        missing=$((missing + 1))
        continue
    fi
    echo "Baking Capsem MITM CA into $img..."
    cat >"$TMP_DIR/Dockerfile" <<INNER_EOF
FROM $img
COPY capsem-ca.crt /usr/local/share/ca-certificates/capsem-ca.crt
RUN update-ca-certificates
ENV UV_NATIVE_TLS=1
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
LABEL $LABEL_KEY=$CA_FINGERPRINT
INNER_EOF
    docker build --network=host -t "$img" "$TMP_DIR"
    baked=$((baked + 1))
    short_tag="$(short_alias_for "$img")"
    if [ -n "$short_tag" ]; then
        docker tag "$img" "$short_tag"
    fi
done

if [ "$CHECK_ONLY" -eq 1 ]; then
    echo "Check complete: $skipped baked, $missing need baking."
    [ "$missing" -eq 0 ]
    exit $?
fi
echo "Done baking CA certificates into base images ($baked baked, $skipped already current)."
EOF
chmod 755 /usr/local/bin/eval-bake-ca

# Single entrypoint that brings the guest container runtime to a usable state: dockerd running and
# task base images trusting the Capsem MITM CA. Both steps are idempotent, so this is safe to call on
# every boot and on every cloned sandbox. Harnesses should invoke this instead of `service docker start`,
# which leaves base images unable to complete TLS handshakes through the Capsem MITM proxy.
cat >/usr/local/bin/eval-docker-up <<'EOF'
#!/bin/bash
set -euo pipefail

TIMEOUT_SECS=120
BAKE=1
while [ $# -gt 0 ]; do
    case "$1" in
        --no-bake) BAKE=0 ;;
        --timeout) shift; TIMEOUT_SECS="${1:?--timeout requires a value}" ;;
        --help|-h)
            echo "usage: eval-docker-up [--no-bake] [--timeout SECONDS]"
            exit 0
            ;;
        *)
            echo "Error: unknown argument $1" >&2
            exit 2
            ;;
    esac
    shift
done

# A cloned guest inherits /var/run from its source VM, because /var/run lives on the overlay rather
# than a tmpfs. A fork taken while dockerd was running therefore leaves dead sockets and pidfiles
# behind, and the next dockerd blocks dialing the stale containerd socket before giving up with
# "failed to start containerd: timeout waiting for containerd to start". Clear that state, but only
# when no daemon is actually alive, so a healthy runtime is never disturbed.
purge_stale_runtime() {
    if pgrep -x dockerd >/dev/null 2>&1 || pgrep -x containerd >/dev/null 2>&1; then
        return 0
    fi
    # Container runtimes leave mounts under several paths, not just netns:
    # containerd task rootfs bind mounts and runc state both live here. A
    # recursive lazy unmount detaches whatever is left without needing to
    # enumerate it, and never blocks on a mount that is still busy.
    umount -R -l /var/run/docker /var/run/containerd 2>/dev/null || true
    # Best-effort: a residual busy mount must not abort the script under
    # `set -e`. A failed purge only means dockerd retries its own recovery.
    rm -rf /var/run/docker /var/run/docker.sock /var/run/docker.pid /var/run/containerd 2>/dev/null || true
}

if ! docker info >/dev/null 2>&1; then
    purge_stale_runtime
    echo "Starting dockerd..."
    service docker start >/dev/null 2>&1 || true
    deadline=$((SECONDS + TIMEOUT_SECS))
    until docker info >/dev/null 2>&1; do
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "Error: dockerd did not become ready within ${TIMEOUT_SECS}s." >&2
            exit 1
        fi
        sleep 1
    done
fi
echo "dockerd is ready."

if [ "$BAKE" -eq 1 ]; then
    eval-bake-ca
fi
EOF
chmod 755 /usr/local/bin/eval-docker-up

