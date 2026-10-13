# 校准题的独立评分测试，不依赖尚待确认的表现记录 schema 或模型发现接口。
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('calibrate_checker', ROOT / 'demo/check.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

# 正确实现只存在于测试里，派给真实工人的材料没有参考答案。
SOLUTIONS = {
    'sort': '# 排序保留空行和重复项。\nimport sys\nrows=sorted(sys.stdin.read().splitlines())\nsys.stdout.write("".join(row+"\\n" for row in rows))\n',
    'sum': '# 严格检查整数，排除 bool。\nimport json,sys\ntry:\n x=json.load(sys.stdin)\n assert type(x) is list and all(type(v) is int for v in x)\nexcept Exception:\n sys.exit(1)\nprint(sum(x))\n',
    'runs': '# Unicode 连续游程输出紧凑 JSON。\nimport json,sys,itertools\ntry:\n x=json.load(sys.stdin)\n assert type(x) is str\nexcept Exception:\n sys.exit(1)\nprint(json.dumps([[c,len(list(v))] for c,v in itertools.groupby(x)],ensure_ascii=False,separators=(",",":")))\n'
}


class CalibrationTests(unittest.TestCase):
    """既检查好实现通过，也检查常见错误不能蒙混过关。"""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.calibrate-test-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.questions = json.loads((ROOT / 'demo/calibrate/questions.json').read_text(encoding='utf-8'))

    def test_all_reference_answers_pass(self):
        for question in self.questions:
            with self.subTest(question=question['id']):
                (self.directory / 'solve.py').write_text(SOLUTIONS[question['id']], encoding='utf-8')
                check.grade(self.directory, question)

    def test_empty_output_rejected_for_every_question(self):
        (self.directory / 'solve.py').write_text('# 不合格实现：没有输出\n')
        for question in self.questions:
            with self.subTest(question=question['id']), self.assertRaises(check.CheckError):
                check.grade(self.directory, question)

    def test_bool_is_not_integer(self):
        (self.directory / 'solve.py').write_text('# 故障：把 bool 当成 int\nimport sys,json\nprint(sum(json.load(sys.stdin)))\n')
        with self.assertRaises(check.CheckError):
            check.grade(self.directory, self.questions[1])

    def test_symbolic_link_artifact_rejected(self):
        (self.directory / 'solve.py').symlink_to(ROOT / 'demo/check.py')
        with self.assertRaises(check.CheckError):
            check.grade(self.directory, self.questions[0])


if __name__ == '__main__':
    unittest.main()
