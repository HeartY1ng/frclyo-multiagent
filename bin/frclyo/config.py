"""统一读取配置：缺字段补默认，错误配置明确失败，不静默降低隔离。"""
import copy
import json
import os
from pathlib import Path
import re
import tempfile


# 提供方集合显式配置时以用户列出的键为准；已有家只补齐缺失字段。
DEFAULTS = {
    "version": 1, "work_dir": "~/AgentWork", "board_port": 8789,
    "codex": {"proxy": "", "default_fast": True},
    "providers": {
        "deepseek": {"opencode_family": "deepseek", "api_host": "api.deepseek.com", "balance_api": "deepseek"},
        "moonshotai-cn": {"opencode_family": "moonshotai-cn", "api_host": "api.moonshot.cn", "balance_api": "moonshot"},
        "xiaomi": {"opencode_family": "xiaomi", "api_host": "api.xiaomimimo.com", "balance_api": ""},
    },
}


def _path(value, field):
    """路径规范成绝对路径，拒绝会破坏 shell/沙箱配置的控制字符。"""
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError("配置路径无效：" + field)
    return str(Path(value).expanduser().absolute())


def codex_home():
    """Codex 文件优先位于 CODEX_HOME，未设置时才使用当前 HOME。"""
    return Path(_path(os.environ.get("CODEX_HOME", "~/.codex"), "CODEX_HOME"))


def load():
    """返回独立字典；每次读取，允许调用方修改返回值而不污染默认值。"""
    path = _path(os.environ.get("FRCLYO_CONFIG", "~/.frclyo/config.json"), "FRCLYO_CONFIG")
    try:
        with open(path, encoding="utf-8") as stream:
            given = json.load(stream)
    except FileNotFoundError:
        given = {}
    except (ValueError, UnicodeError):
        # 不回显配置内容，避免误填的敏感值进入诊断。
        raise ValueError("配置文件不是有效 UTF-8 JSON") from None
    return _normalize(given)


def _normalize(given):
    """读写共用校验和缺省合并，写入前发现错误。"""
    if not isinstance(given, dict):
        raise ValueError("配置必须为 JSON 对象")
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(copy.deepcopy(given))
    if type(cfg["version"]) is not int or cfg["version"] != 1:
        raise ValueError("只支持配置 version=1")
    if type(cfg["board_port"]) is not int or not 1 <= cfg["board_port"] <= 65535:
        raise ValueError("board_port 必须在 1～65535 之间")
    if not isinstance(cfg["codex"], dict):
        raise ValueError("codex 必须为对象")
    cfg["codex"] = dict(DEFAULTS["codex"], **cfg["codex"])
    if not isinstance(cfg["codex"]["proxy"], str) or type(cfg["codex"]["default_fast"]) is not bool:
        raise ValueError("codex.proxy 必须为文字，default_fast 必须为布尔值")
    if not isinstance(cfg["providers"], dict):
        raise ValueError("providers 必须为对象")
    for provider, fields in list(cfg["providers"].items()):
        if not re.fullmatch(r"[a-z0-9-]+", provider) or not isinstance(fields, dict):
            raise ValueError("提供方名字或配置无效")
        entry = dict(DEFAULTS["providers"].get(provider, {}), **fields)
        if not isinstance(entry.get("opencode_family"), str) or not re.fullmatch(r"[a-z0-9-]+", entry["opencode_family"]):
            raise ValueError("提供方缺少有效 opencode_family")
        host = entry.get("api_host")
        if not isinstance(host, str) or len(host) > 253 or not re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host, re.I):
            raise ValueError("api_host 必须为完整 DNS 主机名，不含协议、端口或路径")
        entry["api_host"] = host.lower()
        entry.setdefault("balance_api", "")
        cfg["providers"][provider] = entry
    # 公共五条路径以及运行时内部路径都在此推导，执行脚本不自行解析配置。
    cfg["work_dir"] = _path(cfg["work_dir"], "work_dir")
    work = Path(cfg["work_dir"])
    derived = {"deliver_dir": work / "交付", "cards_dir": work / "任务卡",
               "ledger_file": work / "账本.jsonl", "perf_file": work / "表现记录.jsonl",
               "runtime_dir": work / ".runtime" / "opencode",
               "opencode_config": Path(__file__).resolve().parents[2] / "workers" / "opencode" / "opencode.worker.jsonc"}
    for name, default in derived.items():
        # 账本和交付位置固定在 work_dir 内，仅契约允许的字段可覆盖。
        value = str(default) if name in ("deliver_dir", "ledger_file") else cfg.get(name, str(default))
        cfg[name] = _path(value, name)
    for name in ("key_env_file", "models_snapshot_source"):
        if name in cfg:
            cfg[name] = _path(cfg[name], name)
    return cfg


def path_of(name):
    """公共路径接口仅接受契约五个名字，不创建目录，保证只读调用无副作用。"""
    if name not in ("work_dir", "deliver_dir", "cards_dir", "ledger_file", "perf_file"):
        raise ValueError("未知路径名称：" + name)
    return load()[name]


def ensure_layout():
    """启动工人时创建数据目录；账本和表现文件留到有记录时再写。"""
    cfg = load()
    for name in ("work_dir", "deliver_dir", "cards_dir"):
        Path(cfg[name]).mkdir(parents=True, exist_ok=True)
    Path(cfg["perf_file"]).parent.mkdir(parents=True, exist_ok=True)
    return cfg


def _write(cfg, exclusive=False):
    """同目录临时文件落盘后原子发布；初始化用硬链接保证不覆盖并发创建。"""
    _normalize(cfg)
    data = (json.dumps(cfg, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    path = Path(_path(os.environ.get("FRCLYO_CONFIG", "~/.frclyo/config.json"), "FRCLYO_CONFIG"))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save(cfg):
    """整份保存传入配置，0600 权限；校验失败不影响旧文件。"""
    _write(cfg)


def init_default(overrides):
    """仅首次生成默认配置；已有文件保持原字节，返回公共读取结果。"""
    path = Path(_path(os.environ.get("FRCLYO_CONFIG", "~/.frclyo/config.json"), "FRCLYO_CONFIG"))
    if path.exists():
        return load()
    # 先合并默认字段，再保存未展开的用户路径，便于配置随 HOME 移动。
    _normalize(overrides)
    value = copy.deepcopy(DEFAULTS)
    value.update(copy.deepcopy(overrides))
    value["codex"] = dict(DEFAULTS["codex"], **value["codex"])
    value["providers"] = {name: dict(DEFAULTS["providers"].get(name, {}), **entry)
                          for name, entry in value["providers"].items()}
    try:
        _write(value, exclusive=True)
    except FileExistsError:
        pass
    return load()
