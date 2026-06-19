#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from datetime import datetime
from datetime import timezone
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


IP_RULE_TYPES = {"IP-CIDR", "IP-CIDR6", "IP-ASN"}
ALL_RULE_TYPES = {"*"}
BEIJING_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class YamlLine:
    lineno: int
    indent: int
    content: str


@dataclass
class ParsedSource:
    resolved_url: str
    rules: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class SourceRecord:
    source: str
    resolved_url: str
    status: str
    error: str = ""
    cached: bool = False


@dataclass
class OutputSpec:
    name: str
    path: Path
    include: set[str]
    exclude: set[str]


@dataclass
class OutputResult:
    name: str
    path: Path
    include: set[str]
    exclude: set[str]
    rules: list[str] = field(default_factory=list)


@dataclass
class GroupResult:
    name: str
    outputs: list[OutputResult] = field(default_factory=list)
    success_sources: list[SourceRecord] = field(default_factory=list)
    failed_sources: list[SourceRecord] = field(default_factory=list)
    duplicate_sources: list[SourceRecord] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="聚合 Clash/Mihomo 规则文件。")
    parser.add_argument(
        "--config",
        default="rule/rule-aggregate.yaml",
        help="配置文件路径，默认值：rule/rule-aggregate.yaml",
    )
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_yaml_subset(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在：{path}")

    lines: list[YamlLine] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw:
            raise ValueError(f"配置文件包含 tab 缩进，行号：{lineno}")
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw[indent:].rstrip()
        lines.append(YamlLine(lineno=lineno, indent=indent, content=content))

    if not lines:
        return {}

    node, next_index = parse_block(lines, 0, lines[0].indent)
    if next_index != len(lines):
        extra = lines[next_index]
        raise ValueError(f"配置文件解析未完成，行号：{extra.lineno}")
    if not isinstance(node, dict):
        raise ValueError("配置文件根节点必须是映射。")
    return node


def parse_block(lines: list[YamlLine], index: int, expected_indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return {}, index

    line = lines[index]
    if line.indent != expected_indent:
        raise ValueError(f"缩进错误，行号：{line.lineno}")

    if line.content.startswith("- "):
        return parse_list(lines, index, expected_indent)
    return parse_dict(lines, index, expected_indent)


def parse_dict(lines: list[YamlLine], index: int, expected_indent: int) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}

    while index < len(lines):
        line = lines[index]
        if line.indent < expected_indent:
            break
        if line.indent > expected_indent:
            raise ValueError(f"缩进错误，行号：{line.lineno}")
        if line.content.startswith("- "):
            break

        key, sep, rest = line.content.partition(":")
        if not sep:
            raise ValueError(f"无效的键值行，行号：{line.lineno}")

        key = key.strip()
        if not key:
            raise ValueError(f"空键名，行号：{line.lineno}")

        rest = rest.strip()
        index += 1
        if rest:
            mapping[key] = parse_scalar(rest)
            continue

        if index >= len(lines) or lines[index].indent <= expected_indent:
            mapping[key] = {}
            continue

        child, index = parse_block(lines, index, lines[index].indent)
        mapping[key] = child

    return mapping, index


def parse_list(lines: list[YamlLine], index: int, expected_indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []

    while index < len(lines):
        line = lines[index]
        if line.indent < expected_indent:
            break
        if line.indent > expected_indent:
            raise ValueError(f"缩进错误，行号：{line.lineno}")
        if not line.content.startswith("- "):
            break

        item_text = line.content[2:].strip()
        index += 1
        if not item_text:
            if index >= len(lines) or lines[index].indent <= expected_indent:
                items.append({})
                continue
            child, index = parse_block(lines, index, lines[index].indent)
            items.append(child)
            continue

        items.append(parse_scalar(item_text))

    return items, index


def parse_scalar(token: str) -> Any:
    token = token.strip()
    if token.startswith("{") and token.endswith("}"):
        inner = token[1:-1].strip()
        if not inner:
            return {}

        mapping: dict[str, Any] = {}
        for item in split_inline_items(inner):
            key, sep, value = item.partition(":")
            if not sep:
                raise ValueError(f"无效的行内映射项：{item}")
            key = key.strip()
            if not key:
                raise ValueError(f"行内映射包含空键名：{item}")
            mapping[key] = parse_scalar(value.strip())
        return mapping

    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(item.strip()) for item in split_inline_items(inner)]
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        return ast.literal_eval(token)
    return token


def split_inline_items(text: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    depth = 0

    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue

        if char in {"'", '"'}:
            quote = char
            current.append(char)
            continue

        if char in "[{":
            depth += 1
            current.append(char)
            continue

        if char in "]}":
            depth -= 1
            if depth < 0:
                raise ValueError(f"行内配置括号不匹配：{text}")
            current.append(char)
            continue

        if char == "," and depth == 0:
            item = "".join(current).strip()
            if item:
                items.append(item)
            current = []
            continue

        current.append(char)

    if quote:
        raise ValueError(f"行内配置引号不匹配：{text}")
    if depth != 0:
        raise ValueError(f"行内配置括号不匹配：{text}")

    item = "".join(current).strip()
    if item:
        items.append(item)
    return items


def get_setting(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def normalize_source(source: str, base_url: str) -> str:
    source = source.strip()
    if not source:
        raise ValueError("源配置不能为空。")
    if source.startswith(("http://", "https://")):
        return source.rstrip("/")
    return f"{base_url.rstrip('/')}/{source}/{source}.list"


def fetch_url_text(url: str, timeout: int = 30) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc.reason) if exc.reason else str(exc)) from exc

    return raw.decode("utf-8-sig", errors="replace")


def parse_rule_line(raw_line: str) -> tuple[str, str] | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None

    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 2:
        return None

    rule_type = parts[0].upper()
    if not rule_type:
        return None

    canonical = ",".join([rule_type, *parts[1:]])
    return rule_type, canonical


def parse_source_text(text: str, resolved_url: str) -> ParsedSource:
    parsed_rules: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        parsed = parse_rule_line(raw_line)
        if parsed is None:
            continue
        rule_type, canonical = parsed
        parsed_rules.append((rule_type, canonical))
    return ParsedSource(resolved_url=resolved_url, rules=parsed_rules)


def normalize_rule_types(
    value: Any,
    field_name: str,
    default: set[str],
    filters: dict[str, set[str]] | None = None,
) -> set[str]:
    if value is None:
        return set(default)
    if not isinstance(value, list):
        raise ValueError(f"{field_name} 必须是列表。")

    filters = filters or {}
    normalized: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} 仅支持非空字符串。")
        item = item.strip()
        if item.startswith("$"):
            filter_name = item[1:]
            if filter_name not in filters:
                raise ValueError(f"{field_name} 引用了不存在的过滤器：{item}")
            normalized.update(filters[filter_name])
            continue
        normalized.add(item if item == "*" else item.upper())
    return normalized


