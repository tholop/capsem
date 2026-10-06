"""Compose YAML variable interpolation and project `.env` loading."""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)
CAPSEM_INSPECT_ALLOWED_HOST_ENV_VAR = "CAPSEM_INSPECT_ALLOWED_HOST_ENV"
_COMPOSE_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_PAT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_*?]*$")
_DOTENV_DQ_ESC_MAP = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "$": "$$"}
_SAMPLE_METADATA_PREFIX = "SAMPLE_METADATA_"
AllowedEnvSpec = Sequence[str] | tuple[tuple[str, ...], tuple[str, ...]]


def _validate_host_env_patterns(patterns: Sequence[str], *, source: str) -> tuple[str, ...]:
    cleaned: list[str] = []
    for raw in patterns:
        if not (pat := raw.strip()):
            continue
        if not _ENV_PAT_RE.match(pat):
            raise ValueError(
                f"Wildcard-only or invalid pattern {pat!r} is not allowed in {source}; "
                "specify explicit variable names or a literal prefix such as 'MY_APP_*'."
            )
        cleaned.append(pat)
    return tuple(cleaned)


def resolve_effective_allowed_host_env(
    task_allowed_host_env: Sequence[str] = (),
) -> tuple[str, ...] | tuple[tuple[str, ...], tuple[str, ...]]:
    """Return effective host-env patterns from operator env narrowed by task config."""
    task_pats = _validate_host_env_patterns(task_allowed_host_env, source="allowed_host_env")
    op_pats = _validate_host_env_patterns(
        os.environ.get(CAPSEM_INSPECT_ALLOWED_HOST_ENV_VAR, "").split(","),
        source=CAPSEM_INSPECT_ALLOWED_HOST_ENV_VAR,
    )
    return ((op_pats, task_pats) if task_pats else op_pats) if op_pats else ()


def _is_host_env_allowed(name: str, allowed: AllowedEnvSpec) -> bool:
    """Return True if `name` matches the effective host-env allowlist."""
    if not allowed:
        return False
    if isinstance(allowed[0], tuple):
        return all(any(fnmatch.fnmatchcase(name, p) for p in grp) for grp in allowed)
    return any(fnmatch.fnmatchcase(name, str(pat)) for pat in allowed)


def _warn_blocked_host_env(name: str, warned_blocked: set[str] | None) -> None:
    if warned_blocked is None or name not in warned_blocked:
        if warned_blocked is not None:
            warned_blocked.add(name)
        logger.warning(
            "Ignoring non-allowlisted host environment variable %r in Compose "
            "(set %s or allowed_host_env to pass it through).",
            name,
            CAPSEM_INSPECT_ALLOWED_HOST_ENV_VAR,
        )


def _strip_unquoted_dotenv_comment(raw: str) -> str:
    for idx, ch in enumerate(raw):
        if ch == "#" and (idx == 0 or raw[idx - 1] in (" ", "\t")):
            return raw[:idx].strip()
    return raw.strip()


def _parse_quoted_dotenv_value(raw: str, quote: str) -> str:
    out: list[str] = []
    i, n, closed = 1, len(raw), False
    while i < n:
        ch = raw[i]
        if quote == '"' and ch == "\\" and i + 1 < n:
            out.append(_DOTENV_DQ_ESC_MAP.get(raw[i + 1], "\\" + raw[i + 1]))
            i += 2
        elif ch == quote:
            i, closed = i + 1, True
            break
        else:
            out.append(ch)
            i += 1
    if not closed:
        qtype = "double" if quote == '"' else "single"
        raise ValueError(f"Unterminated {qtype}-quoted value in .env line: {raw!r}")
    if (rest := raw[i:].strip()) and not rest.startswith("#"):
        raise ValueError(f"Unexpected trailing characters after quoted .env value: {raw!r}")
    return "".join(out)


def _load_dotenv(dotenv_path: Path) -> dict[str, str]:
    """Parse a Compose `.env` file into a key/value dict."""
    if not dotenv_path.is_file():
        return {}
    env: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("export ") and len(line) > 7:
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = (p.strip() for p in line.partition("="))
        if not _COMPOSE_VAR_NAME_RE.match(key):
            continue
        if val.startswith("'"):
            env[key] = _parse_quoted_dotenv_value(val, "'")
        else:
            inner = (
                _parse_quoted_dotenv_value(val, '"')
                if val.startswith('"')
                else _strip_unquoted_dotenv_comment(val)
            )
            env[key] = _interpolate_compose_str(inner, env, allow_host_env=False)
    return env


def _split_braced_expr(expr: str) -> tuple[str, str, str]:
    for i, ch in enumerate(expr):
        if ch in (":", "-", "?", "+"):
            if ch == ":":
                if i + 1 < len(expr) and expr[i + 1] in ("-", "?", "+"):
                    return expr[:i], expr[i : i + 2], expr[i + 2 :]
                raise ValueError(f"Invalid Compose variable interpolation: ${{{expr}}}")
            return expr[:i], ch, expr[i + 1 :]
    return expr, "", ""


