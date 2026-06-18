# 规则聚合

## 快速使用

1. 修改 `rule/rule-aggregate.yaml`。
2. 运行 `python scripts/aggregate_rules.py --config rule/rule-aggregate.yaml`。
3. 查看 `rule/list/build-log.md`。

## 配置格式

```yaml
# 简写源的上游根地址。
base:
  blackmatrix7_raw: "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash"

# 编译日志输出位置。
log:
  path: "rule/list/build-log.md"

# 可复用的规则类型集合。
filters:
  domain: [DOMAIN, DOMAIN-SUFFIX, DOMAIN-KEYWORD, DOMAIN-WILDCARD, DOMAIN-REGEX]
  ip: [IP-CIDR, IP-CIDR6, IP-SUFFIX, IP-ASN]

# 全局默认收录类型。
# 某个输出文件没有写 include 时，会使用这里的配置。
include:
  - "$domain"

# 每个一级键都是一个聚合组。
groups:
  # 聚合组名，自行定义。
  GlobalMedia:
    # 分组默认收录类型。
    # 会覆盖全局 include。
    include:
      - "$domain"
    # 一个聚合组可以生成多个输出文件。
    outputs:
      # 输出名称，自行定义。
      non_ip:
        # 输出文件路径。
        path: "rule/list/non_ip/GlobalMedia_NON_IP.txt"
        # 输出文件收录类型。
        # 会覆盖分组 include 和全局 include。
        # 不写时使用分组 include。
        include: ["$domain"]
        # 排除 IP 类规则。
        exclude:
          - IP-CIDR
          - IP-CIDR6
          - IP-ASN
      ip:
        path: "rule/list/ip/GlobalMedia_ip.txt"
        # 只收录 IP 类规则。
        include:
          - "$ip"
    # 本聚合组要引入的上游规则。
    sources:
      # 简写：展开为 base.blackmatrix7_raw/YouTube/YouTube.list。
      - YouTube
      - YouTubeMusic
      - AppleTV
      # 完整 URL：原样拉取。
      - "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash/HBO/HBO.list"
```

## 字段说明

- `base.blackmatrix7_raw`：规则简写的上游根地址。
- `log.path`：编译日志路径。
- `filters`：可复用的规则类型集合。
- `include`：全局默认收录规则类型。
- `groups`：聚合分组。
- `groups.<name>.include`：本分组默认收录规则类型。
- `groups.<name>.sources`：本分组要引入的源文件。
- `groups.<name>.outputs`：本分组要生成的输出文件。
- `outputs.<name>.path`：输出文件路径。
- `outputs.<name>.include`：本文件收录的规则类型。
- `outputs.<name>.exclude`：本文件排除的规则类型。

## 源文件写法

- `YouTube` 会展开为 `base.blackmatrix7_raw/YouTube/YouTube.list`。
- 完整 URL 会原样使用。
- 同一个源重复出现只会拉取一次。
- 某个源失败只写入日志，不影响其他源。

## 输出文件筛选

- `include: ["*"]` 表示收录全部有效规则。
- `include: ["$domain"]` 表示引用 `filters.domain`。
- `exclude` 会从 `include` 结果中排除指定类型。
- `outputs.<name>.include` 优先级最高。
- `groups.<name>.include` 优先级其次。
- 顶层 `include` 是全局默认值。
- 只收录域名规则时，写 `include: [DOMAIN, DOMAIN-SUFFIX, DOMAIN-KEYWORD]`。
- 只收录进程规则时，写 `include: [PROCESS-NAME]`。
- 只收录 IP 规则时，写 `include: [IP-CIDR, IP-CIDR6, IP-ASN]`。

## 过滤器变量

重复的规则类型建议放到 `filters`。

```yaml
filters:
  domain: [DOMAIN, DOMAIN-SUFFIX, DOMAIN-KEYWORD, DOMAIN-WILDCARD, DOMAIN-REGEX]
  ip: [IP-CIDR, IP-CIDR6, IP-SUFFIX, IP-ASN]

groups:
  Google:
    outputs:
      non_ip:
        path: "rule/list/non_ip/google.txt"
        include: ["$domain"]
      ip:
        path: "rule/list/ip/google.txt"
        include: ["$ip"]
    sources: [Google, GoogleDrive]
```

- `$domain` 会展开为 `filters.domain`。
- `$ip` 会展开为 `filters.ip`。
- 过滤器可以用于顶层 `include`、分组 `include`、输出 `include` 和 `exclude`。
- 引用不存在的过滤器会直接报错。

## Include 继承

输出文件不写 `include` 时，会继承分组或全局配置。

