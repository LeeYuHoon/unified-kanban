"""OAuth opt-in에서 원본 설정의 구조를 엄격하게 검사한다."""
from __future__ import annotations


def parse_config(raw: bytes | None) -> dict:
    import yaml

    if raw is None:
        return {}
    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError("Duplicate YAML key")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    config = yaml.load(raw, Loader=UniqueLoader)
    if config is None and not raw.strip():
        return {}
    if not isinstance(config, dict):
        raise ValueError("Dashboard OAuth config must be a mapping")
    dashboard = config.get("dashboard", {})
    if not isinstance(dashboard, dict):
        raise ValueError("dashboard must be a mapping")
    if "require_auth" in dashboard and type(dashboard["require_auth"]) is not bool:
        raise ValueError("dashboard.require_auth must be a boolean")
    if not isinstance(dashboard.get("oauth", {}), dict):
        raise ValueError("dashboard.oauth must be a mapping")
    return config


def unrelated_config(config: dict) -> dict:
    """허용한 leaf만 제외하고 비교한다. 다른 플러그인의 권한은 보존한다."""
    import copy

    result = copy.deepcopy(config)
    dashboard = result.get("dashboard", {})
    dashboard.pop("require_auth", None)
    if not dashboard:
        result.pop("dashboard", None)
    plugins = result.get("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("plugins must be a mapping for Dashboard OAuth")
    for key in ("enabled", "disabled"):
        values = plugins.get(key, [])
        if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
            raise ValueError("plugin lists must contain names")
        remaining = sorted(set(values) - {"hermes-kanban"})
        if remaining:
            plugins[key] = remaining
        else:
            plugins.pop(key, None)
    entries = plugins.get("entries", {})
    if not isinstance(entries, dict):
        raise ValueError("plugin entries must be a mapping")
    own = entries.get("hermes-kanban", {})
    if not isinstance(own, dict):
        raise ValueError("hermes-kanban entry must be a mapping")
    if "allow_tool_override" in own and own["allow_tool_override"] is not False:
        raise ValueError("hermes-kanban tool override must remain disabled")
    own.pop("allow_tool_override", None)
    if not own:
        entries.pop("hermes-kanban", None)
    if not entries:
        plugins.pop("entries", None)
    if not plugins:
        result.pop("plugins", None)
    return result
