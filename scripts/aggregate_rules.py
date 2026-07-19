#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import os
import random
import sys
import time
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
SOURCE_STATE_VERSION = 1
DEFAULT_SOURCE_STATE_PATH = ".cache/rule-source-state/source-state.json"
CLASSICAL = "classical"
DOMAIN = "domain"
IPCIDR = "ipcidr"
VALID_BEHAVIORS = {CLASSICAL, DOMAIN, IPCIDR}


@dataclass(frozen=True)
class YamlLine:
    lineno: int
    indent: int
    content: str


@dataclass
class ParsedSource:
    resolved_url: str
    behavior: str = CLASSICAL
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
    behavior: str
    include: set[str]
    exclude: set[str]


@dataclass
class OutputResult:
    name: str
    path: Path
    behavior: str
    include: set[str]
    exclude: set[str]
    rules: list[str] = field(default_factory=list)
    source_rules: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class GroupResult:
    name: str
    outputs: list[OutputResult] = field(default_factory=list)
    success_sources: list[SourceRecord] = field(default_factory=list)
    failed_sources: list[SourceRecord] = field(default_factory=list)
    duplicate_sources: list[SourceRecord] = field(default_factory=list)


@dataclass(frozen=True)
class SourceRuleDiff:
    source: str
    added: int
    removed: int


@dataclass
class OutputRuleDiff:
    group_name: str
    output_name: str
    output_path: Path
    source_diffs: list[SourceRuleDiff] = field(default_factory=list)


@dataclass(frozen=True)
class GroupRuleDiffRow:
    source: str
    output_name: str
    output_path: Path
    added: int
    removed: int


@dataclass
class GroupRuleDiff:
    group_name: str
    rows: list[GroupRuleDiffRow] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="聚合 Clash/Mihomo 规则文件。")
    parser.add_argument(
        "--config",
        default="rule/rule-aggregate.yaml",
        help="配置文件路径，默认值：rule/rule-aggregate.yaml",
    )
    parser.add_argument(
        "--state",
        default=DEFAULT_SOURCE_STATE_PATH,
        help=f"源规则状态文件路径，默认值：{DEFAULT_SOURCE_STATE_PATH}",
    )
    parser.add_argument(
        "--report",
        default="",
        help="存在规则更新时写入 Markdown 推送报告的路径。",
    )
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def strip_inline_comment(text: str) -> str:
    """剥离行尾注释 `# ...`，忽略引号内的 #，保留值本身。"""

    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "#":
            # 仅当 # 前面是空白或位于行首时才视为注释，避免切到 URL 中的 #fragment。
            prev = text[index - 1] if index > 0 else " "
            if prev.isspace() or index == 0:
                return text[:index].rstrip()
    return text.rstrip()


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
        content = strip_inline_comment(raw[indent:])
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


def normalize_source(source: str, base_url: str, group_name: str = "") -> str:
    source = source.strip()
    if not source:
        hint = f"{group_name}.sources" if group_name else "sources"
        raise ValueError(f"{hint} 存在空字符串源配置，请检查 YAML 缩进或多余空行。")
    if source.startswith(("http://", "https://")):
        return source.rstrip("/")
    return f"{base_url.rstrip('/')}/{source}/{source}.list"


# 可重试的 HTTP 状态码：限流与临时服务端错误。
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
MAX_FETCH_RETRIES = 4
# 退避上限，避免服务器返回过大的 Retry-After 让整个 Action 卡死。
MAX_BACKOFF_SECONDS = 60.0


