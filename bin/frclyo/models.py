"""离线发现本机模型候选；缓存列出不代表当前 CLI 已验证可用。"""
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from . import config, keys
from .worker import read_jsonc


def cli_version(worker):
    """版本来自实际 CLI，不能使用 App 写入的缓存版本。查询失败不复用旧结论。"""
    if worker not in ("codex", "opencode"):
        raise ValueError("未知工人")
    try:
        result = subprocess.run([worker, "--version"], capture_output=True, text=True, timeout=10)
        lines = result.stdout.strip().splitlines()
        if result.returncode == 0 and lines:
            return lines[0].split()[-1]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def _path():
    return Path(config.path_of("work_dir")) / "模型可用性.json"


@contextlib.contextmanager
def _locked(path, write=False):
    """稳定的旁路 flock 覆盖读/改/原子替换；离线首次发现不创建工作目录。"""
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = path.with_suffix(".lock").open("a+b" if write else "rb")
    except FileNotFoundError:
        # 尚无写入者；原子替换保证即使此刻首次写入也不会读到半份 JSON。
        yield
        return
    with stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("records"), list):
        raise ValueError("模型可用性记录格式无效")
    for row in data["records"]:
        if (not isinstance(row, dict) or row.get("status") not in ("ok", "unusable") or
                any(not isinstance(row.get(k), str) for k in
                    ("worker", "model", "cli_version", "time", "reason"))):
            raise ValueError("模型可用性记录格式无效")
    return data["records"]


def records():
    """读取可用性快照；调用者不能修改磁盘记录。"""
    path = _path()
    with _locked(path):
        return _read(path)


def record(worker, model, version, status, reason):
    """每个 (工人, 模型, CLI 版本) 保存最新结论，旧版本留作证据。"""
    if worker not in ("codex", "opencode") or status not in ("ok", "unusable"):
        raise ValueError("模型可用性结论无效")
    if any(not isinstance(v, str) or not v for v in (model, version, reason)):
        raise ValueError("模型可用性字段无效")
    row = dict(worker=worker, model=model, cli_version=version, status=status,
               time=datetime.now(timezone.utc).isoformat(), reason=reason)
    path = _path()
    with _locked(path, write=True):
        rows = [r for r in _read(path) if (r["worker"], r["model"], r["cli_version"]) !=
                (worker, model, version)]
        rows.append(row)
        fd, temporary = tempfile.mkstemp(prefix=".availability-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "records": rows}, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return row


def availability(worker, model, version, rows=None):
    """只信当前版本；版本未知时也不能把历史失败当成当前失败。"""
    if version != "unknown":
        for row in records() if rows is None else rows:
            if (row["worker"], row["model"], row["cli_version"]) == (worker, model, version):
                return row["status"]
    return "unknown"


def rank(candidates, include_unusable=False):
    """在同一快照上稳定排序，保留同状态候选的发现顺序。"""
    rows, versions, found = records(), {}, []
    for candidate in candidates:
        worker = candidate["worker"]
        if worker not in versions:
            versions[worker] = cli_version(worker)
        version = versions[worker]
        status = availability(worker, candidate["model"], version, rows)
        if status != "unusable" or include_unusable:
            found.append(dict(candidate, cli_version=version, availability=status))
    return sorted(found, key=lambda row: {"ok": 0, "unknown": 1, "unusable": 2}[row["availability"]])


def unsupported_reason(directory):
    """仅识别错误事件中的模型拒绝；不把档位警告或一般 HTTP 400 当成不可用。"""
    try:
        stream = (Path(directory) / "events.jsonl").open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    with stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            item = event.get("item")
            if kind not in ("error", "turn.failed") and not (
                    kind == "item.completed" and isinstance(item, dict) and item.get("type") == "error"):
                continue
            # 嵌套 JSON 字符串也包含在序列化结果里；只匹配语义，不依赖模型名字。
            message = json.dumps(event, ensure_ascii=False).lower().replace('\\"', '"')
            direct = re.search(r"\bmodel\b(?:\s+['\"`][^'\"`]+['\"`])?\s+(?:is\s+)?(?:not\s+supported|unsupported|not\s+available)\b|\bunsupported\s+model\b", message)
            context = re.search(r"\b400\b|\baccount\b|\bcli\b|\bcodex\b", message)
            if any(feature in message for feature in ("service tier", "reasoning effort", "response_format", "tool calls")):
                continue
            if "model_not_supported" in message or (direct and context):
                return "模型不被当前账号或 CLI 支持（错误事件）；证据=" + str(Path(directory) / "events.jsonl")
    return None


def observe(worker, model, version, directory, succeeded=False):
    """可用性与产物质量分开：普通失败不改状态；证据只保留路径，避免复制敏感日志。"""
    reason = unsupported_reason(directory)
    if reason:
        record(worker, model, version, "unusable", reason)
        return "unusable"
    if succeeded:
        record(worker, model, version, "ok", "工人正常完成；回执=" + str(Path(directory) / "回执.json"))
        return "ok"
    return "unknown"


def discover(include_unusable=False):
    """仅查已安装工人、Codex 缓存与 OpenCode 白名单，不下载模型目录。"""
    cfg, found = config.load(), []
    if shutil.which("codex"):
        path = config.codex_home() / "models_cache.json"
        try:
            catalog = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            catalog = {"models": []}
        if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
            raise ValueError("Codex 模型缓存格式无效；请刷新 CLI 登录和模型缓存")
        for model in catalog["models"]:
            if not isinstance(model, dict) or model.get("visibility") != "list":
                continue
            slug = model.get("slug")
            if not isinstance(slug, str) or not slug:
                continue
            levels = [item["effort"] for item in model.get("supported_reasoning_levels", [])
                      if isinstance(item, dict) and isinstance(item.get("effort"), str)]
            found.append({"worker": "codex", "model": slug, "levels": levels,
                          "provider": None, "key_ok": True, "source": "models_cache"})
    if shutil.which("opencode"):
        template = read_jsonc(cfg["opencode_config"])
        for provider, entry in cfg["providers"].items():
            family = entry["opencode_family"]
            settings = template.get("provider", {}).get(family, {})
            if family not in template.get("enabled_providers", []):
                continue
            # 只采用模板的显式白名单，不把整个提供商目录默认开放。
            available = bool(keys.get_key(provider))
            for name in settings.get("whitelist", []):
                if not isinstance(name, str) or not name:
                    continue
                variants = settings.get("models", {}).get(name, {}).get("variants", {})
                levels = ["", *[level for level, value in variants.items()
                                if not value.get("disabled", False)]]
                found.append({"worker": "opencode", "model": provider + "/" + name,
                              "levels": levels, "provider": provider, "key_ok": available,
                              "source": "worker_config"})
    return rank(found, include_unusable=include_unusable)
