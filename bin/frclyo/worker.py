"""执行脚本内部桥接：路径、权限模板、模型快照与仅内存凭据注入。"""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit
from . import config, keys


def sensitive_paths(cfg):
    """配置、Codex 登录目录和兼容凭据文件搬家后也必须显式禁读。"""
    paths = [str(Path(os.environ.get("FRCLYO_CONFIG", "~/.frclyo/config.json")).expanduser().absolute()),
             str(config.codex_home())]
    if cfg.get("key_env_file"):
        paths.append(cfg["key_env_file"])
    return paths


def read_jsonc(path):
    """只移除字符串外的注释，保留 URL 的 //；不执行配置中的任何代码。"""
    source = Path(path).read_text(encoding="utf-8")
    out, index, quoted, escaped = [], 0, False, False
    while index < len(source):
        char = source[index]
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            out.append(char)
        elif source[index:index + 2] == "//":
            end = source.find("\n", index)
            index = len(source) if end < 0 else end
            out.append("\n")
            continue
        elif source[index:index + 2] == "/*":
            end = source.find("*/", index + 2)
            if end < 0:
                raise ValueError("配置注释未结束")
            out.append(" ")
            index = end + 2
            continue
        else:
            out.append(char)
        index += 1
    return json.loads("".join(out))


def snapshot(cfg, family, model):
    """仅复制本机 OpenCode models.dev 缓存；不下载、不把模型目录提交仓库。"""
    destination = Path(cfg["runtime_dir"]) / "models.snapshot.json"
    source = Path(cfg.get("models_snapshot_source", str(
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "opencode" / "models.json")))
    chosen = destination if destination.is_file() else source
    remedy = ("模型快照缺失、损坏或不含本轮模型。请在工人外运行 opencode models --refresh，"
              "再重试；自定义缓存可在配置中设置 models_snapshot_source 指向 models.dev JSON。"
              "已有旧快照时删除 runtime_dir/models.snapshot.json 后重试。")
    try:
        data = chosen.read_bytes()
        catalog = json.loads(data)
        entry = catalog[family]
        if not isinstance(entry, dict) or model not in entry["models"] or not isinstance(entry["models"][model], dict):
            raise ValueError()
        if not isinstance(entry.get("npm"), str) or not isinstance(entry.get("api"), str):
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError(remedy) from None
    if chosen != destination:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 同目录临时文件再原子替换，不让并发首跑读到只复制一半的快照。
        fd, temp = tempfile.mkstemp(prefix=".models-", dir=str(destination.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, destination)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return str(destination), entry


def prepare(model):
    """只启用本轮一家；沿用模型白名单/变体/权限，仅把主机和凭据引用参数化。"""
    cfg = config.ensure_layout()
    provider, model_id = model.split("/", 1)
    if provider not in cfg["providers"]:
        raise ValueError("不允许的提供方")
    entry = cfg["providers"][provider]
    family = entry["opencode_family"]
    template = read_jsonc(cfg["opencode_config"])
    settings = copy.deepcopy(template.get("provider", {}).get(family, {}))
    if settings.get("whitelist") is not None and model_id not in settings["whitelist"]:
        raise ValueError("模型不在工人配置白名单内")
    models_path, catalog = snapshot(cfg, family, model_id)
    # 路径沿用配置模板或本机模型目录，主机唯一来自 providers.<家>.api_host。
    options = settings.setdefault("options", {})
    endpoint = urlsplit(options.get("baseURL", catalog["api"]))
    if endpoint.scheme != "https" or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("模型 API 地址必须为无凭据的 HTTPS 地址")
    options["baseURL"] = urlunsplit(("https", entry["api_host"], endpoint.path, "", ""))
    options["apiKey"] = "{env:FRCLYO_PROVIDER_API_KEY}"
    template["enabled_providers"] = [family]
    template["provider"] = {family: settings}
    return {"deliver": cfg["deliver_dir"], "runtime": cfg["runtime_dir"],
            "template": cfg["opencode_config"], "snapshot": models_path,
            "target": entry["api_host"] + ":443", "model": family + "/" + model_id,
            "content": json.dumps(template, ensure_ascii=False),
            "protected": json.dumps([cfg["work_dir"], cfg["runtime_dir"], cfg["opencode_config"]], ensure_ascii=False),
            "sensitive": json.dumps(sensitive_paths(cfg), ensure_ascii=False)}


def inject_key(provider):
    """监督器调用；key 不经 stdout、argv、临时文件，只进入工人环境。"""
    value = keys.get_key(provider)
    if not value:
        raise ValueError("未找到本轮提供方的 key；请先运行 aw key add 或配置 key_env_file")
    # 清除继承环境中的其他凭据；不把派活方的 API key 顺带暴露给子命令。
    for name in list(os.environ):
        if any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            del os.environ[name]
    os.environ["FRCLYO_PROVIDER_API_KEY"] = value


def main():
    """私有 shell 桥接只输出不含凭据的 JSON 标量；绝不提供打印 key 的子命令。"""
    try:
        mode = sys.argv[1]
        if mode == "codex":
            cfg = config.ensure_layout()
            print(json.dumps({"deliver": cfg["deliver_dir"], "runtime": cfg["runtime_dir"],
                              "proxy": cfg["codex"]["proxy"], "fast": cfg["codex"]["default_fast"],
                              "sensitive": json.dumps(sensitive_paths(cfg), ensure_ascii=False)}))
        elif mode == "opencode":
            print(json.dumps(prepare(sys.argv[2]), ensure_ascii=False))
        else:
            raise ValueError("未知内部命令")
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        # 错误文本不包含配置值；详细补救文字固定，防止敏感配置进入日志。
        print("运行时准备失败：检查配置/提供方/模型白名单；模型快照需来自本机 OpenCode。"
              "请运行 opencode models --refresh，或设置 models_snapshot_source；"
              "旧快照不含模型时删除 runtime_dir/models.snapshot.json 后重试。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
