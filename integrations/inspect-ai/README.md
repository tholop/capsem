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
and `pydantic`, and drives the Capsem HTTP gateway (`capsem-service`) through the Python `capsem`
SDK. It requires SDK APIs introduced for `capsem 0.7.0`: those on `main`
(`EXEC_TIMEOUT_CEILING_SECS`, `GATEWAY_REQUEST_BUDGET_SECS`, `decode_exec_output`, and VM labels on
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

Or configure the sandbox in a `@task` definition (runs directly in an
isolated Capsem micro-VM):

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

## Documentation

See the [Inspect AI Sandbox Documentation](https://docs.capsem.org/usage/inspect-ai/) for:

- `CapsemSandboxConfig` VM-mode configuration fields (`template`, `cpu_count`, `ram_gb`, `working_dir`, `environment`, `user`)
- Gateway discovery (`CAPSEM_GATEWAY_URL`, `CAPSEM_GATEWAY_TOKEN`, `CAPSEM_RUN_DIR`, `CAPSEM_HOME`) and `CAPSEM_VM_PREFIX` isolation
- Execution timeouts, output limits (`OutputLimitExceededError`), `write_file` permissions, and `SIGINT` / `inspect sandbox cleanup capsem` lifecycle handling

For the underlying Python gateway client, see [`sdk/python/README.md`](../../sdk/python/README.md).
