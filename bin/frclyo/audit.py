"""只读核对账本的完成声明与最新轮次证据。"""
import hashlib
import json
import sys
import time
from .ledger import LedgerError, Paths, fold, read_events, round_numbers


def _check_paths(paths):
    """只核对账本通过卡的最新轮次，返回可打印报告。"""
    cards = fold(read_events(paths.ledger))
    targets = {key: c["标题"] for key, c in cards.items()
               if c["状态"] == "完成" and c["结论"] == "通过"}
    lines, problems = [], []
    for key, title in sorted(targets.items()):
        try:
            rounds = round_numbers(paths.deliver, key)
            if not rounds:
                raise LedgerError("没有交付轮次，也没有回执")
            run_dir = paths.deliver / key / rounds[-1][1]
            # 不使用完成记录里的老轮次路径，确保 r10 的坏证据不能被 r9 的好证据掩盖。
            receipt_path = run_dir / "回执.json"
            if not receipt_path.is_file():
                raise LedgerError("最新一轮 %s 没有回执" % rounds[-1][1])
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                raise LedgerError("回执不是 JSON 对象")
            result = run_dir / "结果.txt"
            if not result.is_file():
                raise LedgerError("最新一轮没有结果.txt，无法核对校验码")
            sha = hashlib.sha256(result.read_bytes()).hexdigest()
            same = sha == receipt.get("结果sha256")
            if receipt.get("状态") != "执行完成" or not same:
                raise LedgerError("回执状态是「%s」、校验码%s" %
                                  (receipt.get("状态"), "一致" if same else "不一致"))
        except (LedgerError, OSError, ValueError, UnicodeError) as exc:
            lines.append("❌ %s「%s」：%s" % (key, title, exc))
            problems.append(lines[-1])
        else:
            lines.append("✅ %s「%s」：最新一轮 %s 回执和校验码都对得上" % (key, title, rounds[-1][1]))
    return lines, problems, len(targets)



def audit(paths):
    """保留账本 CLI 原报告接口，公共结构化结果由 check 提供。"""
    lines, problems, count = _check_paths(paths)
    lines.append("已核对完成卡 %d 张，问题 %d 个" % (count, len(problems)))
    return lines, len(problems)


def check():
    """只读日审；损坏的配置或账本作为问题返回，不能伪装成健康。"""
    report = {"时间": time.time(), "核对卡数": 0, "问题": [], "提醒": []}
    try:
        _, report["问题"], report["核对卡数"] = _check_paths(Paths())
    except (LedgerError, OSError, ValueError, UnicodeError) as exc:
        report["问题"] = ["自述与证据核对失败：%s" % exc]
    return report


def main():
    """命令行与进程内返回同一 JSON 结构；发现问题时退出非零。"""
    report = check()
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["问题"] else 0


if __name__ == "__main__":
    sys.exit(main())
