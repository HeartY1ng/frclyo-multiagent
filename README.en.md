# frclyo-multiagent

**Prepare your APIs and agent frameworks locally. Pick what each task needs.**

[中文](README.md) · [Design notes (Chinese)](docs/思路.md) · [Case study (Chinese)](docs/案例/2026-10-13-切词实战.md)

Build a local pool of models and agents. The coordinator chooses a framework, model, and reasoning level based on the task and how its output can be verified. It defines the contract, dispatches work, and checks results. Workers deliver artifacts; the board shows recorded state.

**The first release supports macOS only, using Python 3.9+ standard libraries and system bash.** Coordinators: Claude Code and Codex. Workers: Codex and OpenCode, with API providers such as DeepSeek, Kimi, and MiMo through OpenCode. Each model and reasoning configuration needs a real compatibility check.

```text
You → Coordinator (Claude or Codex) → Ledger
                                       ↓
       Workers (Codex; OpenCode with DeepSeek / Kimi / MiMo)
                                       ↓
                  Receipt → Acceptance checks → Board
```

The ledger records task IDs, rounds, and state. A finished process is distinct from an accepted result; the board does not replace artifact verification.

## Three principles

1. **Define the contract before splitting work.** Specify interfaces, formats, file ownership, and malformed-input handling. Keep tightly coupled work together. Cognition describes how implicit decisions can conflict across agents; contracts are our engineering response to that problem. [Source: Don’t Build Multi-Agents](https://cognition.ai/blog/dont-build-multi-agents)
2. **A worker's claim is not evidence. Check receipts and rerun verification.** Check contract compliance, decision consistency, and the complete pipeline. Compare bytes against a reference implementation when practical. MAST identifies task verification as a category of multi-agent failure; our receipts and three checks address that risk. [Source: MAST](https://arxiv.org/abs/2503.13657)
3. **Make work traceable and reversible.** Keep task and progress ledgers, isolate execution rounds, and accept changes before merging them. Magentic-One informs the ledger design. Installation tracking, uninstall support, and rollback boundaries are our own design goals, not capabilities established by that paper. [Source: Magentic-One](https://arxiv.org/abs/2411.04468)

## Three steps to get started

Install and log in to Codex, or prepare OpenCode and provider credentials. The parallel demo needs Codex (two workers are supported), Codex plus OpenCode, or two different OpenCode providers. The framework does not globally install CLIs or log in for you. Before first use of OpenCode, run `opencode models --refresh` outside the worker to populate its local model cache.

Clone the repository and enter it:

```sh
git clone https://github.com/HeartY1ng/frclyo-multiagent.git
cd frclyo-multiagent
```

1. Install:

   ```sh
   ./install.sh
   ```

   Confirm configuration, work directory, command link, Claude skill and board startup separately. Each change is recorded. Add `~/.local/bin` to PATH if prompted, or use `./bin/aw` directly.

2. Register an API:

   ```sh
   aw key add deepseek
   ```

   After confirmation, macOS Keychain handles hidden terminal input. Credentials stay out of conversations, project files and the installation journal.

3. Run the demo:

   ```sh
   aw demo
   ```

   Confirm the worker choices and cost. Two workers implement CSV normalization and category totals in parallel. Receipts are verified, then three gates run automatically: byte comparisons for samples, both workers' tests, and a byte comparison of the complete pipeline. Demo tasks use the independent `DM` source, leaving `CX` numbering untouched. The board opens automatically, reusing the board on the configured port if it is already running. Otherwise, a temporary board stops after the demo; use `aw board start` to view the saved tasks again.

The only configuration source is `~/.frclyo/config.json`; tests can override its location with `FRCLYO_CONFIG`. Codex files are read from `CODEX_HOME` when set, otherwise from `~/.codex`. Credential availability uses actual retrieval from Keychain, falling back to an explicitly configured `key_env_file`; `aw key list` prints only provider names. Codex's shared App/CLI model cache lists candidates, not verified CLI compatibility. `aw doctor` lists local candidates; an unstarted board is only a warning and does not affect its exit code. `aw doctor --live` confirms costs and tests one combination per worker. `aw calibrate` selects one default level per candidate model for three questions, not every available level. Token usage preserves the receipt's raw object; unknown costs remain null.

Availability is stored in `work_dir/模型可用性.json`, keyed by worker, model and actual CLI version, with a UTC timestamp and reason. Discovery returns `availability` and `cli_version`, ranks `ok` before `unknown`, and excludes `unusable` by default; `models.discover(include_unusable=True)` includes all candidates. A CLI version change makes old conclusions inapplicable. Reads and atomic writes are locked.

Demo, calibration and live checks switch models only after an explicit unsupported-model error: at most three replacements (four attempts total), each in a new round with preserved evidence. OpenCode may switch registered providers, while the parallel demo avoids providers occupied by its other worker. Other execution or grading failures do not trigger a retry. A normal worker completion records `ok`; artifact quality is graded separately. Demo failure returns a nonzero exit code. Calibration returns 0 once its report is written, even when combinations fail; the report lists failed rounds, reasons and evidence. Live checks return success only if every worker's final attempt passes.

## Command reference

`aw help` lists these commands; Chinese command aliases are also supported.

| Command | Behavior |
|---|---|
| `aw key add\|list\|remove <provider>` | Register, list names, or remove Keychain entries |
| `aw board [start\|stop\|open\|status]` | Start in foreground, stop launchd, open browser, or inspect status |
| `aw ledger …` | Forward arguments to the ledger |
| `aw run codex\|opencode …` | Forward arguments to a worker launcher |
| `aw doctor [--live]` | Local checks; live mode runs a paid task after confirmation |
| `aw demo` | Contract, two parallel workers, three acceptance gates |
| `aw calibrate` | Confirm combinations and cost, run three questions, write routing advice |
| `aw uninstall [--dry-run]` | Undo recorded changes; preserve configuration and data by default |
| `aw help` | Show command help |

Install, demo, calibration and live checks accept `--yes` or `--no-input`. Uninstall also accepts these flags; its `--yes` authorizes data removal.

## Uninstall

```sh
./uninstall.sh --dry-run
./uninstall.sh
# Or: aw uninstall
```

The journal at `~/.frclyo/安装记录.jsonl` drives reverse removal of command links, the Claude skill, launchd and Keychain entries added by this tool. Modified files such as the installed skill are retained. Configuration and work data are retained by default. `./uninstall.sh --yes` authorizes removal of this tool's `config.json` (including preexisting or modified configurations), the installation journal, and all data in a work directory created by this installation. Preexisting work directories are never recursively removed, and other user files are retained. The journal is kept for retry if an undo fails. `aw key remove deepseek` only deletes the Keychain entry and leaves any explicitly configured legacy credential file untouched. Directories such as `~/.cache/opencode` are created by OpenCode itself and are outside this tool's uninstall scope.

## Security defaults

- **Worker networking:** Codex shell commands have networking disabled by default; web search is explicitly enabled per task. OpenCode's macOS sandbox blocks direct network access.
- **Allowlist:** OpenCode reaches only the current provider's HTTPS host through a CONNECT proxy on a random loopback port. Other hosts, DNS and other loopback ports remain blocked.
- **Keychain:** A shared interface retrieves credentials. OpenCode receives only its current provider's credential. Credentials are absent from the repository and installation journal. The framework does not modify Codex or Claude user configuration files.
- **Zero telemetry:** The framework does not upload tasks, usage records, performance data or statistics. Model requests and board quota/balance queries contact the respective services; third-party CLI behavior remains subject to its own settings and policies.
- **Reversibility:** Machine changes require confirmation and are recorded. Worker rounds are isolated. The board is read-only and binds only to `127.0.0.1`. Hourly audits compare accepted tasks with their latest receipt and result hash.

Receipt hashes cover the result report only; rerun acceptance checks to assess artifacts. The hourly audit is an evidence check, not a machine security audit. OpenCode model catalogs come from local cache; the launcher does not download them.

## Validation

```sh
python3 tests/run_all.py
```

This runs all required offline suites in `bin/tests`, `board/test_board.py`, and `tests`. Missing tests, failures and skips fail the command. Tests use temporary homes, fake Keychain commands and fake models, with no API charges. CI invokes the same entry point on macOS with Python 3.9 and 3.13.

Host checks are separate: `python3 tests/host/test_proxy_tcp.py` and `bash tests/host/network_check.sh`. The latter requires macOS, OpenCode, local model cache and `DEEPSEEK_API_KEY` provided through the environment; it includes a real, billable model task. Offline results from a restricted sandbox do not establish these host or model checks.

## How it compares

Our choice is to retain task execution, contracts, and independent acceptance checks while keeping the board focused on read-only status. In the reviewed snapshots, Vibe Kanban emphasizes workspaces, diff review, and PR workflows. codex-orchestrator emphasizes ongoing Codex sessions, follow-up instructions, and terminal control. Those are useful capabilities; our priority is dispatch across models with explicit acceptance criteria. Plug-and-play integration is not implemented. [Scope and sources](docs/案例/工具对比.md)

The comparison is a static snapshot review, not a performance ranking. Network isolation and host verification scripts are included; host validation is still required before release. Receipt hashes cover only the result report, not every artifact. [Design and limitations](docs/思路.md)

## Documentation

- [Design decisions and lessons learned](docs/思路.md)
- [Segmentation case: two workers, one contract, 252 bigrams](docs/案例/2026-10-13-切词实战.md)
- [Tool comparison](docs/案例/工具对比.md)
- [Task classification](docs/协议/任务分类标准.md), [decomposition and integration protocol](docs/协议/任务拆分与集成规范.md), and [task template](docs/协议/模板/任务卡.md)

These documents are in Chinese. Protocols preserve a sanitized historical version: old skill names, routing choices, and networking prohibitions describe earlier rules or requirements, not implemented features of this release. Historical skills, runtime records, and corpora are not included. This README defines the current scope and development status.

## License

**Source available. Free for permitted personal and noncommercial use. Contact the author for commercial licensing.** See [PolyForm Noncommercial License 1.0.0](LICENSE) and [NOTICE](NOTICE). This is a source-available project; the license is not OSI-approved, so we do not describe the project as open source.

Personal use must still meet the license's purpose conditions, including the absence of an anticipated commercial application. The full license governs the permissions. See [COMMERCIAL.md](COMMERCIAL.md) to request commercial authorization.
