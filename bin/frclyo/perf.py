"""表现记录只保存可观察事实；未知字段填 null，不估算用量或费用。"""
import json
from . import config
from .ledger import LedgerLock

# 固定 v1.2 字段；忽略调用方额外内部字段，避免任意数据流入共享记录。
FIELDS = ("时间", "任务号", "轮次", "任务类型", "类型", "难度", "耦合度", "可验证性",
          "工人", "模型", "档位", "用时秒", "token用量", "费用美元", "通过验收", "备注")


def append(record):
    """复用账本的进程锁、短写处理和 fsync；先验证再创建任何文件。"""
    if not isinstance(record, dict):
        raise ValueError("表现记录必须为对象")
    row = {name: record.get(name) for name in FIELDS}
    if row["token用量"] is not None and not isinstance(row["token用量"], dict):
        raise ValueError("token用量 必须为对象或 null")
    if row["通过验收"] is not None and type(row["通过验收"]) is not bool:
        raise ValueError("通过验收 必须为布尔值或 null")
    if row["费用美元"] is not None:
        if type(row["费用美元"]) not in (int, float):
            raise ValueError("费用美元 必须为数值或 null")
        row["费用美元"] = float(row["费用美元"])
    json.dumps(row, allow_nan=False)
    with LedgerLock(config.path_of("perf_file")) as lock:
        lock.append([row])
