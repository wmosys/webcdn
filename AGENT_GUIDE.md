# Agent 项目指引

这份文档用于给后续接手本仓库的 agent 快速建立上下文。仓库核心不是应用构建，而是维护规则、图标等静态资源，并通过 Python 脚本聚合 Clash/Mihomo 规则文件。

## 项目定位

- 规则聚合脚本：`scripts/aggregate_rules.py`
- 规则聚合配置：`rule/rule-aggregate.yaml`
- 生成结果目录：`rule/list/non_ip/`、`rule/list/ip/`
- 生成日志：`rule/list/build-log.md`
- GitHub Action：`.github/workflows/update-rules.yml`
- 源规则状态基线：GitHub Actions cache 中的 `rule-source-state/source-state.json`
- Python 依赖：`requirements.txt` 中有 `PyYAML`，但当前 Action 为了节省时间不安装依赖，脚本需要能在无 `PyYAML` 环境下运行。

## 常用命令

```bash
python3 scripts/aggregate_rules.py --config rule/rule-aggregate.yaml --state .cache/rule-source-state/source-state.json
python3 -m py_compile scripts/aggregate_rules.py
git diff --check
```

脚本需要访问 `raw.githubusercontent.com`。在 Codex 沙箱内可能出现 DNS 失败；这种情况下本地验证要用已获准的外部网络执行方式。GitHub Actions runner 本身有网络，正常可直接运行。

## GitHub Action 设计

`.github/workflows/update-rules.yml` 当前设计目标是尽量减少构建时间：

- 使用 `workflow_dispatch` 支持手动运行。
- 使用 `schedule` 每天北京时间 `02:00` 自动运行。
- 不执行 `actions/setup-python`。
- 不执行 `pip install -r requirements.txt`。
- 直接用 runner 自带的 `python3` 运行脚本。
- 给 `contents: write` 权限，用于提交生成结果。
- `actions/cache/restore` 会在执行前恢复源规则状态基线。
- `Generate rules` 步骤会输出 `exit_code`、`has_rule_updates`、`has_output_changes`、`report_kind`、`state_updated`、`state_path`、`report_path`，后续缓存保存、提交和通知都基于这些输出判断。
- `actions/cache/save` 会在脚本成功且状态变化时保存新的源规则状态基线。
- 仅当脚本成功且规则输出文件有变更时提交；脚本失败时不提交。
- 自动提交信息使用 `github-actions: 自动更新聚合规则`，用于在 git 历史中明显区分 Action 自动提交。
- 仅当脚本失败、首次初始化状态基线，或脚本成功且存在源规则增删时，通过 Server 酱 3 推送通知；成功但无规则更新不推送。

核心步骤是：

```yaml
- name: Generate rules
  run: |
    python3 scripts/aggregate_rules.py \
      --config rule/rule-aggregate.yaml \
      --state "${RUNNER_TEMP}/rule-source-state/source-state.json" \
      --report "${RUNNER_TEMP}/rule-update-report.md"
```

因此，脚本必须保持“无第三方依赖也能解析当前配置”的能力。

通知使用 Server 酱 3 的完整推送 URL，例如 `https://<uid>.push.ft07.com/send/<sendkey>.send`。该值只应保存到 GitHub `Settings` -> `Secrets and variables` -> `Actions` -> `Repository secrets`，secret 名称为 `SERVER_CHAN_SEND_URL`。如果 secret 未配置，workflow 会跳过通知步骤。推送参数包括 `title`、`desp`、`tags=Github Actions`，其中 `desp` 是 Markdown。

## 配置文件结构

`rule/rule-aggregate.yaml` 的主要结构：

```yaml
base:
  blackmatrix7_raw: "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash"

log:
  path: "rule/list/build-log.md"

filters:
  domain: [DOMAIN, DOMAIN-SUFFIX, DOMAIN-KEYWORD, DOMAIN-WILDCARD, DOMAIN-REGEX]
  ip: [IP-CIDR, IP-CIDR6, IP-SUFFIX, IP-ASN]

groups:
  Google:
    outputs:
      non_ip: {path: "rule/list/non_ip/google.txt", include: ["$domain"]}
      ip: {path: "rule/list/ip/google.txt", include: ["$ip"]}
    sources: [Google, Chromecast, GoogleFCM]
```