def fetch_url_text(url: str, timeout: int = 30) -> str:
    """抓取 URL 文本，对限流(429)和 5xx 做指数退避重试。

    退避基数 2 秒、倍增、最多 4 次；尊重 Retry-After 响应头；加少量抖动避免
    大量源同时重试再次触发限流。
    """

    last_error: str = ""
    for attempt in range(1, MAX_FETCH_RETRIES + 1):
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8-sig", errors="replace")
        except HTTPError as exc:
            last_error = f"HTTP {exc.code} {exc.reason}"
            if exc.code not in RETRYABLE_HTTP_STATUS or attempt == MAX_FETCH_RETRIES:
                raise RuntimeError(last_error) from exc
            # 优先尊重服务器返回的 Retry-After（秒）。
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            wait = _backoff_seconds(attempt, retry_after)
            time.sleep(wait)
            continue
        except URLError as exc:
            last_error = str(exc.reason) if exc.reason else str(exc)
            # 网络层错误（DNS、超时、连接重置）同样退避重试。
            if attempt == MAX_FETCH_RETRIES:
                raise RuntimeError(last_error) from exc
            time.sleep(_backoff_seconds(attempt))
            continue
    raise RuntimeError(last_error or "未知抓取错误")


def _backoff_seconds(attempt: int, retry_after: str | None = None) -> float:
    """计算退避秒数：尊重 Retry-After，否则指数退避 + 抖动。"""

    if retry_after:
        try:
            return min(max(float(retry_after), 1.0), MAX_BACKOFF_SECONDS)
        except (TypeError, ValueError):
            pass
    # 简单指数退避，2/4/8 秒，加最多 50% 抖动打散重试，并限制在上限内。
    base = 2 ** attempt
    return min(float(base) + random.uniform(0, base * 0.5), MAX_BACKOFF_SECONDS)


def normalize_behavior(value: Any, field_name: str) -> str:
    """校验并归一化 behavior/type 值，返回小写形式。"""

    if value is None:
        return CLASSICAL
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串。")
    behavior = value.strip().lower()
    if behavior not in VALID_BEHAVIORS:
        raise ValueError(f"{field_name} 不支持：{value}（可选：{', '.join(sorted(VALID_BEHAVIORS))}）")
    return behavior


def parse_cidr_line(value: str) -> tuple[str, str] | None:
    """把裸 CIDR 值补成规范 (rule_type, canonical)，按是否含冒号判定 v4/v6。

    容错处理 '1.2.3.0/24,no-resolve' 这类带附加选项的行：只取首个 CIDR 段。
    """

    cidr = value.split(",", 1)[0].strip()
    if not cidr or " " in cidr:
        return None
    rule_type = "IP-CIDR6" if ":" in cidr else "IP-CIDR"
    return rule_type, f"{rule_type},{cidr}"


def parse_domain_line(value: str) -> tuple[str, str] | None:
    """把裸域名补成 DOMAIN-SUFFIX 规范形式；支持 '+.example.com' 写法。"""

    domain = value.strip()
    if not domain or " " in domain or "," in domain:
        return None
    # mihomo domain 文件里 '+.example.com' 是显式后缀写法，语义与纯域名行一致（均为后缀匹配）。
    if domain.startswith("+."):
        domain = domain[2:].strip()
    if not domain:
        return None
    return "DOMAIN-SUFFIX", f"DOMAIN-SUFFIX,{domain}"


def rule_value(rule: str) -> str:
    """提取规则行去掉类型前缀后的「值」部分，用于跨 behavior 归一化比较。

    例：'IP-CIDR,1.2.3.0/24' -> '1.2.3.0/24'；'DOMAIN-SUFFIX,google.com' -> 'google.com'。
    规范化规则内部统一为 'TYPE,value[,...]'，取首个逗号之后的内容。
    """

    parts = rule.split(",", 1)
    return parts[1] if len(parts) == 2 else rule


def emit_rule_for_behavior(rule: str, behavior: str) -> str:
    """按 output 的 behavior 把内部规范规则转成文件行。

    classical：原样输出 'TYPE,value,...'。
    ipcidr/domain：剥离类型前缀，只写值（附加选项如 no-resolve 在非 classical 下丢弃）。
    """

    if behavior == CLASSICAL:
        return rule
    return rule_value(rule)


