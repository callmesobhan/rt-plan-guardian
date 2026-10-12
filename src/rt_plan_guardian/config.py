"""Ruleset configuration (YAML).

Design rules, all aimed at making a ruleset hard to get silently wrong:
  * unknown rule ids, unknown fields and unknown parameters are errors (a typo must not
    quietly disable a safety check);
  * rules not mentioned in the file still run with their defaults;
  * a ruleset may `extends` another (built-in name or file) and override single rules/params;
  * the raw file's SHA-256 (and its parents') is recorded so a report can be tied to the exact
    ruleset used, and `sources` carries the citations the limits come from.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .models import Severity
from .rules import REGISTRY

_TOP_LEVEL_KEYS = {"ruleset", "rules"}
_META_KEYS = {"name", "version", "description", "extends", "sources"}
_RULE_KEYS = {"enabled", "severity", "params"}
_RULESET_PACKAGE = "rt_plan_guardian.data.rulesets"


@dataclass(frozen=True)
class RuleSetting:
    enabled: bool
    severity: Severity
    params: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "severity": self.severity.value,
            "params": dict(self.params),
        }


@dataclass(frozen=True)
class RuleSet:
    name: str
    version: str
    description: str
    settings: Mapping[str, RuleSetting]  # one entry per registered rule, in registry order
    source: str = "<built-in>"
    sha256: str = ""
    extends: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    parents_sha256: Mapping[str, str] = field(default_factory=dict)


def _type_ok(default: Any, value: Any) -> bool:
    if default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int | float):
        return isinstance(value, int | float) and not isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, list)
    return isinstance(value, type(default))


def _resolve_rules(rules_cfg: Mapping[str, Any], source: str) -> dict[str, RuleSetting]:
    unknown_rules = sorted(set(rules_cfg) - set(REGISTRY))
    if unknown_rules:
        raise ConfigError(
            f"{source}: unknown rule id(s) {unknown_rules}. Run `rtpg rules` to list valid ids."
        )
    settings: dict[str, RuleSetting] = {}
    for rule_id, rule_cls in REGISTRY.items():
        cfg = rules_cfg.get(rule_id) or {}
        if not isinstance(cfg, dict) or set(cfg) - _RULE_KEYS:
            raise ConfigError(
                f"{source}: {rule_id}: settings must be a mapping with keys from {sorted(_RULE_KEYS)}"
            )
        enabled = cfg.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{source}: {rule_id}: 'enabled' must be true or false")
        try:
            severity = Severity(str(cfg.get("severity", rule_cls.default_severity.value)).lower())
        except ValueError:
            raise ConfigError(
                f"{source}: {rule_id}: severity must be one of {[s.value for s in Severity]}"
            ) from None
        user_params = cfg.get("params") or {}
        if not isinstance(user_params, dict):
            raise ConfigError(f"{source}: {rule_id}: 'params' must be a mapping")
        bad = sorted(set(user_params) - set(rule_cls.default_params))
        if bad:
            raise ConfigError(
                f"{source}: {rule_id}: unknown parameter(s) {bad}; "
                f"valid: {sorted(rule_cls.default_params) or 'none'}"
            )
        params = copy.deepcopy(dict(rule_cls.default_params))
        for key, value in user_params.items():
            if not _type_ok(rule_cls.default_params[key], value):
                raise ConfigError(
                    f"{source}: {rule_id}.{key}: expected "
                    f"{type(rule_cls.default_params[key]).__name__}, got {type(value).__name__}"
                )
            params[key] = value
        try:
            rule_cls.validate_params(params)
        except ValueError as exc:
            raise ConfigError(f"{source}: {rule_id}: {exc}") from None
        settings[rule_id] = RuleSetting(enabled, severity, params)
    return settings


def _merge_rules(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    """Child overrides parent per rule; params merge key-by-key."""
    merged = {k: copy.deepcopy(v) for k, v in parent.items()}
    for rule_id, cfg in child.items():
        cfg = cfg or {}
        base = merged.setdefault(rule_id, {}) or {}
        params = {**(base.get("params") or {}), **(cfg.get("params") or {})}
        base = {**base, **{k: v for k, v in cfg.items() if k != "params"}}
        if params:
            base["params"] = params
        merged[rule_id] = base
    return merged


def _read_builtin(name: str) -> tuple[bytes, str]:
    try:
        pkg = resources.files(_RULESET_PACKAGE)
        return pkg.joinpath(f"{name}.yaml").read_bytes(), f"<built-in {name}>"
    except (FileNotFoundError, ModuleNotFoundError):
        raise ConfigError(
            f"Unknown built-in ruleset {name!r}; available: {', '.join(builtin_rulesets())}"
        ) from None


def _load_data(
    raw: bytes, source: str, base_dir: Path | None, chain: tuple[str, ...]
) -> tuple[dict[str, Any], dict[str, Any], list[str], dict[str, str]]:
    """-> (meta, merged rules mapping, extends chain, parent hashes)."""
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: invalid YAML ({exc})") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: top level must be a mapping")
    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(
            f"{source}: unknown top-level key(s) {sorted(unknown)}; allowed {sorted(_TOP_LEVEL_KEYS)}"
        )
    meta = data.get("ruleset") or {}
    if not isinstance(meta, dict) or set(meta) - _META_KEYS:
        raise ConfigError(
            f"{source}: 'ruleset' must be a mapping with keys from {sorted(_META_KEYS)}"
        )
    rules_cfg = data.get("rules") or {}
    if not isinstance(rules_cfg, dict):
        raise ConfigError(f"{source}: 'rules' must be a mapping of rule id -> settings")

    parent_name = meta.get("extends")
    if not parent_name:
        return meta, rules_cfg, [], {}
    if source in chain:
        raise ConfigError(f"{source}: circular 'extends' ({' -> '.join([*chain, source])})")
    parent_path = base_dir / str(parent_name) if base_dir is not None else None
    if parent_path is not None and parent_path.is_file():
        parent_raw, parent_src, parent_dir = (
            parent_path.read_bytes(),
            parent_path.name,
            parent_path.parent,
        )
    else:
        parent_raw, parent_src = _read_builtin(str(parent_name))
        parent_dir = None
    _, parent_rules, parent_chain, parent_hashes = _load_data(
        parent_raw, parent_src, parent_dir, (*chain, source)
    )
    hashes = {**parent_hashes, str(parent_name): hashlib.sha256(parent_raw).hexdigest()}
    return meta, _merge_rules(parent_rules, rules_cfg), [str(parent_name), *parent_chain], hashes


def load_ruleset_bytes(
    raw: bytes, source: str = "<upload>", base_dir: Path | None = None
) -> RuleSet:
    meta, rules, chain, parent_hashes = _load_data(raw, source, base_dir, ())
    sources = meta.get("sources") or []
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise ConfigError(f"{source}: ruleset.sources must be a list of citation strings")
    return RuleSet(
        name=str(meta.get("name", "unnamed ruleset")),
        version=str(meta.get("version", "0")),
        description=str(meta.get("description", "")),
        settings=_resolve_rules(rules, source),
        source=source,
        sha256=hashlib.sha256(raw).hexdigest() if raw else "",
        extends=tuple(chain),
        sources=tuple(sources),
        parents_sha256=parent_hashes,
    )


def parse_ruleset(data: Any, *, source: str = "<memory>", raw: bytes = b"") -> RuleSet:
    """Build a RuleSet from an already-parsed mapping (used by tests and programmatic callers)."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: top level must be a mapping")
    return load_ruleset_bytes(raw or yaml.safe_dump(data).encode(), source)


def builtin_rulesets() -> dict[str, tuple[str, str]]:
    """{name: (title, first source citation)} for every packaged ruleset."""
    out: dict[str, tuple[str, str]] = {}
    for entry in sorted(resources.files(_RULESET_PACKAGE).iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".yaml"):
            continue
        meta = (yaml.safe_load(entry.read_bytes()) or {}).get("ruleset", {})
        out[entry.name[:-5]] = (
            str(meta.get("name", entry.name[:-5])),
            (meta.get("sources") or [""])[0],
        )
    return out


def load_ruleset(path: str | Path | None = None) -> RuleSet:
    """Load a ruleset: a file path, a built-in name (see `rtpg rulesets`), or the default."""
    if path is None:
        raw, source = _read_builtin("default")
        return load_ruleset_bytes(raw, source)
    p = Path(path)
    if p.is_file():
        return load_ruleset_bytes(p.read_bytes(), p.name, p.parent)
    if str(path) in builtin_rulesets():
        raw, source = _read_builtin(str(path))
        return load_ruleset_bytes(raw, source)
    raise ConfigError(
        f"Ruleset not found: {path} (not a file; built-in names: {', '.join(builtin_rulesets())})"
    )