def parse_filters(config: dict[str, Any]) -> dict[str, set[str]]:
    filters_cfg = config.get("filters", {})
    if filters_cfg is None:
        return {}
    if not isinstance(filters_cfg, dict):
        raise ValueError("filters 必须是映射。")

    filters: dict[str, set[str]] = {}
    for filter_name, value in filters_cfg.items():
        if not isinstance(filter_name, str) or not filter_name.strip():
            raise ValueError("filters 包含空过滤器名称。")
        filters[filter_name] = normalize_rule_types(value, f"filters.{filter_name}", set(), filters)
    return filters


def parse_outputs(
    group_name: str,
    group_cfg: dict[str, Any],
    group_include: set[str],
    filters: dict[str, set[str]],
) -> list[OutputSpec]:
    outputs_cfg = group_cfg.get("outputs")
    if not isinstance(outputs_cfg, dict) or not outputs_cfg:
        raise ValueError(f"{group_name} 缺少 outputs 配置。")

    specs: list[OutputSpec] = []
    for output_name, output_cfg in outputs_cfg.items():
        if not isinstance(output_name, str) or not output_name.strip():
            raise ValueError(f"{group_name}.outputs 包含空输出名称。")
        if not isinstance(output_cfg, dict):
            raise ValueError(f"{group_name}.outputs.{output_name} 必须是映射。")

        output_path = output_cfg.get("path")
        if not output_path:
            raise ValueError(f"{group_name}.outputs.{output_name}.path 不能为空。")

        specs.append(
            OutputSpec(
                name=output_name,
                path=Path(str(output_path)),
                include=normalize_rule_types(output_cfg.get("include"), f"{group_name}.{output_name}.include", group_include, filters),
                exclude=normalize_rule_types(output_cfg.get("exclude"), f"{group_name}.{output_name}.exclude", set(), filters),
            )
        )
    return specs


def rule_matches_output(rule_type: str, output: OutputResult) -> bool:
    included = "*" in output.include or rule_type in output.include
    excluded = rule_type in output.exclude
    return included and not excluded


