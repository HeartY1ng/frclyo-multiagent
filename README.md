# frclyo-multiagent

**本地备好 API 和 agent 框架，用的时候按任务自取。**

[English](README.en.md) · [设计思路](docs/思路.md) · [切词实战](docs/案例/2026-10-13-切词实战.md)

在本机准备模型资源池，让大脑按任务类型、难度和验收方式，选择“框架 × 模型 × 档位”。大脑负责定契约、派活和验收，工人负责交付，看板展示有证据的状态。

**第一版只支持 macOS，使用 Python 3.9+ 标准库和系统 bash。** 大脑范围为 Claude Code、Codex；工人范围为 Codex、OpenCode，后者连接 DeepSeek、Kimi、MiMo 等 API。具体型号和档位需要实际校准，不保证任意组合可用。

```text
你 → 大脑（Claude 或 Codex）→ 账本
                                ↓
              工人（Codex、OpenCode 接 DeepSeek / Kimi / MiMo）
                                ↓
                   回执 → 验收 → 看板
```

账本保留任务号、轮次和状态；执行结束不等于验收通过。看板展示进度，交付物仍须独立检查。

## 三条原则

1. **先定契约再拆活。** 先写清接口、格式、文件归属和坏输入处理，再决定是否并行；强耦合的活默认不拆。Cognition 的案例说明，各自合理的隐含决定也会造成集成冲突；契约是本项目据此采用的工程方法。[出处：Don’t Build Multi-Agents](https://cognition.ai/blog/dont-build-multi-agents)
2. **工人自述不算证据，靠回执和复跑验收。** 收齐产物后过契约关、一致关和端到端关；能写参考实现就做逐字节比较。MAST 将任务验证问题列为多 agent 失败的一类；回执机制和三关检查是本项目的应对方式。[出处：MAST](https://arxiv.org/abs/2503.13657)
3. **每件活能追溯、能撤销。** 用任务台账、进度台账和独立轮次目录留住事实，验收后才并入项目。台账借鉴 Magentic-One；安装登记、按账卸载和撤销边界是本项目的设计，不是论文已经证明的功能。[出处：Magentic-One](https://arxiv.org/abs/2411.04468)

## 三步上手

先安装并登录 Codex，或准备 OpenCode 及提供商 API。并行演示需要 Codex（可派两位工人），或 Codex + OpenCode；只有 OpenCode 时需两家提供商。框架不替你全局安装 CLI 或登录账号。OpenCode 首次使用前，在工人外运行 `opencode models --refresh` 准备本机模型缓存。

克隆仓库后进入目录：

```sh
git clone https://github.com/HeartY1ng/frclyo-multiagent.git
cd frclyo-multiagent
```

1. 安装：

   ```sh
   ./install.sh
   ```

   安装器检查环境，询问创建配置、工作目录、命令链接、Claude 技能和看板自启，并逐项登记。将 `~/.local/bin` 加入 PATH 后可运行 `aw`；安装器会提示缺少 PATH 的情况，也可直接使用 `./bin/aw`。

2. 登记 API：

   ```sh
   aw key add deepseek
   ```

   确认后由 macOS 钥匙串工具隐藏输入；密钥不进入对话、项目文件或安装记录。

3. 运行演示：

   ```sh
   aw demo
   ```

   确认组合与费用后，两个工人并行完成 CSV 规范化和分类汇总，核对回执后自动跑三关：样例逐字节、双方测试复跑、端到端逐字节。演示使用独立来源 `DM`，不占用 `CX` 编号。看板自动打开；配置端口已有看板时直接复用，否则启动临时看板，结束后可运行 `aw board start` 再查看。

配置唯一来源为 `~/.frclyo/config.json`；测试可用 `FRCLYO_CONFIG` 指定另一个文件。Codex 文件优先读取 `CODEX_HOME`，未设置时才读取 `~/.codex`。密钥可用性统一由实际读取判断：先查钥匙串，再查显式配置的 `key_env_file`；`aw key list` 只输出能取到 key 的提供方名字。Codex 模型缓存只代表候选，App 与 CLI 可能支持不同模型；`aw doctor` 离线列出候选，看板未启动只显示 ⚠️，不影响退出码。`aw doctor --live` 经费用确认后按工人各选一个组合实跑。`aw calibrate` 对候选模型各选一个默认档位跑三题，不代表所有档位均已测试。用量保留回执原始对象，费用未知就留空。

模型可用性保存在 `work_dir/模型可用性.json`，按工人、模型和实际 CLI 版本记录状态、UTC 时间及原因。候选按 `ok`、`unknown`（未测）排序，`unusable` 默认隐藏；运行时接口 `models.discover(include_unusable=True)` 可返回全部候选，每项含 `availability` 和 `cli_version`。CLI 版本变化后，旧版本结论不参与本次选择，旧记录保留；版本查询失败也按未测处理。

`aw demo`、`aw calibrate` 和 `aw doctor --live` 遇到明确的模型不支持错误时，在同一工人的剩余候选中自动切换，最多换 3 个（含首选共 4 次），每次使用新轮次并保留失败证据。OpenCode 可以切到另一家已登记的提供商，演示会避开另一位仍在运行的工人占用的提供商。普通网络、鉴权、超时、回执或产物验收失败不会触发切换。正常执行记 `ok`，与产物评分分开；模型支持错误记 `unusable`，其他失败不覆盖既有可用性结论。并发读写加锁，文件原子替换。

演示三关通过返回 0，执行或验收失败返回非零。校准完成出报告后返回 0，即使存在失败组合；`路由建议.md` 列出失败原因、轮次和证据路径，不能仅看退出码判断模型质量。真实自检按各工人的最终验收结果返回 0 或非零。记录按 CLI 版本区分，不保存账号身份；同版本后续成功记录可以覆盖旧的不可用结论。

## 命令一览

以下与 `aw help` 一致：

```text
aw：本机多工人入口（中文/英文命令均可）
  aw key add|list|remove <家>     钥匙串登记、只列名字、删除
  aw board [start|stop|open|status] 前台启动、停自启、打开、状态
  aw ledger …                   原样传给账本
  aw run codex|opencode …        原样传给执行脚本
  aw doctor [--live]             本机自检；live 确认费用后跑小题
  aw demo                       契约 → 两个工人并行 → 三关验收
  aw calibrate                  确认组合和费用后跑三题，生成路由建议
  aw uninstall [--dry-run]       按安装记录撤销，默认保留配置和数据
  aw help                       本说明
中文：密钥（添加/列表/删除）、看板（启动/停止/打开/状态）、账本、运行、自检、演示、校准、卸载、帮助
安装/演示/校准支持 --yes、--no-input；卸载 --yes 也会同意删除数据，使用前请先 --dry-run。
```

## 卸载

```sh
./uninstall.sh --dry-run
./uninstall.sh
# 也可使用 aw uninstall
```

按 `~/.frclyo/安装记录.jsonl` 逆序撤销命令链接、Claude 技能、launchd 和本工具新增的钥匙串条目；修改过的技能等安装文件会保留。默认保留配置和工作数据。`./uninstall.sh --yes` 同意删除本工具的 `config.json`（已有或修改过的也删除）、安装记录及本次安装创建的工作目录全部数据；已有的工作目录不会递归删除，其他用户文件继续保留。撤销失败时保留安装记录供重试。`aw key remove deepseek` 仅删除钥匙串条目，不改显式配置的旧凭据回退文件。`~/.cache/opencode` 等目录由 OpenCode 自己创建，不属于本工具的卸载范围。

## 安全默认

- **工人断网**：Codex 命令执行默认断网，网页搜索按任务显式开启；OpenCode 使用 macOS 沙箱禁止直接出网。
- **白名单**：OpenCode 只允许本轮提供商的 HTTPS 主机，经随机本机端口的 CONNECT 代理连接；不放行其他主机、DNS 或其他回环端口。
- **钥匙串**：凭据由统一接口读取，OpenCode 每轮只注入该家凭据；仓库和安装记录不保存密钥。不改用户的 Codex 或 Claude 配置文件。
- **零遥测**：框架不上传使用记录、任务、表现或统计。模型请求和看板额度／余额查询会连接相应服务，第三方 CLI 自身行为受其配置及政策约束。
- **可撤销**：机器变更先确认并记账，工人按独立轮次交付；看板只读且只监听 `127.0.0.1`。每小时日审核对完成卡的最新回执与结果哈希。

回执哈希只覆盖结果说明，完整产物正确性仍靠复跑验收；日审不代表整机安全审计。OpenCode 模型目录必须来自本机缓存，启动器不自行下载。

## 验证

```sh
python3 tests/run_all.py
```

一次运行 `bin/tests`、`board/test_board.py` 和 `tests` 全部必过离线测试；零测试、失败和跳过都返回非零。测试使用临时 HOME、假钥匙串和假模型，不消耗 API 费用。CI 在 macOS 的 Python 3.9、3.13 上调用同一入口。

主机验收单独运行 `python3 tests/host/test_proxy_tcp.py` 和 `bash tests/host/network_check.sh`；后者需要 macOS、OpenCode、本机模型缓存和预先从环境提供的 `DEEPSEEK_API_KEY`，包含真实模型调用并产生费用。受限沙箱中的离线通过不能替代这些内核与真实模型验收。

## 和现成工具的区别

本项目选择保留自己的执行、契约和验收体系，有限投入只读看板。输入对比报告中的 Vibe Kanban 更侧重工作区、diff 审查和 PR 开发流程；codex-orchestrator 更侧重 Codex 持续会话、追问和终端调度。这些能力各有价值，本项目优先处理多模型派活和独立验收，尚未实现与它们的即插即用桥接。[比较范围与出处](docs/案例/工具对比.md)

对比来自一次静态快照审阅，不是性能排名。网络隔离实现和主机验收脚本已随仓库提供；发布前仍须完成主机实测。回执哈希只覆盖结果说明，不能证明所有产物完整或正确。[设计与边界](docs/思路.md)

## 文档与案例

- [为什么这样设计，以及踩过的坑](docs/思路.md)
- [切词实战：两位工人通过契约交接，得到 252 个二元组](docs/案例/2026-10-13-切词实战.md)
- [工具对比简版](docs/案例/工具对比.md)
- [任务分类标准](docs/协议/任务分类标准.md)、[任务拆分与集成规范](docs/协议/任务拆分与集成规范.md)、[任务卡模板](docs/协议/模板/任务卡.md)

协议按历史版本保留并脱敏，其中旧技能名、模型路由和“不能联网”等表述是当时的规则或要求，不代表本版已实现；历史技能、运行记录和语料不随仓库发布。当前支持范围和开发状态以本 README 为准。

## 许可证

**源码公开、个人和非商用免费、商用请联系。** 采用 [PolyForm Noncommercial License 1.0.0](LICENSE)，版权声明见 [NOTICE](NOTICE)。这是 source-available 项目；该许可证不是 OSI 认证的开源许可证，因此不将本项目称为开源项目。

个人免费使用仍须符合许可证规定的用途条件，包括个人用途不得预期用于商业应用；完整许可范围以原文为准。商用授权申请见 [COMMERCIAL.md](COMMERCIAL.md)。
