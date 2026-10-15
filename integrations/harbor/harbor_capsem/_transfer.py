"""File, tar-archive transfer, and container definition helpers for `CapsemEnvironment`."""

from __future__ import annotations

import asyncio
import contextlib
import os
import posixpath
import shlex
import tempfile
import uuid
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harbor.environments.definition import (
    DOCKERFILE_NAME,
    require_agent_environment_definition,
    should_use_prebuilt_docker_image,
)
from harbor.environments.tar_transfer import (
    extract_dir_from_bytes,
    pack_dir_to_bytes,
    remote_pack_command,
    remote_unpack_command,
)

# TODO(https://github.com/google/capsem/issues/310, https://github.com/google/capsem/issues/311):
# Extract `_controller`, `_transfer`, `_cleanup`, `_lifecycle`, and `containers/` from
# `inspect_capsem` (`../inspect-ai`) into a shared package (`sdk/python` or
# `integrations/common`) once PRs #336-#340 land upstream.
from inspect_capsem import containers
from inspect_capsem._transfer import (
    _DIRECT_REL_RE,
    _OCI_STAGE_DIR,
    _XFER_STAGE_DIR,
    _rel_to_stage_dir,
    _staged_download,
    _staged_upload,
)
from inspect_capsem.containers.build_grant import resolve_effective_host_build

if TYPE_CHECKING:
    from inspect_capsem._controller import CapsemController

_TAR_TRANSFER_TIMEOUT_SEC = 300
_COMPOSE_FILENAMES = ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml")


@contextlib.contextmanager
def _with_default_build_roots(
    environment_dir: Path,
    allowed_host_paths: Sequence[str],
    host_build: Any,
    *,
    allow_internet: bool = True,
) -> Generator[None, None, None]:
    prev_hb = os.environ.get("CAPSEM_INSPECT_HOST_BUILD")
    prev_paths = os.environ.get("CAPSEM_INSPECT_ALLOWED_HOST_PATHS")
    prev_net = os.environ.get("CAPSEM_INSPECT_BUILD_NETWORK")
    try:
        if prev_hb is None and host_build is not False:
            os.environ["CAPSEM_INSPECT_HOST_BUILD"] = "1"
        if prev_paths is None:
            prebuilt_root = (Path(tempfile.gettempdir()) / "capsem-harbor-prebuilt").resolve()
            roots = [str(environment_dir.resolve()), str(prebuilt_root), *allowed_host_paths]
            os.environ["CAPSEM_INSPECT_ALLOWED_HOST_PATHS"] = os.pathsep.join(roots)
        if prev_net is None and allow_internet:
            os.environ["CAPSEM_INSPECT_BUILD_NETWORK"] = "default"
        yield
    finally:
        if prev_hb is None:
            os.environ.pop("CAPSEM_INSPECT_HOST_BUILD", None)
        if prev_paths is None:
            os.environ.pop("CAPSEM_INSPECT_ALLOWED_HOST_PATHS", None)
        if prev_net is None:
            os.environ.pop("CAPSEM_INSPECT_BUILD_NETWORK", None)


def _validate_environment_definition(
    environment_dir: Path,
    *,
    execution_mode: str,
    docker_image: str | None,
    allowed_host_paths: Sequence[str],
    allowed_host_env: Sequence[str],
    host_build: Any,
) -> tuple[Path | None, dict[str, Any]]:
    compose_file = next(
        (p for n in _COMPOSE_FILENAMES if (p := environment_dir / n).is_file()), None
    )
    if compose_file is not None:
        with _with_default_build_roots(environment_dir, allowed_host_paths, host_build):
            parsed = containers.parse_host_compose_yaml_file(
                compose_file, allowed_host_env=allowed_host_env
            )
            fields = containers.extract_capsem_compose_fields(
                parsed,
                base_dir=compose_file.parent,
                allowed_host_env=allowed_host_env,
                allowed_host_paths=allowed_host_paths,
                host_build=host_build,
            )
        return compose_file, fields
    if execution_mode == "container":
        require_agent_environment_definition(environment_dir, docker_image=docker_image)
    return None, {}


def _resolve_container_start_inputs(
    environment_dir: Path,
    *,
    execution_mode: str,
    docker_image: str | None,
    force_build: bool,
    compose_fields: dict[str, Any],
    allowed_host_paths: Sequence[str],
    host_build: Any,
) -> tuple[str | None, dict[str, Any] | None, tuple[str, ...] | None]:
    if execution_mode == "vm":
        return None, None, None
    oci_image, build_spec = None, None
    if compose_fields:
        oci_image = compose_fields.get("image")
        build_spec = compose_fields.get("build")
    elif (
        should_use_prebuilt_docker_image(
            environment_dir, docker_image=docker_image, force_build=force_build
        )
        and (environment_dir / DOCKERFILE_NAME).is_file()
        and host_build is not False
        and docker_image
    ):
        slug = uuid.uuid5(uuid.NAMESPACE_URL, docker_image).hex[:12]
        p_dir = (Path(tempfile.gettempdir()) / "capsem-harbor-prebuilt" / slug).resolve()
        p_dir.mkdir(parents=True, exist_ok=True)
        p_df = p_dir / DOCKERFILE_NAME
        p_df.write_text(f"FROM {docker_image}\n", encoding="utf-8")
        with _with_default_build_roots(p_dir, allowed_host_paths, host_build):
            build_spec = resolve_effective_host_build(
                str(p_df),
                str(p_dir),
                stanza="Dockerfile",
                allowed_host_paths=(*allowed_host_paths, str(p_dir)),
                host_build=host_build,
            )
    elif should_use_prebuilt_docker_image(
        environment_dir, docker_image=docker_image, force_build=force_build
    ):
        oci_image = docker_image
    elif (df_path := environment_dir / DOCKERFILE_NAME).is_file():
        with _with_default_build_roots(environment_dir, allowed_host_paths, host_build):
            build_spec = resolve_effective_host_build(
                str(df_path),
                str(environment_dir),
                stanza="Dockerfile",
                allowed_host_paths=allowed_host_paths,
                host_build=host_build,
            )
    else:
        oci_image = docker_image
    raw_cmd = compose_fields.get("command")
    if isinstance(raw_cmd, str) and raw_cmd.strip():
        return oci_image, build_spec, tuple(shlex.split(raw_cmd))
    if isinstance(raw_cmd, (list, tuple)) and raw_cmd:
        return oci_image, build_spec, tuple(str(x) for x in raw_cmd)
    return oci_image, build_spec, None