关键约定：

- `base.blackmatrix7_raw` 是相对源名称的根 URL。
- `filters` 可以定义规则类型集合，输出中用 `"$domain"`、`"$ip"` 引用。
- `groups` 是聚合分组。
- 每个 group 必须有非空 `sources` 和非空 `outputs`。
- `sources` 中的普通名称会被展开为：`{base}/{source}/{source}.list`。
- `sources` 也可以直接写完整 `http://` 或 `https://` URL。
- 每个 output 需要 `path`，并可配置 `include`、`exclude`。
- `include`、`exclude` 按规则类型过滤，不按域名内容过滤。

当前配置中的主要组包括 `ChinaMax`、`Google`、`Microsoft`、`Develop`、`Communication`、`Social`。如果移除某个 group，脚本不会自动删除旧输出文件，需要手动处理不再需要的历史文件。

## 脚本执行流程

`scripts/aggregate_rules.py` 的主流程在 `main()`：

1. 解析参数，默认配置路径是 `rule/rule-aggregate.yaml`。
2. 定位仓库根目录，保证相对路径从项目根解析。
3. 加载 YAML 配置。
4. 读取 `base.blackmatrix7_raw`、`log.path`、`groups`。
5. 解析 `filters`。
6. 遍历每个 group，调用 `build_group()` 聚合规则。
7. 若存在源抓取失败，打印失败源并返回非 0。
8. 读取 `--state` 指定的源规则状态文件，Action 中该文件来自 GitHub Actions cache。
9. 如果已有状态基线，按 `group -> output -> source` 比较过滤后的源规则集合。
10. 如果没有状态基线，初始化新的状态文件，并生成“状态初始化”通知报告，但不把全部规则算作新增。
11. 如果所有输出规则和 source-state 都没有变化，打印 `规则无变化，跳过写入。` 并直接返回。
12. 如果有变化，只重写发生变化的输出文件，更新状态文件，并在 `--report` 指定时按场景写入 Markdown 报告。

## YAML 加载策略

脚本优先使用 `PyYAML`：

```python
try:
    import yaml
except ImportError:
    yaml = None
```

如果 `PyYAML` 不存在，会走内置轻量 YAML 解析器：

- `load_yaml_subset()`
- `parse_block()`
- `parse_dict()`
- `parse_list()`
- `parse_scalar()`
- `split_inline_items()`

这个解析器是为当前配置格式服务的，不是完整 YAML 实现。它支持：

- 缩进映射。
- 缩进列表。
- 行内列表：`[A, B, C]`
- 行内映射：`{path: "...", include: ["$domain"]}`
- 单引号、双引号字符串。
- 行内值后的行尾注释：`key: [A, B]  # 注释` 会由 `strip_inline_comment()` 剥离注释，且不会切掉引号内 URL 的 `#fragment`。

它不应被当成通用 YAML parser。若未来配置需要 YAML anchor、多行字符串、复杂对象等高级能力，要么扩展解析器，要么重新在 Action 中安装 `PyYAML`。

## 规则 behavior（classical / domain / ipcidr）

mihomo 的 rule-provider 支持三种 behavior，对应三种文件格式。脚本支持按 behavior 读写，使裸值源（如 `chnroutes.txt` 这种纯 CIDR 文件）能被正确处理。

### behavior 与文件格式

| behavior | 文件行格式 | 读取时内部补成 | 写出时 |
|---|---|---|---|
| `classical`（默认） | `TYPE,value[,...]` 如 `IP-CIDR,1.2.3.0/24` | 原样 | 保留前缀 |
| `ipcidr` | 裸 CIDR 如 `1.2.3.0/24` | IPv4→`IP-CIDR,值`，IPv6(含`:`)→`IP-CIDR6,值` | 剥前缀，写裸 CIDR |
| `domain` | 裸域名如 `google.com` | 统一补 `DOMAIN-SUFFIX,值` | 剥前缀，写裸域名 |

关键点：