def restore_rule_from_line(line: str, behavior: str) -> str | None:
    """把已写入文件的行按 output behavior 还原成内部规范规则，供 diff 比较。

    classical：原样（行本身即 'TYPE,value,...'）。
    ipcidr：裸 CIDR -> 'IP-CIDR[CIDR6],value'。
    domain：裸域名 -> 'DOMAIN-SUFFIX,value'。
    无法还原的行返回 None。
    """

    value = line.strip()
    if not value:
        return None
    if behavior == CLASSICAL:
        return value
    if behavior == IPCIDR:
        parsed = parse_cidr_line(value)
        return parsed[1] if parsed else None
    if behavior == DOMAIN:
        parsed = parse_domain_line(value)
        return parsed[1] if parsed else None
    return value


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


def parse_source_text(text: str, resolved_url: str, behavior: str = CLASSICAL) -> ParsedSource:
    parsed_rules: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if behavior == IPCIDR:
            parsed = parse_cidr_line(line)
        elif behavior == DOMAIN:
            parsed = parse_domain_line(line)
        else:
            parsed = parse_rule_line(raw_line)

        if parsed is None:
            continue
        rule_type, canonical = parsed
        parsed_rules.append((rule_type, canonical))
    return ParsedSource(resolved_url=resolved_url, behavior=behavior, rules=parsed_rules)


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


