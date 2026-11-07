"""Versions: this package, the context shapes it reads, and the prompts it accepts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

VERSION = "0.1.0"
# MetricChatContext.ContextVersion values this package understands.
SUPPORTED_CONTEXT_VERSIONS = frozenset({1})


class IncompatibleError(RuntimeError):
    """The database holds a prompt or context this package is too old (or too new) for:
    replace the uw_metric_chat folder with the version the pipeline team names."""


def _parse(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in version.strip().split("."))
    except ValueError as exc:
        raise IncompatibleError(f"not a version: {version!r}") from exc


def check_prompt(prompt: Any) -> None:
    """Raise IncompatibleError when the prompt needs a newer package or reads no context
    version this package supports. `prompt`: a ChatPromptRow or the function's row."""
    row: Mapping[str, Any] = prompt if isinstance(prompt, Mapping) else vars(prompt)
    need = row.get("min_package_version") or row.get("minPackageVersion") or "0"
    if _parse(str(need)) > _parse(VERSION):
        raise IncompatibleError(
            f"prompt {row.get('prompt_version') or row.get('promptVersion')} needs "
            f"uw_metric_chat {need} or later; this is {VERSION}"
        )
    versions = str(row.get("context_versions") or row.get("contextVersions") or "")
    wanted = {int(v) for v in versions.replace(" ", "").split(",") if v.isdigit()}
    if not wanted & SUPPORTED_CONTEXT_VERSIONS:
        raise IncompatibleError(
            f"prompt reads context versions {sorted(wanted)}; this package reads "
            f"{sorted(SUPPORTED_CONTEXT_VERSIONS)}"
        )


def check_context(version: int) -> None:
    if version not in SUPPORTED_CONTEXT_VERSIONS:
        raise IncompatibleError(
            f"context version {version} is not supported by uw_metric_chat {VERSION} "
            f"(reads {sorted(SUPPORTED_CONTEXT_VERSIONS)})"
        )
