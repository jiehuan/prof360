"""YAML for config/: like `yaml.safe_load`, but a key given twice in one mapping is an error.
Plain YAML keeps the later one silently (a second `short_term_investments` concept replaced the
first, D-058)."""

from __future__ import annotations

from typing import IO, Any

import yaml


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while reading a mapping", node.start_mark, f"duplicate key {key!r}",
                key_node.start_mark,
            )  # fmt: skip
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def safe_load(stream: str | bytes | IO[str] | IO[bytes]) -> Any:
    return yaml.load(stream, Loader=_UniqueKeyLoader)
