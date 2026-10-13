项目：frclyo-demo
# 子任务 B：CSV 分类统计

类型 T-code；C1；V1；L1；风险低；X1。超时 240 秒，允许再拆 0 层。
先完整阅读 `输入/CONTRACT.md`。只做 B，独立实现，不等待 A。
输入材料在本轮 `输入/`：normalized.expected.csv 和 summary.expected.csv。
按契约交付 summarize.py、test_summarize.py、summary.sample.csv，文件均直接放本轮目录。
实际运行程序生成样例，并执行 `python3 -m unittest -v test_summarize`。
不能修改输入材料，也不能读写其他轮次或仓库。Python 3.9 标准库，每段中文注释。
验收以契约三关为准。最后说明结论、产物、自检实际输出、决策披露、状态。