def build_group(
    group_name: str,
    group_cfg: dict[str, Any],
    base_url: str,
    global_include: set[str],
    filters: dict[str, set[str]],
    source_cache: dict[str, ParsedSource],
) -> GroupResult:
    sources = group_cfg.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError(f"{group_name} 的 sources 必须是非空列表。")

    group_include = normalize_rule_types(group_cfg.get("include"), f"{group_name}.include", global_include, filters)
    output_specs = parse_outputs(group_name, group_cfg, group_include, filters)
    outputs = [
        OutputResult(
            name=spec.name,
            path=spec.path,
            include=spec.include,
            exclude=spec.exclude,
        )
        for spec in output_specs
    ]
    output_seen: dict[str, set[str]] = {output.name: set() for output in outputs}
    resolved_seen: set[str] = set()
    success_sources: list[SourceRecord] = []
    failed_sources: list[SourceRecord] = []
    duplicate_sources: list[SourceRecord] = []

    for source in sources:
        if not isinstance(source, str):
            raise ValueError(f"{group_name} 的 sources 仅支持字符串。")

        resolved_url = normalize_source(source, base_url)
        if resolved_url in resolved_seen:
            duplicate_sources.append(
                SourceRecord(
                    source=source,
                    resolved_url=resolved_url,
                    status="duplicate",
                )
            )
            continue

        resolved_seen.add(resolved_url)

        if resolved_url in source_cache:
            parsed_source = source_cache[resolved_url]
            success_sources.append(
                SourceRecord(
                    source=source,
                    resolved_url=resolved_url,
                    status="success",
                    cached=True,
                )
            )
        else:
            try:
                text = fetch_url_text(resolved_url)
                parsed_source = parse_source_text(text, resolved_url)
                source_cache[resolved_url] = parsed_source
                success_sources.append(
                    SourceRecord(
                        source=source,
                        resolved_url=resolved_url,
                        status="success",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                failed_sources.append(
                    SourceRecord(
                        source=source,
                        resolved_url=resolved_url,
                        status="failed",
                        error=str(exc),
                    )
                )
                continue

        for rule_type, rule in parsed_source.rules:
            for output in outputs:
                if not rule_matches_output(rule_type, output):
                    continue
                seen = output_seen[output.name]
                if rule in seen:
                    continue
                seen.add(rule)
                output.rules.append(rule)

    return GroupResult(
        name=group_name,
        outputs=outputs,
        success_sources=success_sources,
        failed_sources=failed_sources,
        duplicate_sources=duplicate_sources,
    )


def write_rule_file(path: Path, build_time: str, rules: list[str], sources: list[SourceRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_rule_file(build_time, rules, sources), encoding="utf-8")


def format_rule_file(build_time: str, rules: list[str], sources: list[SourceRecord]) -> str:
    content = [
        f"# Build Date: {build_time}",
        f"# Rule Count: {len(rules)}",
        "# Source:",
    ]
    if sources:
        content.extend(f"#   - {record.source}: {record.resolved_url}" for record in sources)
    else:
        content.append("#   - none")

    content.extend(
        [
            "",
            *rules,
        ]
    )
    return "\n".join(content).rstrip() + "\n"


def read_existing_rules(path: Path) -> list[str] | None:
    if not path.exists():
        return None
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def output_rules_changed(root: Path, output: OutputResult) -> bool:
    existing_rules = read_existing_rules(root / output.path)
    return existing_rules != output.rules


def format_source_line(record: SourceRecord) -> str:
    extra = "（复用缓存）" if record.cached else ""
    return f"- `{record.source}` -> `{record.resolved_url}`{extra}"


def format_failed_line(record: SourceRecord) -> str:
    return f"- `{record.source}` -> `{record.resolved_url}`（{record.error}）"


def write_build_log(path: Path, build_time: str, results: list[GroupResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# 规则编译日志",
        "",
        f"- 编译日期：{build_time}",
        "",
    ]

    for result in results:
        lines.append(f"## {result.name}")
        for output in result.outputs:
            lines.append(f"- 输出文件：`{output.path.as_posix()}`（{len(output.rules)} 条）")
        lines.append("- 成功源：")
        if result.success_sources:
            lines.extend(format_source_line(record) for record in result.success_sources)
        else:
            lines.append("- 无")

        lines.append("- 失败源：")
        if result.failed_sources:
            lines.extend(format_failed_line(record) for record in result.failed_sources)
        else:
            lines.append("- 无")

        lines.append("- 重复源：")
        if result.duplicate_sources:
            lines.extend(format_source_line(record) for record in result.duplicate_sources)
        else:
            lines.append("- 无")

        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def load_config(path: Path) -> dict[str, Any]:
    if yaml is not None:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        config = load_yaml_subset(path)
    if not isinstance(config, dict):
        raise ValueError("配置文件格式不正确。")
    return config


def main() -> int:
    args = parse_args()
    root = repo_root()
    config_path = (root / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config)

    config = load_config(config_path)
    base_url = str(get_setting(config, "base", "blackmatrix7_raw", default="")).strip()
    if not base_url:
        raise ValueError("缺少 base.blackmatrix7_raw 配置。")

    log_path = Path(get_setting(config, "log", "path", default="rule/list/build-log.md"))

    groups = get_setting(config, "groups", default={})
    if not isinstance(groups, dict) or not groups:
        raise ValueError("groups 配置必须是非空映射。")

    filters = parse_filters(config)
    global_include = normalize_rule_types(config.get("include"), "include", ALL_RULE_TYPES, filters)
    source_cache: dict[str, ParsedSource] = {}
    results: list[GroupResult] = []

    for group_name, group_cfg in groups.items():
        if not isinstance(group_cfg, dict):
            raise ValueError(f"{group_name} 的配置必须是映射。")

        result = build_group(
            group_name=group_name,
            group_cfg=group_cfg,
            base_url=base_url,
            global_include=global_include,
            filters=filters,
            source_cache=source_cache,
        )
        results.append(result)

    changed_outputs = [
        output
        for result in results
        for output in result.outputs
        if output_rules_changed(root, output)
    ]
    if not changed_outputs:
        print("规则无变化，跳过写入。")
        return 0

    build_time = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S %z")
    for result in results:
        for output in result.outputs:
            if output not in changed_outputs:
                continue
            write_rule_file(root / output.path, build_time, output.rules, result.success_sources)

    write_build_log(root / log_path, build_time, results)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