```yaml
filters:
  domain: [DOMAIN, DOMAIN-SUFFIX, DOMAIN-KEYWORD]
  ip: [IP-CIDR, IP-CIDR6, IP-ASN]

include: ["$domain"]

groups:
  GlobalMedia:
    include: ["$domain"]
    outputs:
      domain:
        path: "rule/list/non_ip/GlobalMedia_DOMAIN.txt"
      ip:
        path: "rule/list/ip/GlobalMedia_ip.txt"
        include: ["$ip"]
    sources:
      - YouTube
```

- `domain` 没写 `include`，使用 `GlobalMedia.include`。
- `ip` 写了 `include`，使用自己的配置。
- 如果分组也没写 `include`，则使用顶层 `include`。

## 多个聚合组

在 `groups` 下继续增加同级配置即可。

```yaml
groups:
  GlobalMedia:
    outputs:
      non_ip:
        path: "rule/list/non_ip/GlobalMedia_NON_IP.txt"
        include: ["$domain"]
      ip:
        path: "rule/list/ip/GlobalMedia_ip.txt"
        include: ["$ip"]
    sources:
      - YouTube
      - YouTubeMusic
      - HBO

  AI:
    outputs:
      non_ip:
        path: "rule/list/non_ip/AI_NON_IP.txt"
        include: ["$domain"]
      ip:
        path: "rule/list/ip/AI_ip.txt"
        include: ["$ip"]
    sources:
      - OpenAI
      - Claude
      - Gemini

  Apple:
    outputs:
      domain:
        path: "rule/list/non_ip/Apple_DOMAIN.txt"
        include: [DOMAIN, DOMAIN-SUFFIX, DOMAIN-KEYWORD]
      process:
        path: "rule/list/non_ip/Apple_PROCESS.txt"
        include: [PROCESS-NAME]
    sources:
      - Apple
      - AppleTV
```

- `GlobalMedia`、`AI`、`Apple` 是同级聚合组。
- 每个聚合组有自己的 `sources`。
- 每个聚合组可以生成一个或多个输出文件。

## 输出内容

- 非 IP 规则建议输出到 `rule/list/non_ip`。
- IP 规则建议输出到 `rule/list/ip`。
- 每个输出文件顶部包含 `Build Date`、`Rule Count` 和 `Source`。
- `Rule Count` 只统计有效规则。
- `Source` 只记录成功引入的源文件。
- 编译日志记录成功源、失败源、重复源和输出统计。

```text
# Build Date: 2026-06-18 12:00:00 +0800
# Rule Count: 123
# Source:
#   - YouTube: https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash/YouTube/YouTube.list
```

## 常见 Mihomo 规则类型

- `DOMAIN`：完整域名。
- `DOMAIN-SUFFIX`：域名后缀。
- `DOMAIN-KEYWORD`：域名关键词。
- `DOMAIN-WILDCARD`：域名通配符。
- `DOMAIN-REGEX`：域名正则。
- `GEOSITE`：域名集合。
- `IP-CIDR`：目标 IPv4 CIDR。
- `IP-CIDR6`：目标 IPv6 CIDR。
- `IP-SUFFIX`：目标 IP 后缀。
- `IP-ASN`：目标 IP ASN。
- `GEOIP`：目标 IP 国家或地区。
- `SRC-GEOIP`：来源 IP 国家或地区。
- `SRC-IP-ASN`：来源 IP ASN。
- `SRC-IP-CIDR`：来源 IPv4 CIDR。
- `SRC-IP-SUFFIX`：来源 IP 后缀。
- `DST-PORT`：目标端口。
- `SRC-PORT`：来源端口。
- `IN-PORT`：入站端口。
- `IN-TYPE`：入站类型。
- `IN-USER`：入站用户。
- `IN-NAME`：入站名称。
- `PROCESS-PATH`：进程路径。
- `PROCESS-PATH-WILDCARD`：进程路径通配符。
- `PROCESS-PATH-REGEX`：进程路径正则。
- `PROCESS-NAME`：进程名。
- `PROCESS-NAME-WILDCARD`：进程名通配符。
- `PROCESS-NAME-REGEX`：进程名正则。
- `UID`：Linux 用户 ID。
- `NETWORK`：网络类型。
- `DSCP`：DSCP 标记。
- `RULE-SET`：规则集合。
- `AND`：逻辑与。
- `OR`：逻辑或。
- `NOT`：逻辑非。
- `SUB-RULE`：子规则。
- `MATCH`：最终匹配。

规则类型以 Mihomo 官方文档为准：`https://wiki.metacubex.one/config/rules/`。

## 自动更新

- GitHub Actions 文件：`.github/workflows/update-rules.yml`。
- 支持定时执行。
- 支持手动执行。
- 有变更时会直接提交。
