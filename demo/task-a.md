项目：frclyo-demo
# 子任务 A：CSV 规范化

类型 T-code；C1；V1；L1；风险低；X1。超时 240 秒，允许再拆 0 层。
先完整阅读 `输入/CONTRACT.md`。只做 A，独立实现，不等待 B。
输入材料在本轮 `输入/`：raw.csv 和 normalized.expected.csv。
按契约交付 normalize.py、test_normalize.py、normalized.sample.csv，文件均直接放本轮目录。
实际运行程序生成样例，并执行 `python3 -m unittest -v test_normalize`。
不能修改输入材料，也不能读写其他轮次或仓库。Python 3.9 标准库，每段中文注释。
验收以契约三关为准。最后说明结论、产物、自检实际输出、决策披露、状态。