def _eval_braced_compose_var(expr: str, dotenv: Mapping[str, str], **kw: Any) -> str:
    """Evaluate body of `${...}` with `:-`, `-`, `:?`, `?`, `:+`, `+` operators."""
    var_name, op, operand_raw = _split_braced_expr(expr)
    if not _COMPOSE_VAR_NAME_RE.match(var_name):
        raise ValueError(f"Invalid Compose variable interpolation: ${{{expr}}}")
    sample_env: Mapping[str, str] | None = kw.get("sample_env")
    is_sample_var = var_name.startswith(_SAMPLE_METADATA_PREFIX)
    in_sample = sample_env is not None and var_name in sample_env
    in_dotenv = not is_sample_var and var_name in dotenv
    in_host = not is_sample_var and kw.get("allow_host_env", True) and var_name in os.environ
    host_allowed = in_host and _is_host_env_allowed(var_name, kw.get("allowed_host_env", ()))
    if in_host and not in_sample and not in_dotenv and not host_allowed:
        if op in (":?", "?"):
            raise ValueError(
                f"Required Compose variable {var_name!r} is set in the host environment "
                f"but is not allowlisted via {CAPSEM_INSPECT_ALLOWED_HOST_ENV_VAR} / "
                "allowed_host_env"
            )
        _warn_blocked_host_env(var_name, kw.get("warned_blocked"))
    is_set = in_sample or in_dotenv or host_allowed
    val = (
        sample_env[var_name]
        if in_sample and sample_env is not None
        else (os.environ[var_name] if host_allowed else dotenv.get(var_name, ""))
    )
    if op == "":
        return val if is_set else ""
    if op in (":-", "-") and is_set and (op == "-" or val != ""):
        return val
    if op in (":?", "?") and ((is_set and val != "") if op == ":?" else is_set):
        return val
    if op in (":+", "+") and not (is_set and (op == "+" or val != "")):
        return ""
    op_val = _interpolate_compose_str(operand_raw, dotenv, **kw)
    if op in (":?", "?"):
        raise ValueError(
            f"Missing required Compose variable {var_name!r}: "
            f"{op_val or 'required variable is missing or empty'}"
        )
    return op_val


def _interpolate_compose_str(text: str, dotenv: Mapping[str, str], **kw: Any) -> str:
    """Interpolate `$$`, `${VAR...}` (including nested braces), and `$VAR` in `text`."""
    if "$" not in text:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "$":
            out.append(text[i])
            i += 1
        elif i + 1 < n and text[i + 1] == "$":
            out.append("$")
            i += 2
        elif i + 1 < n and text[i + 1] == "{":
            depth, j = 1, i + 2
            while j < n and depth > 0:
                if text[j : j + 2] == "${":
                    depth, j = depth + 1, j + 2
                else:
                    if text[j] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
            if depth != 0:
                raise ValueError(f"Invalid Compose variable interpolation in {text!r}")
            out.append(_eval_braced_compose_var(text[i + 2 : j], dotenv, **kw))
            i = j + 1
        elif m := re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[i + 1 :]):
            out.append(_eval_braced_compose_var(m.group(0), dotenv, **kw))
            i += 1 + len(m.group(0))
        else:
            out.append("$")
            i += 1
    return "".join(out)


def _interpolate_compose_tree(node: Any, dotenv: Mapping[str, str], **kw: Any) -> Any:
    if isinstance(node, str):
        return _interpolate_compose_str(node, dotenv, **kw)
    if isinstance(node, Mapping):
        return {k: _interpolate_compose_tree(v, dotenv, **kw) for k, v in node.items()}
    if isinstance(node, list):
        return [_interpolate_compose_tree(item, dotenv, **kw) for item in node]
    return node


def build_sample_metadata_env(sample_metadata: Mapping[str, Any] | None) -> dict[str, str]:
    """Convert sample `metadata` into `SAMPLE_METADATA_<KEY>` strings."""
    if not sample_metadata:
        return {}
    return {
        f"{_SAMPLE_METADATA_PREFIX}{str(k).replace(' ', '_').upper()}": str(v)
        for k, v in sample_metadata.items()
        if v is not None
    }


def parse_compose_yaml_file(
    compose_path: Path,
    *,
    allowed_host_env: Sequence[str] = (),
    sample_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Load `compose_path` with `.env` + `SAMPLE_METADATA_*` + operator-allowlisted env."""
    parsed = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    if not isinstance(parsed, dict):
        raise ValueError(f"Compose file {compose_path} must parse to a YAML mapping")
    return _interpolate_compose_tree(
        parsed,
        _load_dotenv(compose_path.parent / ".env"),
        allowed_host_env=resolve_effective_allowed_host_env(allowed_host_env),
        warned_blocked=set(),
        sample_env=build_sample_metadata_env(sample_metadata),
    )