- `ipcidr`/`domain` 文件**不带类型前缀**，这是 mihomo 协议要求；附加选项如 `no-resolve` 不应出现在这两种文件里（引用方在 rule-providers 使用处声明）。
- `ipcidr` 源里出现 `1.2.3.0/24,no-resolve` 这种带附加项的行，脚本会容错只取 CIDR 部分（取首个逗号前）。
- `domain` 源里 `+.example.com` 会被剥成 `DOMAIN-SUFFIX,example.com`（与 mihomo domain 行为一致，纯域名行等价后缀匹配）。
- 非法行（非注释非空但不符合格式）会被跳过，不报错中止。

### source 声明 behavior

`sources` 支持简写和对象两种写法：

```yaml
sources:
  - Google                         # 简写，默认 classical
  - {name: ChinaIPsBGP}            # 对象，默认 classical
  - {url: "https://.../chnroutes.txt", behavior: ipcidr}
  - {url: "https://.../domains.txt", behavior: domain, name: CustomDomains}
```

- 简写字符串：展开为 `{base}/{name}/{name}.list`，behavior 默认 `classical`。
- 对象形式：必须提供 `name` 或 `url` 之一；`url` 直接使用，`name` 走 base 展开；`behavior` 可选。
- `behavior` 可选值：`classical` / `domain` / `ipcidr`，默认 `classical`。

### output 声明 type

output 用 `type` 字段声明产物格式：

```yaml
outputs:
  ip:
    path: "rule/list/ip/ChinaIP.txt"
    type: ipcidr              # 产出裸 CIDR 文件
    include: ["$ip"]
  non_ip:
    path: "rule/list/non_ip/ChinaIP.txt"
    include: ["$domain"]      # 省略 type 即 classical
```

`type` 可选值：`classical` / `domain` / `ipcidr`，默认 `classical`。

### 跨 behavior 归一化去重

跨组去重（`exclude_from`）时按**规范值**比较，即剥掉类型前缀后的值部分。因此 classical 的 `IP-CIDR,1.2.3.0/24` 和 ipcidr 的 `1.2.3.0/24` 会被识别为同一条，避免重复。详见「跨组去重」章节。

### 文件 diff 与 behavior

读取已写入的规则文件做变更比较时，会按 output 的 `type` 还原成内部规范形式（裸 CIDR→`IP-CIDR,值`，裸域名→`DOMAIN-SUFFIX,值`）后再比较，避免「裸值文件 vs 内部规范化表示」误判为每次都变化。

## 规则抓取与解析

`normalize_source()` 负责把 source 转为 URL：

- 完整 URL 原样使用。
- 普通名称展开为：`{base}/{source}/{source}.list`。
- 空字符串或仅空白的 source 会报错，错误信息形如 `AI.sources 存在空字符串源配置...`，明确指向出问题的 group，便于排查 YAML 缩进或多余空行导致的空条目。

`fetch_url_text()` 使用标准库 `urllib.request` 抓取文本，并设置 `User-Agent: Mozilla/5.0`。

`parse_rule_line()` 负责解析规则行：

- 忽略空行。
- 忽略 `#` 开头的注释。
- 按逗号切分。
- 少于两段的行忽略。
- 规则类型会转为大写。
- 输出规则格式是标准化后的 `TYPE,value,...`。

`parse_source_text()` 会把每个源文本解析成 `(rule_type, canonical_rule)` 列表。

## 分组聚合逻辑

`build_group()` 是核心聚合函数：

- 每个 group 内用 `resolved_seen` 跳过重复 source。
- 跨 group 使用 `source_cache` 复用已经抓取并解析过的 URL。
- 每个 output 有自己的 `output_seen`，用于去重。
- 一条规则会被投放到所有匹配该规则类型的 output。
- `rule_matches_output()` 判断 `include` 和 `exclude`。
- 抓取失败不会立即终止整个脚本，而是记录到 `failed_sources`，继续处理其他源。

结果结构：

- `GroupResult.success_sources`
- `GroupResult.failed_sources`
- `GroupResult.duplicate_sources`
- `GroupResult.outputs`
- `OutputResult.rules`

## 输出文件格式

规则输出文件由 `format_rule_file()` 生成，大致格式：

```text
# Build Date: 2026-06-18 13:59:59 +0800
# Rule Count: 711
# Source:
#   - Google: https://...

DOMAIN-SUFFIX,example.com
IP-CIDR,1.2.3.0/24
```

