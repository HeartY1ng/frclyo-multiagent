# 离线假工人的可执行交付物；只在临时 HOME 中使用，不会调用模型。
import csv
from pathlib import Path
import re
import sys


def main():
    """用独立小实现产生真实 CSV，给三关验收测试提供合格基线。"""
    source, destination = map(Path, sys.argv[1:])
    summarize = Path(__file__).name == 'summarize.py'
    if source.resolve() == destination.resolve():
        raise ValueError('行 1：不能覆盖输入')
    with source.open(encoding='utf-8', newline='') as stream:
        rows = list(csv.reader(stream))
    if not rows or rows[0] != ['name', 'category', 'amount']:
        raise ValueError('行 1：表头不符')
    output, groups = [], {}
    for number, row in enumerate(rows[1:], 2):
        if len(row) != 3:
            raise ValueError('行 %d：列数不符' % number)
        name, category, amount = row
        if summarize and (name != name.strip() or category != category.strip().lower() or not re.fullmatch(r'0|[1-9][0-9]*', amount)):
            raise ValueError('行 %d：未规范化' % number)
        name, category, amount = name.strip(), category.strip().lower(), amount.strip()
        if not name or not category or not re.fullmatch(r'[0-9]+', amount) or int(amount) > 1000000:
            raise ValueError('行 %d：字段不合法' % number)
        output.append([name, category, str(int(amount))])
        count, total = groups.get(category, (0, 0))
        groups[category] = count + 1, total + int(amount)
    if summarize:
        output = [[cat, str(count), str(total)] for cat, (count, total) in sorted(groups.items())]
    # E2E_MUTATION：故障注入只改测试复制品。
    header = ['category', 'count', 'total'] if summarize else ['name', 'category', 'amount']
    with destination.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream, lineterminator='\n')
        writer.writerow(header)
        writer.writerows(output)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
