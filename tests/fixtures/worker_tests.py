# 假工人也交真实测试，验证第二关确实重新执行测试而不是只看文件名。
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class WorkerTests(unittest.TestCase):
    """覆盖契约要求的四种独立边界。"""
    def call(self, data):
        script = Path(__file__).name.replace('test_', '')
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            source, output = Path(temp) / 'input.csv', Path(temp) / 'out.csv'
            source.write_bytes(data)
            result = subprocess.run([sys.executable, script, str(source), str(output)], capture_output=True)
            return result.returncode, output.read_bytes() if output.exists() else b''

    def test_empty(self):
        self.assertEqual(self.call(b'name,category,amount\n')[0], 0)

    def test_header(self):
        self.assertNotEqual(self.call(b'wrong\n')[0], 0)

    def test_number(self):
        self.assertNotEqual(self.call(b'name,category,amount\na,b,-1\n')[0], 0)

    def test_comma(self):
        self.assertEqual(self.call(b'name,category,amount\n"a,b",c,1\n')[0], 0)
