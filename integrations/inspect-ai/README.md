# Capsem Inspect AI Sandbox Extension (`inspect-capsem-sandbox`)

`inspect-capsem-sandbox` registers a `capsem` [`SandboxEnvironment`](https://inspect.aisi.org.uk/sandboxing.html)
for [Inspect AI](https://inspect.aisi.org.uk/) backed by isolated Capsem micro-VMs.

| What | Name |
| --- | --- |
| Distribution (`pip install`) | `inspect-capsem-sandbox` |
| Python import package | `inspect_capsem` |
| Inspect sandbox type | `capsem` |
| Repository directory | `integrations/inspect-ai` |

## Installation

`inspect-capsem-sandbox` depends on [`capsem`](../../sdk/python/README.md) (`>=0.7.0`), `inspect-ai`,
`pydantic`, and `pyyaml`, and drives the Capsem HTTP gateway (`capsem-service`) through the Python `capsem`
SDK. It requires SDK APIs introduced for `capsem 0.7.0`: those on `main`
(`EXEC_TIMEOUT_CEILING_SECS`, `GATEWAY_REQUEST_BUDGET_SECS`, `decode_exec_output`, VM labels, and OCI container fields on
`Hypervisor.create`) plus `#286` (streaming of large binary bodies), `#287` (`CREATE_READY_SECS`
and the `Hypervisor.create` `request_timeout`), and `Hypervisor.vm(id=...)` foreign VM attachment.
Until `capsem 0.7.0` is published, install from the GitHub repository, pinned to the same branch,
tag, or commit revision `<rev>` so the extension and the SDK match:

```bash
pip install \
  "capsem @ git+https://github.com/google/capsem.git@<rev>#subdirectory=sdk/python" \
  "inspect-capsem-sandbox @ git+https://github.com/google/capsem.git@<rev>#subdirectory=integrations/inspect-ai"
```

With `uv`:

```bash
uv add \
  "capsem @ git+https://github.com/google/capsem.git@<rev>#subdirectory=sdk/python" \
  "inspect-capsem-sandbox @ git+https://github.com/google/capsem.git@<rev>#subdirectory=integrations/inspect-ai"
```

Once installed, `inspect_ai` discovers the `capsem` sandbox environment through
its `[project.entry-points.inspect_ai]` entry point.

## Quickstart

Run any Inspect evaluation in a Capsem VM sandbox:

```bash
inspect eval task.py --sandbox capsem
```

Or configure the sandbox in a `@task` definition (`execution_mode="vm"` runs directly in a Capsem micro-VM; `execution_mode="container"` runs inside a rootless OCI workload container provisioned from a pre-built `image` or single-service `compose_file`):

```python
from inspect_ai import Task, task
from inspect_capsem import CapsemSandboxConfig


@task
def my_eval() -> Task:
    return Task(
        dataset=...,
        solver=...,
        scorer=...,
        sandbox=(
            "capsem",
            CapsemSandboxConfig(template="code", cpu_count=4, ram_gb=8),
        ),
    )
```

A string sandbox config is also accepted: a `compose.yaml` / `docker-compose.yml` path or a pre-built container image reference (`"python:3.12-slim"`). When `working_dir` is not explicitly set, `execution_mode="vm"` defaults to `"/workspace"`, whereas `execution_mode="container"` defaults to the container image's `WORKDIR` (probed via `pwd` at startup, falling back to `"/"`).

## Host Environment & Path Isolation

- **Operator-owned host `os.environ` allowlist (`CAPSEM_INSPECT_ALLOWED_HOST_ENV`)**: `${VAR}` / `$VAR` interpolation and bare `environment: [KEY]` / `environment: {KEY: null}` entries in `compose.yaml` do **not** read from the host's `os.environ` unless the operator sets `CAPSEM_INSPECT_ALLOWED_HOST_ENV` (comma-separated variable names or literal-prefix patterns such as `HF_TOKEN,OPENAI_*`; wildcard-only and character-class patterns are rejected). Task code may narrow this operator allowlist via `CapsemSandboxConfig.allowed_host_env`. Project `.env` files next to `compose.yaml` and Inspect `SAMPLE_METADATA_<KEY>` variables synthesized from scalar sample metadata remain enabled by default.
- **Operator-owned host bind-mount allowlist (`CAPSEM_INSPECT_ALLOWED_HOST_PATHS`)**: Host bind-mount sources inside the resolved `compose.yaml` directory (`os.path.realpath` containment) are allowed automatically; any bind-mount source outside that directory is rejected with `ValueError` unless covered by `CAPSEM_INSPECT_ALLOWED_HOST_PATHS` (optionally narrowed by `CapsemSandboxConfig.allowed_host_paths`). Bind mounts are staged into the VM once at init as one-way, root-owned (`0:0`) tar copies capped at 256 MiB.

## Documentation

See the [Inspect AI Sandbox Documentation](https://docs.capsem.org/usage/inspect-ai/) for:

- `CapsemSandboxConfig` VM-mode configuration fields (`template`, `cpu_count`, `ram_gb`, `working_dir`, `environment`, `user`)
- Gateway discovery (`CAPSEM_GATEWAY_URL`, `CAPSEM_GATEWAY_TOKEN`, `CAPSEM_RUN_DIR`, `CAPSEM_HOME`) and `CAPSEM_VM_PREFIX` isolation
- Execution timeouts, output limits (`OutputLimitExceededError`), `write_file` permissions, and `SIGINT` / `inspect sandbox cleanup capsem` lifecycle handling

For the underlying Python gateway client, see [`sdk/python/README.md`](../../sdk/python/README.md).