注意：这些 `#` 注释是元数据，不参与“规则是否变化”的判断。

## 无变化不提交的关键设计

这是当前脚本最重要的维护点。

为了避免 Action 每次因为 `Build Date` 或 `build-log.md` 更新时间变化而产生空 commit，脚本不会先写文件再交给 Git 判断，而是先比较真实规则内容和源规则状态：

- `read_existing_rules()` 读取已有输出文件。
- 它会忽略空行和所有 `#` 注释行。
- `output_rules_changed()` 比较现有真实规则行和新生成的 `OutputResult.rules`。
- `--state` 指定的状态文件保存每个 `group -> output -> source` 的过滤后源规则快照。
- GitHub Action 中该状态文件保存到 GitHub Actions cache，不进入 Git 仓库。
- `build_source_diffs()` 基于 source-state 统计每个源规则在每个聚合输出中的新增和删除数量。
- 若所有 output 和 source-state 都无变化，`main()` 直接返回，不写任何文件。

这一点不要随意改回“每次运行都写输出文件”，否则会导致 GitHub Action 每次定时运行都产生提交。

当前行为：

- 没有规则变化：不写输出文件，不写状态文件，不写 `build-log.md`，不提交，不推送。
- 有规则变化：只写发生变化的输出文件，同时更新 cache 状态文件和 `build-log.md`，并生成推送报告。
- 首次 cache miss：写入 cache 状态文件，生成初始化报告；如果规则输出文件本身没有变化，不提交。
- 新增 output 文件：视为变化，会写入。
- 删除配置中的 group/output：脚本不会自动删除旧文件。

## 源规则状态缓存

源规则状态文件用于通知统计，不是仓库产物。它保存每个聚合输出下每个源规则过滤后的规则集合，体积会接近完整规则集，因此不要提交到 Git。

GitHub Action 的处理方式：

- `Restore source state cache` 从 GitHub Actions cache 恢复 `${RUNNER_TEMP}/rule-source-state`。
- `Generate rules` 通过 `--state "${RUNNER_TEMP}/rule-source-state/source-state.json"` 读取和更新状态。
- `Save source state cache` 在脚本成功且 `STATE_UPDATED=true` 时保存新的 cache。
- cache key 使用 `rule-source-state-${{ github.ref_name }}-${{ github.run_id }}`，并通过 `restore-keys` 读取同分支最近一次状态。

脚本输出给 workflow 的关键字段：

- `HAS_RULE_UPDATES=true|false`：已有状态基线下是否存在源规则新增/删除。
- `HAS_OUTPUT_CHANGES=true|false`：生成的规则输出文件是否需要提交。
- `REPORT_KIND=updates|initialized|none`：通知报告类型。
- `STATE_UPDATED=true|false`：状态文件是否需要保存到 cache。

cache miss 时，`REPORT_KIND=initialized`，脚本会初始化状态文件并生成初始化通知，不会把当前所有规则都算作新增。

## 构建日志

`write_build_log()` 写入 `rule/list/build-log.md`，包含：

- 编译日期。
- 每个 group 的输出文件和规则数量。
- 成功源。
- 失败源。
- 重复源。

只有当至少一个 output 的真实规则发生变化时，日志才会被重写。这是为了避免仅更新 cache 状态时产生 commit。

## 推送报告

成功且存在规则更新时，脚本在 `--report` 指定路径写入 Markdown 报告。报告按聚合分组列出，同一分组下的 `non_ip`、`ip` 等输出会合并到一张表，通过“类型”列区分：

```md
### Google

| 源规则 | 类型 | 新增 | 删除 |
|---|---|---:|---:|
| `Google` | non_ip | 12 | 3 | <!-- rule/list/non_ip/google.txt -->
| `Google` | ip | 4 | 0 | <!-- rule/list/ip/google.txt -->
| `Chromecast` | non_ip | 1 | 0 | <!-- rule/list/non_ip/google.txt -->
```

失败时，workflow 自己生成失败 Markdown，不依赖脚本报告。通知标题格式固定为 `GitHub Actions : 成功 : 仓库名称` 或 `GitHub Actions : 失败 : 仓库名称`。

