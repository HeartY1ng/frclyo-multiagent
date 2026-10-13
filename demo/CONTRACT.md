# CSV 演示契约 v1

目标：A 规范化 CSV，B 按类别统计；双方只通过此契约和固定样例衔接，不读取对方代码。Python 3.9 标准库，代码每段用中文注释。只能写本轮交付目录，不能联网，不能继续拆任务。

## 输入与接口

所有 CSV 为 UTF-8、无 BOM，输出严格 LF，含表头、末行含 LF，用 Python csv 的最小引号规则。命令均接收两个位置参数：输入文件、输出文件。

A：`python3 normalize.py raw.csv normalized.csv`
- 输入表头严格为 `name,category,amount`。每行恰好三列。
- name 去除首尾空白，category 去除首尾空白后转小写；两者不能为空。
- amount 去除首尾空白后必须匹配 `[0-9]+`，范围 0 到 1000000，按十进制整数输出（去除前导零）。
- 保持输入行序、保留重复行。空数据只输出表头。不允许跳过坏行。

B：`python3 summarize.py normalized.csv summary.csv`
- 输入必须已经规范化：同一表头、非空且没有首尾空白的 name/category，category 小写，amount 必须是 `0` 或 `[1-9][0-9]*`，不超过 1000000。
- 输出表头 `category,count,total`，按 category 的 Unicode 码位升序排序。count 是行数，total 是整数总和，无前导零。
- 空数据只输出表头；含逗号的字段由 csv 正确引用。

两者遇坏表头、坏列数、空字段、非法数字或非规范化输入，stderr 报出行号和原因，非零退出，不留下部分输出（先验证全部数据）。输入与输出不得是同一文件。

## 独立样例与交付

`raw.csv` → `normalized.expected.csv` → `summary.expected.csv` 为固定样例。另有 `e2e.raw.csv` 和 `e2e.summary.expected.csv` 用于集成复验。

- A 只交付 `normalize.py`、`test_normalize.py`、`normalized.sample.csv`。
- B 只交付 `summarize.py`、`test_summarize.py`、`summary.sample.csv`。
- 测试使用 unittest，必须覆盖空输入数据、坏表头、坏数字和逗号字段；测试至少 4 项。
- 样例产物必须由自己程序产生，不可复制期望文件冒充运行结果。
- 交付说明写入正常最终回复：结论、产物、自检证据、决策披露、状态。

## 集成者三关

1. 两份交付样例，以及再次运行各自程序的结果，与固定期望逐字节比较。
2. 独立复跑双方的 unittest（至少各 4 项）；同时验证坏输入和空数据。
3. 将 A 对 e2e.raw.csv 的输出交给 B，最终结果与固定期望逐字节比较。

任一关失败，父卡和子卡标记卡住，不能报完成。结果保留在工作目录，演示无需并入项目仓库。
