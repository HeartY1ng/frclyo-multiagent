"""钥匙只在内存中传递；读取失败返回 None，不输出命令错误或秘密。"""
import re
import subprocess
from . import config


# 仅兼容历史变量名；新增家在 .env 中使用大写提供方名加 _API_KEY。
ENV_NAMES = {"deepseek": "DEEPSEEK_API_KEY", "moonshotai-cn": "KIMI_CN_API_KEY", "xiaomi": "XIAOMI_API_KEY"}


def list_providers():
    """按实际取 key 的规则检查（含回退文件），仅返回名字，不返回凭据。"""
    return [provider for provider in config.load()["providers"] if get_key(provider)]


def get_key(provider):
    """优先钥匙串；找不到才读取显式配置的 .env，绝不执行其内容。"""
    cfg = config.load()
    if provider not in cfg["providers"]:
        return None
    try:
        result = subprocess.run(["security", "find-generic-password", "-s", "frclyo-" + provider,
                                 "-a", "frclyo", "-w"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=10)
        value = result.stdout.rstrip("\r\n")
        if result.returncode == 0 and value:
            return value
    except (OSError, UnicodeError, subprocess.SubprocessError):
        pass
    path = cfg.get("key_env_file")
    if not path:
        return None
    name = ENV_NAMES.get(provider, provider.upper().replace("-", "_") + "_API_KEY")
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                match = re.fullmatch(r"\s*(?:export\s+)?" + re.escape(name) + r"\s*=\s*(.*?)\s*", line)
                if match:
                    value = match[1]
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    return value or None
    except (OSError, UnicodeError):
        pass
    return None


def set_key(provider):
    """确认后由 security 隐藏输入；返回是否新增成功，记账由 aw 负责。"""
    if provider not in config.load()["providers"]:
        raise ValueError("提供方未配置")
    if input("将向钥匙串新增 frclyo-%s，继续？[y/N] " % provider).strip().lower() not in ("y", "yes"):
        return False
    # 不覆盖同名条目；密码不经过本程序、argv、日志或安装记录。
    result = subprocess.run(["security", "add-generic-password", "-s", "frclyo-" + provider,
                             "-a", "frclyo", "-w"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError("钥匙串写入失败；已有同名条目时请先核对")
    return True


def remove_key(provider):
    """只撤销本工具的钥匙串条目；移出配置的旧家仍可卸载，不触碰回退文件。"""
    if not isinstance(provider, str) or not re.fullmatch(r"[a-z0-9-]+", provider):
        raise ValueError("提供方名字无效")
    result = subprocess.run(["security", "delete-generic-password", "-s", "frclyo-" + provider,
                             "-a", "frclyo"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=10)
    if result.returncode == 44:
        return False
    if result.returncode:
        raise RuntimeError("钥匙串删除失败")
    return True