## 跨组去重

默认情况下，规则只在 group 内部按 output 去重，不同 group 之间互不影响。如果希望某个 group 排除掉另一个 group 中已有的规则，可以在该 group 配置 `exclude_from`：

```yaml
groups:
  Develop:
    outputs:
      non_ip: {path: "rule/list/non_ip/develop.txt", include: ["$domain"]}
    sources: [GitHub, GitLab]

  Communication:
    outputs:
      non_ip: {path: "rule/list/non_ip/communication.txt", include: ["$domain"]}
    sources: [Discord, Slack]
    exclude_from: [Develop]
```

关键语义：

- `exclude_from` 是一个列表，列出需要排除其规则的上游 group 名称。
- 上游 group 的内容不会变化，被排除的规则只从当前 group 中剔除。
- 去重按 output 名维度匹配：`Communication.non_ip` 只排除 `Develop.non_ip` 的规则，不会把 `Develop.ip` 的 IP 规则误排除到域名 output。只有同名 output 之间才参与跨组排除。
- 去重发生在 group 内部去重之后，按**规范值**（剥掉类型前缀后的值，如 `github.com`、`1.2.3.0/24`）比较，因此不同 behavior 之间也能正确识别同一条规则。
- 多个上游可以叠加：`exclude_from: [A, B]` 表示排除 A 和 B 对应同名 output 规则的并集。
- 不支持传递：`C exclude_from [B]` 只排除 B 的规则，不会自动排除 B 从 A 那里排除的规则。
- 脚本会对 group 按 `exclude_from` 依赖做拓扑排序后再聚合，保证上游先处理。
- 存在循环引用（A 排除 B、B 又排除 A）或引用不存在的 group 时，脚本会报错中止。

## 常见维护任务

### 新增规则分组

在 `rule/rule-aggregate.yaml` 的 `groups` 下新增 group：

```yaml
NewGroup:
  outputs:
    non_ip: {path: "rule/list/non_ip/newgroup.txt", include: ["$domain"]}
    ip: {path: "rule/list/ip/newgroup.txt", include: ["$ip"]}
  sources: [SomeSource, AnotherSource]
```

然后运行：

```bash
python3 scripts/aggregate_rules.py --config rule/rule-aggregate.yaml --state .cache/rule-source-state/source-state.json
```

### 只输出域名规则

只配置 `non_ip` output 即可，例如当前 `Microsoft` 的 `ip` output 是注释掉的。

### 添加新的规则类型集合

在 `filters` 中添加：

```yaml
custom: [DOMAIN-SUFFIX, IP-CIDR]
```

然后在 output 中引用：

```yaml
include: ["$custom"]
```

### 验证无依赖路径

可用下面方式强制模拟没有 `PyYAML`：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("aggregate_rules", "scripts/aggregate_rules.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.yaml = None
config = module.load_config(Path("rule/rule-aggregate.yaml"))
filters = module.parse_filters(config)
print(config["groups"].keys())
print(filters.keys())
PY
```

## 验证清单

改动脚本或配置后，建议至少跑：

```bash
python3 -m py_compile scripts/aggregate_rules.py
git diff --check
```

若改动影响配置解析，额外跑“无依赖路径”验证。

若改动影响聚合行为，必须完整运行脚本并检查：

- `rule/list/build-log.md` 是否有失败源。
- 生成文件规则数量是否符合预期。
- `.cache/rule-source-state/source-state.json` 或 workflow cache 中的状态文件是否更新到当前源规则快照。
- 无规则变化时再次运行是否输出 `规则无变化，跳过写入。`。
- `git status --short -- rule/list` 是否符合预期。

## 注意事项

- 不要提交 `scripts/__pycache__/`、`.cache/`、`rule/list/source-state.json` 这类运行缓存或状态文件。
- Action 中不安装依赖是有意为之，目的是减少定时任务耗时。
- 如果未来为了完整 YAML 能力恢复 `PyYAML` 安装，需要重新评估 Action 构建时间。
- 本地 Codex 沙箱网络可能不能访问 GitHub raw 源，DNS 失败不等同于脚本逻辑失败。
- 任一源抓取失败会让脚本返回非 0，workflow 会推送失败通知并最终失败。