def _resolve_guest_path(path: str, working_dir: str = "/workspace") -> str:
    """Resolve `path` against `working_dir` if relative; return normalized POSIX path."""
    if posixpath.isabs(path):
        return posixpath.normpath(path)
    base = working_dir if posixpath.isabs(working_dir) else f"/{working_dir.lstrip('/')}"
    return posixpath.normpath(posixpath.join(base, path))


async def upload_file(
    controller: CapsemController,
    vm_id: str,
    source_path: Path | str,
    target_path: str,
    *,
    working_dir: str = "/workspace",
) -> None:
    """Upload a local file to `target_path` in the guest VM or OCI workload container."""
    src = Path(source_path)
    if not src.is_file():
        raise FileNotFoundError(f"Source file does not exist: {source_path}")
    guest_dst = _resolve_guest_path(target_path, working_dir)
    data = await asyncio.to_thread(src.read_bytes)
    await controller.upload_to_vm(vm_id, guest_dst, data)
    mode = src.stat().st_mode & 0o777
    if mode & 0o111:
        res = await controller.exec_in_vm(
            vm_id, f"chmod {mode:o} {shlex.quote(guest_dst)}", timeout=30
        )
        if res.exit_code != 0:
            raise RuntimeError(
                f"Failed to set permissions {mode:o} on {guest_dst!r}: {res.stderr or res.stdout}"
            )


async def download_file(
    controller: CapsemController,
    vm_id: str,
    source_path: str,
    target_path: Path | str,
    *,
    working_dir: str = "/workspace",
) -> None:
    """Download `source_path` from the guest VM or OCI workload container to `target_path`."""
    guest_src = _resolve_guest_path(source_path, working_dir)
    data = await controller.download_from_vm(vm_id, guest_src)
    dst = Path(target_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(dst.write_bytes, data)


async def upload_dir(
    controller: CapsemController,
    vm_id: str,
    source_dir: Path | str,
    target_dir: str,
    *,
    working_dir: str = "/workspace",
) -> None:
    """Pack `source_dir` as a `.tar.gz` archive, upload to `/var/tmp`, and extract into `target_dir`."""
    guest_dst = _resolve_guest_path(target_dir, working_dir)
    buffer = await asyncio.to_thread(pack_dir_to_bytes, source_dir, compress=True)
    remote_tar = f"/var/tmp/.hb-upload-{uuid.uuid4().hex[:12]}.tar.gz"
    await controller.upload_to_vm(vm_id, remote_tar, buffer.getvalue())
    unpack = remote_unpack_command(remote_tar, guest_dst).replace("tar -xzf ", "tar -xozf ", 1)
    unpack_cmd = f"{unpack}; __ec=$?; rm -f {shlex.quote(remote_tar)}; exit $__ec"
    res = await controller.exec_in_vm(vm_id, unpack_cmd, timeout=_TAR_TRANSFER_TIMEOUT_SEC)
    if res.exit_code != 0:
        raise RuntimeError(
            f"Failed to extract upload archive into {guest_dst!r} "
            f"(exit {res.exit_code}): {res.stderr or res.stdout}"
        )


async def download_dir(
    controller: CapsemController,
    vm_id: str,
    source_dir: str,
    target_dir: Path | str,
    *,
    working_dir: str = "/workspace",
) -> None:
    """Pack `source_dir` on the guest into `/var/tmp`, download, and extract into `target_dir`."""
    guest_src = _resolve_guest_path(source_dir, working_dir)
    remote_tar = f"/var/tmp/.hb-download-{uuid.uuid4().hex[:12]}.tar.gz"
    pack_cmd = remote_pack_command(guest_src, remote_tar)
    res = await controller.exec_in_vm(vm_id, pack_cmd, timeout=_TAR_TRANSFER_TIMEOUT_SEC)
    if res.exit_code != 0:
        raise RuntimeError(
            f"Failed to create download archive for {guest_src!r} "
            f"(exit {res.exit_code}): {res.stderr or res.stdout}"
        )
    try:
        data = await controller.download_from_vm(vm_id, remote_tar)
    finally:
        with contextlib.suppress(Exception):
            await controller.exec_in_vm(vm_id, f"rm -f {shlex.quote(remote_tar)}", timeout=30)
    await asyncio.to_thread(extract_dir_from_bytes, data, target_dir)


__all__ = [
    "_DIRECT_REL_RE",
    "_OCI_STAGE_DIR",
    "_XFER_STAGE_DIR",
    "_rel_to_stage_dir",
    "_resolve_container_start_inputs",
    "_resolve_guest_path",
    "_staged_download",
    "_staged_upload",
    "_validate_environment_definition",
    "_with_default_build_roots",
    "download_dir",
    "download_file",
    "upload_dir",
    "upload_file",
]
