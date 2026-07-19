"""`scripts/aggregate_rules.py` 的单元测试。

覆盖内置轻量 YAML 解析器、source/output 解析、规则解析与 behavior 归一化、
跨组去重、状态 diff、报告生成与构建日志等纯函数逻辑，并对网络抓取的重试
逻辑做 mock 测试。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

import aggregate_rules as ar


# ---------------------------------------------------------------------------
# strip_inline_comment
# ---------------------------------------------------------------------------
class TestStripInlineComment:
    def test_removes_trailing_comment(self):
        assert ar.strip_inline_comment("key: value  # note") == "key: value"

    def test_keeps_hash_inside_quotes(self):
        assert ar.strip_inline_comment('key: "a#b"') == 'key: "a#b"'

    def test_keeps_url_fragment(self):
        text = 'url: "https://x/y#frag"'
        assert ar.strip_inline_comment(text) == text

    def test_full_line_comment(self):
        assert ar.strip_inline_comment("# whole line") == ""

    def test_no_comment(self):
        assert ar.strip_inline_comment("plain value") == "plain value"

    def test_hash_without_preceding_space_not_comment(self):
        # '#' 紧跟非空白字符不视为注释
        assert ar.strip_inline_comment("value#notcomment") == "value#notcomment"


# ---------------------------------------------------------------------------
# parse_scalar / split_inline_items
# ---------------------------------------------------------------------------
class TestParseScalar:
    def test_plain_string(self):
        assert ar.parse_scalar("Google") == "Google"

    def test_quoted_string(self):
        assert ar.parse_scalar('"hello"') == "hello"
        assert ar.parse_scalar("'world'") == "world"

    def test_inline_list(self):
        assert ar.parse_scalar("[A, B, C]") == ["A", "B", "C"]

    def test_empty_list(self):
        assert ar.parse_scalar("[]") == []

    def test_inline_map(self):
        result = ar.parse_scalar('{path: "x.txt", include: ["$domain"]}')
        assert result == {"path": "x.txt", "include": ["$domain"]}

    def test_empty_map(self):
        assert ar.parse_scalar("{}") == {}

    def test_inline_map_missing_colon_raises(self):
        with pytest.raises(ValueError):
            ar.parse_scalar("{novalue}")

    def test_inline_map_empty_key_raises(self):
        with pytest.raises(ValueError):
            ar.parse_scalar("{: value}")


class TestSplitInlineItems:
    def test_simple(self):
        assert ar.split_inline_items("A, B, C") == ["A", "B", "C"]

    def test_nested_brackets(self):
        assert ar.split_inline_items("a, [b, c], d") == ["a", "[b, c]", "d"]

    def test_quoted_comma(self):
        assert ar.split_inline_items('"a,b", c') == ['"a,b"', "c"]

    def test_unbalanced_bracket_raises(self):
        with pytest.raises(ValueError):
            ar.split_inline_items("a]")

    def test_unclosed_quote_raises(self):
        with pytest.raises(ValueError):
            ar.split_inline_items('"unterminated')

    def test_unclosed_bracket_raises(self):
        with pytest.raises(ValueError):
            ar.split_inline_items("[a, b")


# ---------------------------------------------------------------------------
# load_yaml_subset (内置解析器)
# ---------------------------------------------------------------------------
class TestLoadYamlSubset:
    def _write(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "cfg.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_nested_mapping_and_lists(self, tmp_path):
        text = (
            "base:\n"
            "  blackmatrix7_raw: \"https://example.com/root\"\n"
            "filters:\n"
            "  domain: [DOMAIN, DOMAIN-SUFFIX]\n"
            "groups:\n"
            "  Google:\n"
            "    outputs:\n"
            "      non_ip: {path: \"out.txt\", include: [\"$domain\"]}\n"
            "    sources: [Google, Chromecast]\n"
        )
        cfg = ar.load_yaml_subset(self._write(tmp_path, text))
        assert cfg["base"]["blackmatrix7_raw"] == "https://example.com/root"
        assert cfg["filters"]["domain"] == ["DOMAIN", "DOMAIN-SUFFIX"]
        assert cfg["groups"]["Google"]["sources"] == ["Google", "Chromecast"]
        assert cfg["groups"]["Google"]["outputs"]["non_ip"]["path"] == "out.txt"

    def test_block_list_of_maps(self, tmp_path):
        text = (
            "sources:\n"
            "  - Google\n"
            "  - {name: ChinaIP, behavior: ipcidr}\n"
        )
        cfg = ar.load_yaml_subset(self._write(tmp_path, text))
        assert cfg["sources"][0] == "Google"
        assert cfg["sources"][1] == {"name": "ChinaIP", "behavior": "ipcidr"}

    def test_empty_file_returns_empty_dict(self, tmp_path):
        assert ar.load_yaml_subset(self._write(tmp_path, "\n# only comment\n")) == {}

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ar.load_yaml_subset(tmp_path / "nope.yaml")

    def test_tab_indent_raises(self, tmp_path):
        with pytest.raises(ValueError):
            ar.load_yaml_subset(self._write(tmp_path, "key:\n\tvalue: 1\n"))

    def test_invalid_key_line_raises(self, tmp_path):
        with pytest.raises(ValueError):
            ar.load_yaml_subset(self._write(tmp_path, "novalue\n"))

    def test_comments_and_blank_lines_ignored(self, tmp_path):
        text = "# header\n\nkey: value  # trailing\n"
        assert ar.load_yaml_subset(self._write(tmp_path, text)) == {"key": "value"}


# ---------------------------------------------------------------------------
# get_setting
# ---------------------------------------------------------------------------
class TestGetSetting:
    def test_nested_lookup(self):
        data = {"a": {"b": {"c": 1}}}
        assert ar.get_setting(data, "a", "b", "c") == 1

    def test_missing_returns_default(self):
        assert ar.get_setting({"a": 1}, "a", "b", default="x") == "x"

    def test_non_dict_intermediate_returns_default(self):
        assert ar.get_setting({"a": 5}, "a", "b", default=None) is None


# ---------------------------------------------------------------------------
# normalize_source
# ---------------------------------------------------------------------------
class TestNormalizeSource:
    def test_plain_name_expands(self):
        assert (
            ar.normalize_source("Google", "https://base/root/")
            == "https://base/root/Google/Google.list"
        )

    def test_full_url_kept_and_stripped(self):
        assert ar.normalize_source("https://x/y/", "https://base") == "https://x/y"

    def test_empty_raises_with_group_hint(self):
        with pytest.raises(ValueError, match="MyGroup.sources"):
            ar.normalize_source("   ", "https://base", "MyGroup")


# ---------------------------------------------------------------------------
# normalize_behavior
# ---------------------------------------------------------------------------
class TestNormalizeBehavior:
    def test_default_none(self):
        assert ar.normalize_behavior(None, "f") == ar.CLASSICAL

    def test_lowercased(self):
        assert ar.normalize_behavior("IPCIDR", "f") == ar.IPCIDR

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            ar.normalize_behavior("weird", "f")

    def test_non_string_raises(self):
        with pytest.raises(ValueError):
            ar.normalize_behavior(123, "f")


# ---------------------------------------------------------------------------
# parse_cidr_line / parse_domain_line
# ---------------------------------------------------------------------------
class TestParseCidrLine:
    def test_ipv4(self):
        assert ar.parse_cidr_line("1.2.3.0/24") == ("IP-CIDR", "IP-CIDR,1.2.3.0/24")

    def test_ipv6(self):
        assert ar.parse_cidr_line("2001:db8::/32") == ("IP-CIDR6", "IP-CIDR6,2001:db8::/32")

    def test_strips_trailing_option(self):
        assert ar.parse_cidr_line("1.2.3.0/24,no-resolve") == ("IP-CIDR", "IP-CIDR,1.2.3.0/24")

    def test_empty_returns_none(self):
        assert ar.parse_cidr_line("") is None

    def test_space_returns_none(self):
        assert ar.parse_cidr_line("1.2.3.0 /24") is None


class TestParseDomainLine:
    def test_plain(self):
        assert ar.parse_domain_line("example.com") == ("DOMAIN-SUFFIX", "DOMAIN-SUFFIX,example.com")

    def test_suffix_prefix_stripped(self):
        assert ar.parse_domain_line("+.example.com") == ("DOMAIN-SUFFIX", "DOMAIN-SUFFIX,example.com")

    def test_empty_returns_none(self):
        assert ar.parse_domain_line("") is None

    def test_only_prefix_returns_none(self):
        assert ar.parse_domain_line("+.") is None

    def test_contains_comma_returns_none(self):
        assert ar.parse_domain_line("a.com,x") is None


# ---------------------------------------------------------------------------
# rule_value / emit_rule_for_behavior / restore_rule_from_line
# ---------------------------------------------------------------------------
class TestRuleValueAndBehavior:
    def test_rule_value_strips_type(self):
        assert ar.rule_value("IP-CIDR,1.2.3.0/24") == "1.2.3.0/24"
        assert ar.rule_value("DOMAIN-SUFFIX,google.com") == "google.com"

    def test_rule_value_no_comma(self):
        assert ar.rule_value("SINGLE") == "SINGLE"

    def test_emit_classical(self):
        assert ar.emit_rule_for_behavior("IP-CIDR,1.2.3.0/24", ar.CLASSICAL) == "IP-CIDR,1.2.3.0/24"

    def test_emit_ipcidr_strips_prefix(self):
        assert ar.emit_rule_for_behavior("IP-CIDR,1.2.3.0/24", ar.IPCIDR) == "1.2.3.0/24"

    def test_emit_domain_strips_prefix(self):
        assert ar.emit_rule_for_behavior("DOMAIN-SUFFIX,x.com", ar.DOMAIN) == "x.com"

    def test_restore_classical(self):
        assert ar.restore_rule_from_line("IP-CIDR,1.2.3.0/24", ar.CLASSICAL) == "IP-CIDR,1.2.3.0/24"

    def test_restore_ipcidr(self):
        assert ar.restore_rule_from_line("1.2.3.0/24", ar.IPCIDR) == "IP-CIDR,1.2.3.0/24"

    def test_restore_domain(self):
        assert ar.restore_rule_from_line("x.com", ar.DOMAIN) == "DOMAIN-SUFFIX,x.com"

    def test_restore_blank_returns_none(self):
        assert ar.restore_rule_from_line("   ", ar.CLASSICAL) is None

    def test_restore_ipcidr_invalid_returns_none(self):
        assert ar.restore_rule_from_line("not a cidr", ar.IPCIDR) is None


# ---------------------------------------------------------------------------
# parse_rule_line / parse_source_text
# ---------------------------------------------------------------------------
class TestParseRuleLine:
    def test_basic_uppercases_type(self):
        assert ar.parse_rule_line("domain-suffix,google.com") == (
            "DOMAIN-SUFFIX",
            "DOMAIN-SUFFIX,google.com",
        )

    def test_comment_returns_none(self):
        assert ar.parse_rule_line("# comment") is None

    def test_blank_returns_none(self):
        assert ar.parse_rule_line("   ") is None

    def test_single_field_returns_none(self):
        assert ar.parse_rule_line("JUSTONE") is None

    def test_extra_fields_preserved(self):
        assert ar.parse_rule_line("IP-CIDR,1.2.3.0/24,no-resolve") == (
            "IP-CIDR",
            "IP-CIDR,1.2.3.0/24,no-resolve",
        )


class TestParseSourceText:
    def test_classical(self):
        text = "# header\nDOMAIN,a.com\nIP-CIDR,1.2.3.0/24\n\n"
        parsed = ar.parse_source_text(text, "u")
        assert parsed.resolved_url == "u"
        assert parsed.behavior == ar.CLASSICAL
        assert parsed.rules == [("DOMAIN", "DOMAIN,a.com"), ("IP-CIDR", "IP-CIDR,1.2.3.0/24")]

    def test_ipcidr_behavior(self):
        parsed = ar.parse_source_text("1.2.3.0/24\n2001:db8::/32\n", "u", ar.IPCIDR)
        assert parsed.rules == [
            ("IP-CIDR", "IP-CIDR,1.2.3.0/24"),
            ("IP-CIDR6", "IP-CIDR6,2001:db8::/32"),
        ]

    def test_domain_behavior(self):
        parsed = ar.parse_source_text("example.com\n+.foo.com\n", "u", ar.DOMAIN)
        assert parsed.rules == [
            ("DOMAIN-SUFFIX", "DOMAIN-SUFFIX,example.com"),
            ("DOMAIN-SUFFIX", "DOMAIN-SUFFIX,foo.com"),
        ]

    def test_invalid_lines_skipped(self):
        parsed = ar.parse_source_text("badline\nDOMAIN,ok.com\n", "u")
        assert parsed.rules == [("DOMAIN", "DOMAIN,ok.com")]


# ---------------------------------------------------------------------------
# normalize_rule_types
# ---------------------------------------------------------------------------
class TestNormalizeRuleTypes:
    def test_default_when_none(self):
        assert ar.normalize_rule_types(None, "f", {"IP-CIDR"}) == {"IP-CIDR"}

    def test_uppercases(self):
        assert ar.normalize_rule_types(["domain"], "f", set()) == {"DOMAIN"}

    def test_wildcard_kept(self):
        assert ar.normalize_rule_types(["*"], "f", set()) == {"*"}

    def test_filter_reference_expands(self):
        filters = {"domain": {"DOMAIN", "DOMAIN-SUFFIX"}}
        assert ar.normalize_rule_types(["$domain"], "f", set(), filters) == {"DOMAIN", "DOMAIN-SUFFIX"}

    def test_unknown_filter_raises(self):
        with pytest.raises(ValueError):
            ar.normalize_rule_types(["$missing"], "f", set(), {})

    def test_non_list_raises(self):
        with pytest.raises(ValueError):
            ar.normalize_rule_types("x", "f", set())

    def test_empty_item_raises(self):
        with pytest.raises(ValueError):
            ar.normalize_rule_types(["  "], "f", set())


# ---------------------------------------------------------------------------
# parse_filters
# ---------------------------------------------------------------------------
class TestParseFilters:
    def test_parses(self):
        cfg = {"filters": {"domain": ["DOMAIN"], "ip": ["IP-CIDR", "IP-CIDR6"]}}
        filters = ar.parse_filters(cfg)
        assert filters == {"domain": {"DOMAIN"}, "ip": {"IP-CIDR", "IP-CIDR6"}}

    def test_missing_returns_empty(self):
        assert ar.parse_filters({}) == {}

    def test_none_returns_empty(self):
        assert ar.parse_filters({"filters": None}) == {}

    def test_non_dict_raises(self):
        with pytest.raises(ValueError):
            ar.parse_filters({"filters": ["x"]})


# ---------------------------------------------------------------------------
# parse_exclude_from
# ---------------------------------------------------------------------------
class TestParseExcludeFrom:
    def test_default_empty(self):
        assert ar.parse_exclude_from("G", {}) == []

    def test_none_empty(self):
        assert ar.parse_exclude_from("G", {"exclude_from": None}) == []

    def test_list(self):
        assert ar.parse_exclude_from("G", {"exclude_from": ["A", "B"]}) == ["A", "B"]

    def test_non_list_raises(self):
        with pytest.raises(ValueError):
            ar.parse_exclude_from("G", {"exclude_from": "A"})

    def test_empty_item_raises(self):
        with pytest.raises(ValueError):
            ar.parse_exclude_from("G", {"exclude_from": [" "]})


# ---------------------------------------------------------------------------
# topological_group_order
# ---------------------------------------------------------------------------
class TestTopologicalGroupOrder:
    def test_upstream_before_downstream(self):
        order = ar.topological_group_order(["B", "A"], {"B": ["A"], "A": []})
        assert order.index("A") < order.index("B")

    def test_no_deps_preserves(self):
        assert ar.topological_group_order(["A", "B"], {"A": [], "B": []}) == ["A", "B"]

    def test_missing_dep_raises(self):
        with pytest.raises(ValueError, match="不存在"):
            ar.topological_group_order(["A"], {"A": ["X"]})

    def test_cycle_raises(self):
        with pytest.raises(ValueError, match="循环"):
            ar.topological_group_order(["A", "B"], {"A": ["B"], "B": ["A"]})


# ---------------------------------------------------------------------------
# parse_outputs
# ---------------------------------------------------------------------------
class TestParseOutputs:
    def test_basic(self):
        cfg = {"outputs": {"non_ip": {"path": "o.txt", "include": ["DOMAIN"]}}}
        specs = ar.parse_outputs("G", cfg, {"DOMAIN"}, {})
        assert len(specs) == 1
        assert specs[0].name == "non_ip"
        assert specs[0].path == Path("o.txt")
        assert specs[0].behavior == ar.CLASSICAL
        assert specs[0].include == {"DOMAIN"}

    def test_type_behavior(self):
        cfg = {"outputs": {"ip": {"path": "o.txt", "type": "ipcidr"}}}
        specs = ar.parse_outputs("G", cfg, {"IP-CIDR"}, {})
        assert specs[0].behavior == ar.IPCIDR

    def test_missing_outputs_raises(self):
        with pytest.raises(ValueError):
            ar.parse_outputs("G", {}, set(), {})

    def test_missing_path_raises(self):
        with pytest.raises(ValueError):
            ar.parse_outputs("G", {"outputs": {"x": {}}}, set(), {})

    def test_output_not_mapping_raises(self):
        with pytest.raises(ValueError):
            ar.parse_outputs("G", {"outputs": {"x": "str"}}, set(), {})


# ---------------------------------------------------------------------------
# parse_sources
# ---------------------------------------------------------------------------
class TestParseSources:
    BASE = "https://base/root"

    def test_shorthand_string(self):
        specs = ar.parse_sources("G", {"sources": ["Google"]}, self.BASE)
        assert specs[0].name == "Google"
        assert specs[0].resolved_url == "https://base/root/Google/Google.list"
        assert specs[0].behavior == ar.CLASSICAL

    def test_object_name(self):
        specs = ar.parse_sources("G", {"sources": [{"name": "China", "behavior": "ipcidr"}]}, self.BASE)
        assert specs[0].name == "China"
        assert specs[0].behavior == ar.IPCIDR
        assert specs[0].resolved_url == "https://base/root/China/China.list"

    def test_object_url(self):
        specs = ar.parse_sources("G", {"sources": [{"url": "https://x/y.txt", "behavior": "domain"}]}, self.BASE)
        assert specs[0].resolved_url == "https://x/y.txt"
        assert specs[0].name == "https://x/y.txt"
        assert specs[0].behavior == ar.DOMAIN

    def test_object_url_with_name(self):
        specs = ar.parse_sources("G", {"sources": [{"url": "https://x/y.txt", "name": "Custom"}]}, self.BASE)
        assert specs[0].name == "Custom"

    def test_non_list_raises(self):
        with pytest.raises(ValueError):
            ar.parse_sources("G", {"sources": "x"}, self.BASE)

    def test_empty_string_source_raises(self):
        with pytest.raises(ValueError):
            ar.parse_sources("G", {"sources": ["  "]}, self.BASE)

    def test_object_missing_name_and_url_raises(self):
        with pytest.raises(ValueError):
            ar.parse_sources("G", {"sources": [{"behavior": "classical"}]}, self.BASE)

    def test_invalid_item_type_raises(self):
        with pytest.raises(ValueError):
            ar.parse_sources("G", {"sources": [123]}, self.BASE)


# ---------------------------------------------------------------------------
# rule_matches_output
# ---------------------------------------------------------------------------
class TestRuleMatchesOutput:
    def _out(self, include, exclude=None):
        return ar.OutputResult(
            name="o", path=Path("o.txt"), behavior=ar.CLASSICAL,
            include=set(include), exclude=set(exclude or []),
        )

    def test_included(self):
        assert ar.rule_matches_output("DOMAIN", self._out({"DOMAIN"}))

    def test_wildcard(self):
        assert ar.rule_matches_output("ANY", self._out({"*"}))

    def test_excluded(self):
        assert not ar.rule_matches_output("DOMAIN", self._out({"*"}, {"DOMAIN"}))

    def test_not_included(self):
        assert not ar.rule_matches_output("IP-CIDR", self._out({"DOMAIN"}))


# ---------------------------------------------------------------------------
# build_group (mock fetch)
# ---------------------------------------------------------------------------
class TestBuildGroup:
    def _cfg(self, sources):
        return {
            "outputs": {
                "non_ip": {"path": "non_ip/g.txt", "include": ["DOMAIN", "DOMAIN-SUFFIX"]},
                "ip": {"path": "ip/g.txt", "include": ["IP-CIDR"]},
            },
            "sources": sources,
        }

    def test_aggregates_and_routes_by_type(self, monkeypatch):
        def fake_fetch(url, timeout=30):
            return "DOMAIN,a.com\nIP-CIDR,1.2.3.0/24\n"

        monkeypatch.setattr(ar, "fetch_url_text", fake_fetch)
        result = ar.build_group("G", self._cfg(["Google"]), "https://b", {"*"}, {}, {})
        outputs = {o.name: o for o in result.outputs}
        assert outputs["non_ip"].rules == ["DOMAIN,a.com"]
        assert outputs["ip"].rules == ["IP-CIDR,1.2.3.0/24"]
        assert len(result.success_sources) == 1
        assert result.failed_sources == []

    def test_duplicate_source_recorded(self, monkeypatch):
        monkeypatch.setattr(ar, "fetch_url_text", lambda u, timeout=30: "DOMAIN,a.com\n")
        result = ar.build_group("G", self._cfg(["Google", "Google"]), "https://b", {"*"}, {}, {})
        assert len(result.duplicate_sources) == 1

    def test_failed_source_recorded(self, monkeypatch):
        def boom(url, timeout=30):
            raise RuntimeError("network down")

        monkeypatch.setattr(ar, "fetch_url_text", boom)
        result = ar.build_group("G", self._cfg(["Google"]), "https://b", {"*"}, {}, {})
        assert len(result.failed_sources) == 1
        assert result.failed_sources[0].error == "network down"

    def test_cache_reused_across_calls(self, monkeypatch):
        calls = {"n": 0}

        def counting(url, timeout=30):
            calls["n"] += 1
            return "DOMAIN,a.com\n"

        monkeypatch.setattr(ar, "fetch_url_text", counting)
        cache: dict = {}
        ar.build_group("G1", self._cfg(["Google"]), "https://b", {"*"}, {}, cache)
        result2 = ar.build_group("G2", self._cfg(["Google"]), "https://b", {"*"}, {}, cache)
        assert calls["n"] == 1
        assert result2.success_sources[0].cached is True

    def test_cross_group_exclude(self, monkeypatch):
        monkeypatch.setattr(ar, "fetch_url_text", lambda u, timeout=30: "DOMAIN,a.com\nDOMAIN,b.com\n")
        exclude = {"non_ip": {"a.com"}}
        result = ar.build_group("G", self._cfg(["Google"]), "https://b", {"*"}, {}, {}, exclude_sets=exclude)
        outputs = {o.name: o for o in result.outputs}
        assert outputs["non_ip"].rules == ["DOMAIN,b.com"]


# ---------------------------------------------------------------------------
# format_rule_file / write_rule_file / read_existing_rules / output_rules_changed
# ---------------------------------------------------------------------------
class TestRuleFileIO:
    def test_format_classical(self):
        records = [ar.SourceRecord(source="Google", resolved_url="https://x", status="success")]
        out = ar.format_rule_file("2026-01-01", ["DOMAIN,a.com"], records)
        assert "# Rule Count: 1" in out
        assert "#   - Google: https://x" in out
        assert out.rstrip().endswith("DOMAIN,a.com")

    def test_format_no_sources(self):
        out = ar.format_rule_file("t", [], [])
        assert "#   - none" in out

    def test_format_ipcidr_strips_prefix(self):
        out = ar.format_rule_file("t", ["IP-CIDR,1.2.3.0/24"], [], ar.IPCIDR)
        assert "\n1.2.3.0/24" in out
        assert "IP-CIDR,1.2.3.0/24" not in out.split("\n\n", 1)[-1]

    def test_write_and_read_roundtrip(self, tmp_path):
        path = tmp_path / "sub" / "o.txt"
        ar.write_rule_file(path, "t", ["DOMAIN,a.com"], [])
        assert ar.read_existing_rules(path) == ["DOMAIN,a.com"]

    def test_read_missing_returns_none(self, tmp_path):
        assert ar.read_existing_rules(tmp_path / "nope.txt") is None

    def test_read_ipcidr_restores(self, tmp_path):
        path = tmp_path / "o.txt"
        ar.write_rule_file(path, "t", ["IP-CIDR,1.2.3.0/24"], [], ar.IPCIDR)
        assert ar.read_existing_rules(path, ar.IPCIDR) == ["IP-CIDR,1.2.3.0/24"]

    def test_output_rules_changed(self, tmp_path):
        out = ar.OutputResult(name="o", path=Path("o.txt"), behavior=ar.CLASSICAL,
                              include={"*"}, exclude=set(), rules=["DOMAIN,a.com"])
        assert ar.output_rules_changed(tmp_path, out) is True
        ar.write_rule_file(tmp_path / "o.txt", "t", ["DOMAIN,a.com"], [])
        assert ar.output_rules_changed(tmp_path, out) is False


# ---------------------------------------------------------------------------
# source state: load/build/write + diffs
# ---------------------------------------------------------------------------
class TestSourceState:
    def _result_with_rules(self):
        out = ar.OutputResult(
            name="non_ip", path=Path("non_ip/g.txt"), behavior=ar.CLASSICAL,
            include={"*"}, exclude=set(),
            rules=["DOMAIN,a.com", "DOMAIN,b.com"],
            source_rules={"Google": ["DOMAIN,a.com", "DOMAIN,b.com"]},
        )
        return ar.GroupResult(name="G", outputs=[out])

    def test_empty_state(self):
        assert ar.empty_source_state() == {"version": ar.SOURCE_STATE_VERSION, "groups": {}}

    def test_load_missing(self, tmp_path):
        state, existed = ar.load_source_state(tmp_path / "s.json")
        assert existed is False
        assert state["groups"] == {}

    def test_load_valid(self, tmp_path):
        path = tmp_path / "s.json"
        ar.write_source_state(path, ar.build_source_state([self._result_with_rules()]))
        state, existed = ar.load_source_state(path)
        assert existed is True
        assert "G" in state["groups"]

    def test_load_wrong_version_treated_as_empty(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"version": 999, "groups": {"X": {}}}), encoding="utf-8")
        state, existed = ar.load_source_state(path)
        assert existed is False

    def test_load_non_dict_raises(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with pytest.raises(ValueError):
            ar.load_source_state(path)

    def test_build_source_state_shape(self):
        state = ar.build_source_state([self._result_with_rules()])
        sources = state["groups"]["G"]["outputs"]["non_ip"]["sources"]
        assert sources["Google"] == ["DOMAIN,a.com", "DOMAIN,b.com"]

    def test_get_previous_source_rules(self):
        state = ar.build_source_state([self._result_with_rules()])
        rules = ar.get_previous_source_rules(state, "G", "non_ip", "Google")
        assert set(rules) == {"DOMAIN,a.com", "DOMAIN,b.com"}

    def test_get_previous_source_rules_missing(self):
        assert ar.get_previous_source_rules({}, "G", "o", "s") == []

    def test_get_previous_source_names(self):
        state = ar.build_source_state([self._result_with_rules()])
        assert ar.get_previous_source_names(state, "G", "non_ip") == {"Google"}

    def test_build_source_diffs_added_removed(self):
        previous = ar.build_source_state([self._result_with_rules()])
        # 新结果：删除 b.com，新增 c.com
        out = ar.OutputResult(
            name="non_ip", path=Path("non_ip/g.txt"), behavior=ar.CLASSICAL,
            include={"*"}, exclude=set(),
            rules=["DOMAIN,a.com", "DOMAIN,c.com"],
            source_rules={"Google": ["DOMAIN,a.com", "DOMAIN,c.com"]},
        )
        results = [ar.GroupResult(name="G", outputs=[out])]
        diffs = ar.build_source_diffs(previous, results)
        assert len(diffs) == 1
        row = diffs[0].source_diffs[0]
        assert row.added == 1
        assert row.removed == 1

    def test_build_source_diffs_no_change(self):
        previous = ar.build_source_state([self._result_with_rules()])
        diffs = ar.build_source_diffs(previous, [self._result_with_rules()])
        assert diffs == []


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------
class TestReports:
    def _diffs(self):
        return [
            ar.OutputRuleDiff(
                group_name="Google", output_name="non_ip", output_path=Path("non_ip/google.txt"),
                source_diffs=[ar.SourceRuleDiff(source="Google", added=2, removed=1)],
            ),
            ar.OutputRuleDiff(
                group_name="Google", output_name="ip", output_path=Path("ip/google.txt"),
                source_diffs=[ar.SourceRuleDiff(source="Google", added=1, removed=0)],
            ),
        ]

    def test_markdown_escape(self):
        assert ar.markdown_escape_table_cell("a|b") == "a\\|b"

    def test_group_output_diffs_merges(self):
        grouped = ar.group_output_diffs(self._diffs())
        assert len(grouped) == 1
        assert len(grouped[0].rows) == 2

    def test_build_update_report(self, monkeypatch):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
        report = ar.build_update_report(self._diffs())
        assert "## 规则更新" in report
        assert "### Google" in report
        assert "| `Google` | non_ip | 2 | 1 |" in report

    def test_build_update_report_with_run_url(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
        monkeypatch.setenv("GITHUB_RUN_ID", "42")
        report = ar.build_update_report(self._diffs())
        assert "https://github.com/o/r/actions/runs/42" in report

    def test_write_update_report(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        path = tmp_path / "r.md"
        ar.write_update_report(path, self._diffs())
        assert path.read_text(encoding="utf-8").startswith("## 规则更新")

    def test_build_initialized_report(self, monkeypatch):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        report = ar.build_initialized_report()
        assert "## 状态初始化" in report

    def test_write_initialized_report(self, tmp_path):
        path = tmp_path / "r.md"
        ar.write_initialized_report(path)
        assert "状态初始化" in path.read_text(encoding="utf-8")

    def test_github_actions_run_url_missing(self, monkeypatch):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
        assert ar.github_actions_run_url() == ""

    def test_workflow_value_default(self, monkeypatch):
        monkeypatch.delenv("SOME_VAR", raising=False)
        assert ar.workflow_value("SOME_VAR", "d") == "d"


# ---------------------------------------------------------------------------
# build log
# ---------------------------------------------------------------------------
class TestBuildLog:
    def test_write_build_log(self, tmp_path):
        out = ar.OutputResult(name="non_ip", path=Path("non_ip/g.txt"), behavior=ar.CLASSICAL,
                              include={"*"}, exclude=set(), rules=["DOMAIN,a.com"])
        result = ar.GroupResult(
            name="G", outputs=[out],
            success_sources=[ar.SourceRecord(source="Google", resolved_url="https://x", status="success")],
            failed_sources=[ar.SourceRecord(source="Bad", resolved_url="https://y", status="failed", error="boom")],
            duplicate_sources=[ar.SourceRecord(source="Dup", resolved_url="https://z", status="duplicate")],
        )
        path = tmp_path / "log.md"
        ar.write_build_log(path, "2026-01-01", [result])
        text = path.read_text(encoding="utf-8")
        assert "## G" in text
        assert "1 条" in text
        assert "boom" in text
        assert "Dup" in text

    def test_format_source_line_cached(self):
        rec = ar.SourceRecord(source="G", resolved_url="https://x", status="success", cached=True)
        assert "（复用缓存）" in ar.format_source_line(rec)

    def test_format_failed_line(self):
        rec = ar.SourceRecord(source="G", resolved_url="https://x", status="failed", error="e")
        assert "（e）" in ar.format_failed_line(rec)


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------
class TestLoadConfig:
    def test_load_config_valid(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("base:\n  blackmatrix7_raw: \"https://x\"\n", encoding="utf-8")
        cfg = ar.load_config(path)
        assert cfg["base"]["blackmatrix7_raw"] == "https://x"

    def test_load_config_non_dict_raises(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError):
            ar.load_config(path)


# ---------------------------------------------------------------------------
# fetch_url_text 重试逻辑 (_backoff_seconds)
# ---------------------------------------------------------------------------
class TestFetchRetry:
    def test_backoff_respects_retry_after(self):
        assert ar._backoff_seconds(1, "5") == 5.0

    def test_backoff_retry_after_floor(self):
        assert ar._backoff_seconds(1, "0.1") == 1.0

    def test_backoff_invalid_retry_after_falls_back(self):
        val = ar._backoff_seconds(1, "notnum")
        assert val >= 2.0

    def test_backoff_exponential_range(self):
        val = ar._backoff_seconds(2)
        assert 4.0 <= val <= 6.0

    def test_fetch_success(self, monkeypatch):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return "DOMAIN,a.com".encode("utf-8")

        monkeypatch.setattr(ar, "urlopen", lambda req, timeout=30: FakeResp())
        assert ar.fetch_url_text("https://x") == "DOMAIN,a.com"

    def test_fetch_retries_then_succeeds(self, monkeypatch):
        calls = {"n": 0}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"ok"

        def flaky(req, timeout=30):
            calls["n"] += 1
            if calls["n"] == 1:
                raise HTTPError("https://x", 503, "busy", {}, None)
            return FakeResp()

        monkeypatch.setattr(ar, "urlopen", flaky)
        monkeypatch.setattr(ar.time, "sleep", lambda s: None)
        assert ar.fetch_url_text("https://x") == "ok"
        assert calls["n"] == 2

    def test_fetch_non_retryable_raises_immediately(self, monkeypatch):
        def not_found(req, timeout=30):
            raise HTTPError("https://x", 404, "missing", {}, None)

        monkeypatch.setattr(ar, "urlopen", not_found)
        with pytest.raises(RuntimeError, match="404"):
            ar.fetch_url_text("https://x")

    def test_fetch_urlerror_retries_and_exhausts(self, monkeypatch):
        def down(req, timeout=30):
            raise URLError("dns fail")

        monkeypatch.setattr(ar, "urlopen", down)
        monkeypatch.setattr(ar.time, "sleep", lambda s: None)
        with pytest.raises(RuntimeError):
            ar.fetch_url_text("https://x")


# ---------------------------------------------------------------------------
# repo_root / resolve_repo_path / parse_args
# ---------------------------------------------------------------------------
class TestMisc:
    def test_repo_root_points_to_repo(self):
        assert (ar.repo_root() / "scripts" / "aggregate_rules.py").exists()

    def test_resolve_repo_path_relative(self):
        root = Path("/tmp/root")
        assert ar.resolve_repo_path(root, Path("a/b")) == root / "a/b"

    def test_resolve_repo_path_absolute(self):
        p = Path("/abs/path")
        assert ar.resolve_repo_path(Path("/tmp/root"), p) == p

    def test_parse_args_defaults(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["prog"])
        args = ar.parse_args()
        assert args.config == "rule/rule-aggregate.yaml"
        assert args.state == ar.DEFAULT_SOURCE_STATE_PATH
        assert args.report == ""


# ---------------------------------------------------------------------------
# main() 端到端流程 (mock 网络 + 临时仓库根)
# ---------------------------------------------------------------------------
class TestMain:
    CONFIG = (
        "base:\n"
        "  blackmatrix7_raw: \"https://base/root\"\n"
        "log:\n"
        "  path: \"rule/list/build-log.md\"\n"
        "filters:\n"
        "  domain: [DOMAIN, DOMAIN-SUFFIX]\n"
        "  ip: [IP-CIDR]\n"
        "groups:\n"
        "  Google:\n"
        "    outputs:\n"
        "      non_ip: {path: \"rule/list/non_ip/google.txt\", include: [\"$domain\"]}\n"
        "      ip: {path: \"rule/list/ip/google.txt\", include: [\"$ip\"]}\n"
        "    sources: [Google]\n"
    )

    def _setup(self, tmp_path, monkeypatch, argv):
        root = tmp_path
        (root / "rule").mkdir(parents=True, exist_ok=True)
        config_path = root / "config.yaml"
        config_path.write_text(self.CONFIG, encoding="utf-8")
        monkeypatch.setattr(ar, "repo_root", lambda: root)
        monkeypatch.setattr(
            ar, "fetch_url_text",
            lambda url, timeout=30: "DOMAIN,a.com\nIP-CIDR,1.2.3.0/24\n",
        )
        monkeypatch.setattr("sys.argv", argv)
        return root

    def test_first_run_initializes_and_writes(self, tmp_path, monkeypatch, capsys):
        state = tmp_path / "state.json"
        report = tmp_path / "report.md"
        root = self._setup(
            tmp_path, monkeypatch,
            ["prog", "--config", "config.yaml", "--state", str(state), "--report", str(report)],
        )
        assert ar.main() == 0
        out = capsys.readouterr().out
        assert "REPORT_KIND=initialized" in out
        assert (root / "rule/list/non_ip/google.txt").exists()
        assert (root / "rule/list/ip/google.txt").exists()
        assert state.exists()
        assert "状态初始化" in report.read_text(encoding="utf-8")

    def test_no_change_second_run_skips(self, tmp_path, monkeypatch, capsys):
        state = tmp_path / "state.json"
        argv = ["prog", "--config", "config.yaml", "--state", str(state)]
        self._setup(tmp_path, monkeypatch, argv)
        assert ar.main() == 0
        capsys.readouterr()
        # 第二次运行：无变化
        assert ar.main() == 0
        out = capsys.readouterr().out
        assert "规则无变化，跳过写入。" in out

    def test_updates_report_on_change(self, tmp_path, monkeypatch, capsys):
        state = tmp_path / "state.json"
        report = tmp_path / "report.md"
        argv = ["prog", "--config", "config.yaml", "--state", str(state), "--report", str(report)]
        self._setup(tmp_path, monkeypatch, argv)
        assert ar.main() == 0
        capsys.readouterr()
        # 源规则新增一条，再次运行应产出 updates 报告
        monkeypatch.setattr(
            ar, "fetch_url_text",
            lambda url, timeout=30: "DOMAIN,a.com\nDOMAIN,new.com\nIP-CIDR,1.2.3.0/24\n",
        )
        assert ar.main() == 0
        out = capsys.readouterr().out
        assert "HAS_RULE_UPDATES=true" in out
        assert "REPORT_KIND=updates" in out
        assert "规则更新" in report.read_text(encoding="utf-8")

    def test_failed_source_returns_1(self, tmp_path, monkeypatch, capsys):
        state = tmp_path / "state.json"
        self._setup(
            tmp_path, monkeypatch,
            ["prog", "--config", "config.yaml", "--state", str(state)],
        )

        def boom(url, timeout=30):
            raise RuntimeError("boom")

        monkeypatch.setattr(ar, "fetch_url_text", boom)
        assert ar.main() == 1
        err = capsys.readouterr().err
        assert "boom" in err

    def test_missing_base_url_raises(self, tmp_path, monkeypatch):
        root = tmp_path
        (root / "config.yaml").write_text("groups:\n  G:\n    sources: [X]\n", encoding="utf-8")
        monkeypatch.setattr(ar, "repo_root", lambda: root)
        monkeypatch.setattr("sys.argv", ["prog", "--config", "config.yaml", "--state", str(tmp_path / "s.json")])
        with pytest.raises(ValueError, match="base.blackmatrix7_raw"):
            ar.main()

    def test_empty_groups_raises(self, tmp_path, monkeypatch):
        root = tmp_path
        (root / "config.yaml").write_text(
            "base:\n  blackmatrix7_raw: \"https://x\"\ngroups: {}\n", encoding="utf-8"
        )
        monkeypatch.setattr(ar, "repo_root", lambda: root)
        monkeypatch.setattr("sys.argv", ["prog", "--config", "config.yaml", "--state", str(tmp_path / "s.json")])
        with pytest.raises(ValueError, match="groups"):
            ar.main()