def parse_exclude_from(group_name: str, group_cfg: dict[str, Any]) -> list[str]:
    """解析 group 的 exclude_from 配置，返回需要排除其规则的上游 group 名称列表。"""

    raw = group_cfg.get("exclude_from", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{group_name}.exclude_from 必须是列表。")

    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{group_name}.exclude_from 仅支持非空字符串。")
        names.append(item.strip())
    return names


def topological_group_order(group_names: list[str], exclude_from_map: dict[str, list[str]]) -> list[str]:
    """按 exclude_from 依赖返回拓扑顺序；存在循环引用时报错中止。

    被 exclude_from 引用的 group 会先处理，保证下游能拿到上游已完成规则集。
    """

    graph: dict[str, list[str]] = {name: list(exclude_from_map.get(name, [])) for name in group_names}
    for name, deps in graph.items():
        for dep in deps:
            if dep not in graph:
                raise ValueError(f"{name}.exclude_from 引用了不存在的 group：{dep}")

    # 检测环：dep 依赖 name 时说明 name 必须在 dep 之后，若 name 又依赖 dep 则成环。
    order: list[str] = []
    visited: dict[str, int] = {name: 0 for name in group_names}  # 0=未访问,1=访问中,2=已完成

    def visit(node: str, stack: list[str]) -> None:
        if visited[node] == 2:
            return
        if visited[node] == 1:
            cycle = " -> ".join(stack + [node])
            raise ValueError(f"exclude_from 存在循环引用：{cycle}")
        visited[node] = 1
        for dep in graph[node]:
            visit(dep, stack + [node])
        visited[node] = 2
        order.append(node)

    for name in group_names:
        visit(name, [])
    return order


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

        behavior = normalize_behavior(output_cfg.get("type"), f"{group_name}.outputs.{output_name}.type")
        specs.append(
            OutputSpec(
                name=output_name,
                path=Path(str(output_path)),
                behavior=behavior,
                include=normalize_rule_types(output_cfg.get("include"), f"{group_name}.{output_name}.include", group_include, filters),
                exclude=normalize_rule_types(output_cfg.get("exclude"), f"{group_name}.{output_name}.exclude", set(), filters),
            )
        )
    return specs


@dataclass(frozen=True)
class SourceSpec:
    name: str            # 展示名（source 标识，用于日志/状态/去重记录）
    resolved_url: str    # 实际抓取 URL
    behavior: str        # classical / domain / ipcidr


def parse_sources(group_name: str, group_cfg: dict[str, Any], base_url: str) -> list[SourceSpec]:
    """解析 sources，支持简写与对象两种写法：

    - 简写：Google -> 展开为 {base}/Google/Google.list，behavior 默认 classical。
    - 对象：{name: Google, behavior: ipcidr} 或 {url: "https://...", behavior: domain}。
    """

    raw = group_cfg.get("sources")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{group_name} 的 sources 必须是非空列表。")

    specs: list[SourceSpec] = []
    for item in raw:
        if isinstance(item, str):
            source_name = item.strip()
            if not source_name:
                hint = f"{group_name}.sources"
                raise ValueError(f"{hint} 存在空字符串源配置，请检查 YAML 缩进或多余空行。")
            specs.append(
                SourceSpec(
                    name=source_name,
                    resolved_url=normalize_source(source_name, base_url, group_name),
                    behavior=CLASSICAL,
                )
            )
            continue

        if not isinstance(item, dict):
            raise ValueError(f"{group_name}.sources 仅支持字符串或映射。")

        url_value = item.get("url")
        name_value = item.get("name")
        behavior = normalize_behavior(item.get("behavior"), f"{group_name}.sources[].behavior")

        if url_value:
            if not isinstance(url_value, str) or not url_value.strip():
                raise ValueError(f"{group_name}.sources[].url 必须是非空字符串。")
            url_value = url_value.strip()
            display = name_value.strip() if isinstance(name_value, str) and name_value.strip() else url_value
            specs.append(SourceSpec(name=display, resolved_url=url_value, behavior=behavior))
            continue

        if name_value:
            if not isinstance(name_value, str) or not name_value.strip():
                raise ValueError(f"{group_name}.sources[].name 必须是非空字符串。")
            name_value = name_value.strip()
            specs.append(
                SourceSpec(
                    name=name_value,
                    resolved_url=normalize_source(name_value, base_url, group_name),
                    behavior=behavior,
                )
            )
            continue

        raise ValueError(f"{group_name}.sources[] 必须提供 name 或 url。")

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
    source_cache: dict[tuple[str, str], ParsedSource],
    exclude_sets: dict[str, set[str]] | None = None,
) -> GroupResult:
    # exclude_sets: 跨组去重用，按 output 名提供上游 group 已完成「规范值」集合。
    exclude_sets = exclude_sets or {}
    source_specs = parse_sources(group_name, group_cfg, base_url)

    group_include = normalize_rule_types(group_cfg.get("include"), f"{group_name}.include", global_include, filters)
    output_specs = parse_outputs(group_name, group_cfg, group_include, filters)
    outputs = [
        OutputResult(
            name=spec.name,
            path=spec.path,
            behavior=spec.behavior,
            include=spec.include,
            exclude=spec.exclude,
        )
        for spec in output_specs
    ]
    output_seen: dict[str, set[str]] = {output.name: set() for output in outputs}
    output_source_seen: dict[str, dict[str, set[str]]] = {output.name: {} for output in outputs}
    # 按 output 名汇总跨组排除「规范值」；只有同名 output 才参与跨组排除。
    output_excluded_values: dict[str, set[str]] = {
        output.name: set(exclude_sets.get(output.name, set()))
        for output in outputs
    }
    resolved_seen: set[str] = set()
    success_sources: list[SourceRecord] = []
    failed_sources: list[SourceRecord] = []
    duplicate_sources: list[SourceRecord] = []

    for spec in source_specs:
        resolved_url = spec.resolved_url
        if resolved_url in resolved_seen:
            duplicate_sources.append(
                SourceRecord(
                    source=spec.name,
                    resolved_url=resolved_url,
                    status="duplicate",
                )
            )
            continue

        resolved_seen.add(resolved_url)
        cache_key = (resolved_url, spec.behavior)

        if cache_key in source_cache:
            parsed_source = source_cache[cache_key]
            success_sources.append(
                SourceRecord(
                    source=spec.name,
                    resolved_url=resolved_url,
                    status="success",
                    cached=True,
                )
            )
        else:
            try:
                text = fetch_url_text(resolved_url)
                parsed_source = parse_source_text(text, resolved_url, spec.behavior)
                source_cache[cache_key] = parsed_source
                success_sources.append(
                    SourceRecord(
                        source=spec.name,
                        resolved_url=resolved_url,
                        status="success",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                failed_sources.append(
                    SourceRecord(
                        source=spec.name,
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

                # 跨组去重：按「规范值」判断是否命中上游 group 同名 output 已有规则，
                # 这样 classical 的 'IP-CIDR,1.2.3.0/24' 与 ipcidr 的 '1.2.3.0/24' 视为同一条。
                if rule_value(rule) in output_excluded_values[output.name]:
                    continue

                source_seen = output_source_seen[output.name].setdefault(spec.name, set())
                if rule not in source_seen:
                    source_seen.add(rule)
                    output.source_rules.setdefault(spec.name, []).append(rule)

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


def write_rule_file(path: Path, build_time: str, rules: list[str], sources: list[SourceRecord], behavior: str = CLASSICAL) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_rule_file(build_time, rules, sources, behavior), encoding="utf-8")


def format_rule_file(build_time: str, rules: list[str], sources: list[SourceRecord], behavior: str = CLASSICAL) -> str:
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
            *(emit_rule_for_behavior(rule, behavior) for rule in rules),
        ]
    )
    return "\n".join(content).rstrip() + "\n"


def read_existing_rules(path: Path, behavior: str = CLASSICAL) -> list[str] | None:
    """读取已写入的规则文件，按 output behavior 还原成内部规范形式。"""

    if not path.exists():
        return None
    restored: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        rule = restore_rule_from_line(line, behavior)
        if rule is not None:
            restored.append(rule)
    return restored


def output_rules_changed(root: Path, output: OutputResult) -> bool:
    existing_rules = read_existing_rules(root / output.path, output.behavior)
    return existing_rules != output.rules


def resolve_repo_path(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def empty_source_state() -> dict[str, Any]:
    return {"version": SOURCE_STATE_VERSION, "groups": {}}


def load_source_state(path: Path) -> tuple[dict[str, Any], bool]:
    if not path.exists():
        return empty_source_state(), False

    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError(f"源规则状态文件格式不正确：{path}")
    if state.get("version") != SOURCE_STATE_VERSION:
        return empty_source_state(), False
    groups = state.get("groups")
    if not isinstance(groups, dict):
        return empty_source_state(), False
    return state, True


def build_source_state(results: list[GroupResult]) -> dict[str, Any]:
    state: dict[str, Any] = {"version": SOURCE_STATE_VERSION, "groups": {}}
    groups_state: dict[str, Any] = state["groups"]

    for result in results:
        outputs_state: dict[str, Any] = {}
        for output in result.outputs:
            sources_state = {
                source: sorted(rules)
                for source, rules in sorted(output.source_rules.items())
            }
            outputs_state[output.name] = {
                "path": output.path.as_posix(),
                "sources": sources_state,
            }
        groups_state[result.name] = {"outputs": outputs_state}

    return state


def write_source_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def get_previous_source_rules(state: dict[str, Any], group_name: str, output_name: str, source: str) -> list[str]:
    groups = state.get("groups", {})
    if not isinstance(groups, dict):
        return []
    group_state = groups.get(group_name, {})
    if not isinstance(group_state, dict):
        return []
    outputs = group_state.get("outputs", {})
    if not isinstance(outputs, dict):
        return []
    output_state = outputs.get(output_name, {})
    if not isinstance(output_state, dict):
        return []
    sources = output_state.get("sources", {})
    if not isinstance(sources, dict):
        return []
    rules = sources.get(source, [])
    return rules if isinstance(rules, list) else []


def get_previous_source_names(state: dict[str, Any], group_name: str, output_name: str) -> set[str]:
    groups = state.get("groups", {})
    if not isinstance(groups, dict):
        return set()
    group_state = groups.get(group_name, {})
    if not isinstance(group_state, dict):
        return set()
    outputs = group_state.get("outputs", {})
    if not isinstance(outputs, dict):
        return set()
    output_state = outputs.get(output_name, {})
    if not isinstance(output_state, dict):
        return set()
    sources = output_state.get("sources", {})
    if not isinstance(sources, dict):
        return set()
    return {source for source in sources if isinstance(source, str)}


def build_source_diffs(previous_state: dict[str, Any], results: list[GroupResult]) -> list[OutputRuleDiff]:
    output_diffs: list[OutputRuleDiff] = []

    for result in results:
        for output in result.outputs:
            source_diffs: list[SourceRuleDiff] = []
            source_names = get_previous_source_names(previous_state, result.name, output.name) | set(output.source_rules)
            for source in sorted(source_names):
                rules = output.source_rules.get(source, [])
                previous_rules = set(get_previous_source_rules(previous_state, result.name, output.name, source))
                current_rules = set(rules)
                added = len(current_rules - previous_rules)
                removed = len(previous_rules - current_rules)
                if added or removed:
                    source_diffs.append(SourceRuleDiff(source=source, added=added, removed=removed))

            if source_diffs:
                output_diffs.append(
                    OutputRuleDiff(
                        group_name=result.name,
                        output_name=output.name,
                        output_path=output.path,
                        source_diffs=source_diffs,
                    )
                )

    return output_diffs


def markdown_escape_table_cell(value: str) -> str:
    return value.replace("|", "\\|")


def workflow_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def print_workflow_output(name: str, value: str) -> None:
    print(f"{name}={value}")


def github_actions_run_url() -> str:
    repository = workflow_value("GITHUB_REPOSITORY")
    run_id = workflow_value("GITHUB_RUN_ID")
    if not repository or not run_id:
        return ""
    return f"https://github.com/{repository}/actions/runs/{run_id}"


def group_output_diffs(output_diffs: list[OutputRuleDiff]) -> list[GroupRuleDiff]:
    """按 group 聚合 output 级别差异，便于同一张表展示 non_ip / ip 等多种输出。"""

    groups: dict[str, GroupRuleDiff] = {}

    for output_diff in output_diffs:
        group = groups.setdefault(output_diff.group_name, GroupRuleDiff(group_name=output_diff.group_name))
        for source_diff in output_diff.source_diffs:
            group.rows.append(
                GroupRuleDiffRow(
                    source=source_diff.source,
                    output_name=output_diff.output_name,
                    output_path=output_diff.output_path,
                    added=source_diff.added,
                    removed=source_diff.removed,
                )
            )

    return list(groups.values())


def build_update_report(output_diffs: list[OutputRuleDiff]) -> str:
    repository = workflow_value("GITHUB_REPOSITORY", "local")
    ref_name = workflow_value("GITHUB_REF_NAME", "local")
    run_url = github_actions_run_url()

    lines = [
        "## 规则更新",
        "",
        f"仓库：`{repository}`",
        f"分支：`{ref_name}`",
    ]
    if run_url:
        lines.append(f"运行：[查看 GitHub Actions]({run_url})")
    lines.append("")

    for group_diff in group_output_diffs(output_diffs):
        lines.extend(
            [
                f"### {group_diff.group_name}",
                "",
                "| 源规则 | 类型 | 新增 | 删除 |",
                "|---|---|---:|---:|",
            ]
        )
        for row in sorted(group_diff.rows, key=lambda item: (item.source, item.output_name)):
            source = markdown_escape_table_cell(row.source)
            output_name = markdown_escape_table_cell(row.output_name)
            lines.append(
                f"| `{source}` | {output_name} | {row.added} | {row.removed} |"
                f" <!-- {row.output_path.as_posix()} -->"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_update_report(path: Path, output_diffs: list[OutputRuleDiff]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_update_report(output_diffs), encoding="utf-8")


def build_initialized_report() -> str:
    repository = workflow_value("GITHUB_REPOSITORY", "local")
    ref_name = workflow_value("GITHUB_REF_NAME", "local")
    event_name = workflow_value("GITHUB_EVENT_NAME", "local")
    run_url = github_actions_run_url()

    lines = [
        "## 状态初始化",
        "",
        f"仓库：`{repository}`",
        f"分支：`{ref_name}`",
        f"触发：`{event_name}`",
    ]
    if run_url:
        lines.append(f"运行：[查看 GitHub Actions]({run_url})")
    lines.extend(
        [
            "",
            "未找到历史源规则基线，已初始化 GitHub Actions cache。下次运行起将推送源规则新增/删除统计。",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def write_initialized_report(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_initialized_report(), encoding="utf-8")


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
    state_path = resolve_repo_path(root, Path(args.state))
    report_path = Path(args.report) if args.report else None

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
    source_cache: dict[tuple[str, str], ParsedSource] = {}
    results: list[GroupResult] = []

    group_names = list(groups.keys())
    exclude_from_map: dict[str, list[str]] = {}
    for group_name, group_cfg in groups.items():
        if not isinstance(group_cfg, dict):
            raise ValueError(f"{group_name} 的配置必须是映射。")
        exclude_from_map[group_name] = parse_exclude_from(group_name, group_cfg)

    # 按 exclude_from 依赖拓扑排序，上游 group 先处理，便于跨组去重。
    ordered_names = topological_group_order(group_names, exclude_from_map)
    # 保留已处理 group 每个 output 名的最终规则集合，供下游排除使用。
    finalized_rules: dict[str, dict[str, set[str]]] = {}
    result_by_name: dict[str, GroupResult] = {}

    for group_name in ordered_names:
        group_cfg = groups[group_name]
        exclude_sets: dict[str, set[str]] = {}
        for upstream in exclude_from_map[group_name]:
            # 合并所有上游 group 同名 output 的最终规则，做跨组去重。
            upstream_outputs = finalized_rules.get(upstream, {})
            for output_name, rules in upstream_outputs.items():
                exclude_sets.setdefault(output_name, set()).update(rules)

        result = build_group(
            group_name=group_name,
            group_cfg=group_cfg,
            base_url=base_url,
            global_include=global_include,
            filters=filters,
            source_cache=source_cache,
            exclude_sets=exclude_sets,
        )
        result_by_name[group_name] = result
        # 存「规范值」集合，下游跨组排除时按 rule_value 比较，兼容不同 behavior。
        finalized_rules[group_name] = {
            output.name: {rule_value(rule) for rule in output.rules}
            for output in result.outputs
        }

    # 按配置原始顺序输出结果，避免拓扑排序改变产物顺序。
    results = [result_by_name[name] for name in group_names]

    failed_sources = [
        record
        for result in results
        for record in result.failed_sources
    ]
    if failed_sources:
        for record in failed_sources:
            print(format_failed_line(record), file=sys.stderr)
        return 1

    previous_state, has_source_state = load_source_state(state_path)
    next_state = build_source_state(results)
    output_diffs = build_source_diffs(previous_state, results) if has_source_state else []
    changed_outputs = [
        output
        for result in results
        for output in result.outputs
        if output_rules_changed(root, output)
    ]
    state_changed = previous_state != next_state
    report_kind = "none"
    if not has_source_state:
        report_kind = "initialized"
    elif output_diffs:
        report_kind = "updates"

    has_rule_updates = bool(output_diffs)
    has_output_changes = bool(changed_outputs)
    print_workflow_output("HAS_RULE_UPDATES", "true" if has_rule_updates else "false")
    print_workflow_output("HAS_OUTPUT_CHANGES", "true" if has_output_changes else "false")
    print_workflow_output("REPORT_KIND", report_kind)
    print_workflow_output("STATE_UPDATED", "true" if state_changed else "false")

    if not changed_outputs and not state_changed:
        print("规则无变化，跳过写入。")
        return 0

    build_time = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S %z")
    for result in results:
        for output in result.outputs:
            if output not in changed_outputs:
                continue
            write_rule_file(root / output.path, build_time, output.rules, result.success_sources, output.behavior)

    write_source_state(state_path, next_state)
    if changed_outputs:
        write_build_log(root / log_path, build_time, results)
    if report_kind == "updates" and report_path is not None:
        write_update_report(report_path, output_diffs)
    elif report_kind == "initialized" and report_path is not None:
        write_initialized_report(report_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
