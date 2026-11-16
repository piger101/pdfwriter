import builtins
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pypdf import PdfReader, PdfWriter

# 被测脚本的位置：与本测试文件同目录，不依赖当前工作目录
SCRIPT_PATH = Path(__file__).resolve().with_name("pdf_extract.py")


# ============================================================================
# 一、测试基础设施
# ============================================================================

@contextlib.contextmanager
def feed_input(*answers):
    """临时把 input() 换掉，按顺序返回 answers 里的字符串。

    为什么需要它：被测脚本导入时就要 3 个输入，main() 里还有确认输入。
    真实 input() 会阻塞在 stdin，测试跑不下去。

    yield 出去的是 prompts 列表，测试可以反过来断言
    "脚本到底问了哪些问题"（比如检查提示语里有没有给格式示例）。
    """
    queue = list(answers)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        if not queue:
            # 答案用完了但脚本还要输入 —— 说明脚本的提问次数变了，
            # 这本身是个信号，直接报错比让它卡住更好。
            raise AssertionError(
                f"脚本还要更多输入，但测试只准备了 {len(answers)} 个。"
                f"最后一次提示语：{prompt!r}"
            )
        return queue.pop(0)

    real_input = builtins.input
    builtins.input = fake_input
    try:
        yield prompts
    finally:
        builtins.input = real_input


def load_module():
    """导入被测脚本并返回模块对象。

    必须包在 feed_input(...) 里调用，因为模块顶层有 3 次 input()。
    用 importlib 而不是 `import pdf_extract`，是为了：
      - 不依赖 sys.path / 当前工作目录
      - 每次都能拿到一个全新的模块（全局变量不会互相污染）
    """
    spec = importlib.util.spec_from_file_location("pdf_extract_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_parse_module():
    """只为了测 parse_page_range 而加载模块，喂 3 个无意义的答案。"""
    with feed_input("dummy.pdf", "dummy_out.pdf", "1"):
        return load_module()


def run_main(module):
    """执行 main()，把 SystemExit 转成退出码，并收集 stdout。

    返回 (退出码, 打印内容)：
        退出码为 None  -> main() 正常跑完（成功）
        退出码为 0/1/2 -> 脚本主动 sys.exit() 了
    非 SystemExit 的异常故意不捕获，让它以"测试出错"的形式暴露出来，
    这样能直接看到 traceback 指向被测脚本的哪一行。
    """
    buffer = io.StringIO()
    code = None
    with contextlib.redirect_stdout(buffer):
        try:
            module.main()
        except SystemExit as exc:
            code = exc.code if exc.code is not None else 0
    return code, buffer.getvalue()


def make_pdf(path, pages=5, width_base=100):
    """造一个测试用 PDF：每页尺寸都不同，用来识别"输出里到底是哪一页"。

    这是个关键技巧：空白页内容完全一样，没法靠肉眼或文本区分。
    让第 i 页（0 开始）宽度 = width_base + i，读回来量一下宽度，
    就知道提取出来的是第几页、顺序对不对。
    """
    writer = PdfWriter()
    for i in range(pages):
        writer.add_blank_page(width=width_base + i, height=200)
    with open(path, "wb") as f:
        writer.write(f)


def make_encrypted_pdf(path, password, pages=3, width_base=100):
    """造一个需要密码才能读的 PDF。"""
    writer = PdfWriter()
    for i in range(pages):
        writer.add_blank_page(width=width_base + i, height=200)
    writer.encrypt(password)
    with open(path, "wb") as f:
        writer.write(f)


def page_widths(path):
    """读回 PDF，返回每页宽度列表 —— 等价于"提取出来的是哪几页"。"""
    reader = PdfReader(path)
    return [round(float(page.mediabox.width)) for page in reader.pages]


# ============================================================================
# 二、parse_page_range：纯函数测试
# ============================================================================
# 这一层最值得多写：它不碰文件、不打印、不退出，纯输入输出，
# 而且"页码解析"正是用户最容易输错、也最容易出 bug 的地方。
# ============================================================================

class TestParsePageRange(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.module = load_parse_module()

    def parse(self, text, total=10):
        return self.module.parse_page_range(text, total)

    # ---------- 正常格式 ----------

    def test_单个页码(self):
        """用户输入 3，应得到 0 基索引 [2]。"""
        self.assertEqual(self.parse("3"), [2])

    def test_连续区间(self):
        """1-5 是闭区间，应包含 5，共 5 个索引。"""
        self.assertEqual(self.parse("1-5"), [0, 1, 2, 3, 4])

    def test_逗号分隔的多个页码(self):
        self.assertEqual(self.parse("1,3,5"), [0, 2, 4])

    def test_混合格式_单页加区间(self):
        """这是原先"一个 START/END"根本表达不了的场景，也是本次改造的核心目的。"""
        self.assertEqual(self.parse("2,4-6,10"), [1, 3, 4, 5, 9])

    def test_中文逗号顿号和空格都能当分隔符(self):
        """中文输入法下很容易打出 ，和 、，不该让用户为此报错。"""
        self.assertEqual(self.parse("1，3、5 7"), [0, 2, 4, 6])

    def test_首尾多余分隔符(self):
        self.assertEqual(self.parse(",1,3,"), [0, 2])

    def test_整篇提取(self):
        self.assertEqual(self.parse("1-10", total=10), list(range(10)))

    # ---------- 宽容的输入 ----------

    def test_倒序区间自动纠正(self):
        """用户写 5-3，直觉上想取 3、4、5，不该报错。"""
        self.assertEqual(self.parse("5-3"), [2, 3, 4])

    def test_重复页码自动去重(self):
        """1,1,3 应该只出 2 页，不是 3 页。"""
        self.assertEqual(self.parse("1,1,3"), [0, 2])

    def test_区间和单页重叠也去重(self):
        self.assertEqual(self.parse("2-5,4,4"), [1, 2, 3, 4])

    def test_乱序输入按页码升序输出(self):
        """决定：输出顺序 = 页码升序，不是用户输入顺序。

        想改成"按输入顺序"的话，这个测试就是第一个要改的地方。
        """
        self.assertEqual(self.parse("9,2,5"), [1, 4, 8])

    # ---------- 空输入 ----------

    def test_空字符串返回空列表而不是报错(self):
        """空输入由调用方（main）判断并提示，解析器本身不越权。"""
        self.assertEqual(self.parse(""), [])

    def test_全是分隔符也返回空列表(self):
        self.assertEqual(self.parse("  ，，  "), [])

    # ---------- 错误输入 ----------

    def test_单页超出总页数(self):
        with self.assertRaises(ValueError) as ctx:
            self.parse("11", total=10)
        # 报错信息要能指出"具体哪一页越界"和"总共几页"，
        # 这是原脚本"页码范围不合法"做不到的。
        self.assertIn("11", str(ctx.exception))
        self.assertIn("10", str(ctx.exception))
        self.assertIn("超出范围", str(ctx.exception))

    def test_区间里有一页越界(self):
        """1-11 里的 11 越界，整段拒绝（不做部分提取）。"""
        with self.assertRaises(ValueError) as ctx:
            self.parse("1-11", total=10)
        self.assertIn("11", str(ctx.exception))

    def test_页码0非法(self):
        """用户输入从 1 开始，0 属于非法输入，不能静默当成第 1 页。"""
        with self.assertRaises(ValueError):
            self.parse("0")

    def test_非法文本统一报错(self):
        """各种脏输入都应走同一个 ValueError 分支，不能被 int() 的半吊子行为放过。"""
        for bad in ("abc", "1.5", "1-3abc", "-3", "一", "1..3"):
            with self.subTest(输入=bad):
                with self.assertRaises(ValueError):
                    self.parse(bad)

    def test_错误信息里带上了原始片段(self):
        """报错要能让用户定位到是哪一段输错了。"""
        with self.assertRaises(ValueError) as ctx:
            self.parse("2,abc,5")
        self.assertIn("abc", str(ctx.exception))


# ============================================================================
# 三、main() 集成测试：真建 PDF、真跑、真读回结果
# ============================================================================

class TestMainExtract(unittest.TestCase):

    def setUp(self):
        # 每个测试都在独立的临时目录里跑，避免污染用户的文件夹
        # （脚本用相对路径，所以 chdir 是"喂不同文件名"最省事的方式）
        self._original_cwd = os.getcwd()
        self.workdir = tempfile.mkdtemp(prefix="pdf_extract_test_")
        os.chdir(self.workdir)
        make_pdf("src.pdf", pages=5)  # 5 页，宽度 100~104

    def tearDown(self):
        os.chdir(self._original_cwd)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def run_script(self, *, input_name="src.pdf", output_name="out.pdf",
                   page_text="2,4", tail=("y",)):
        """跑一遍脚本。

        参数：
            input_name  / output_name / page_text -> 模块导入时吃掉的 3 个输入
            tail        -> 导入之后脚本还会依次吃掉的所有输入
                           （通常是确认 y/n；加密 PDF 时是密码）
        返回 (退出码, 打印内容, 模块)
        """
        with feed_input(input_name, output_name, page_text, *tail) as prompts:
            module = load_module()
            code, output = run_main(module)
        self.prompts = prompts
        return code, output, module

    # ---------- 核心功能 ----------

    def test_提取不连续页码_页数和顺序都正确(self):
        """1 基的第 2、4 页 -> 宽度 101、103。这是整个改造的验收点。"""
        code, output, _ = self.run_script(page_text="2,4")

        self.assertIsNone(code, f"main() 不该退出，实际退出码={code}\n{output}")
        self.assertTrue(Path("out.pdf").exists(), "输出文件没生成")
        self.assertEqual(page_widths("out.pdf"), [101, 103])
        self.assertIn("完成！", output)

    def test_混合格式提取(self):
        """3,1-2  ->  第 1、2、3 页，宽度 100、101、102。"""
        code, output, _ = self.run_script(page_text="3,1-2")
        self.assertIsNone(code, output)
        self.assertEqual(page_widths("out.pdf"), [100, 101, 102])

    def test_倒序输入仍按页序输出(self):
        """用户写 5,1，输出应是第 1 页在前（页码升序）。"""
        code, output, _ = self.run_script(page_text="5,1")
        self.assertIsNone(code, output)
        self.assertEqual(page_widths("out.pdf"), [100, 104])

    def test_确认直接回车视为确认(self):
        """脚本把 "" 当作 y，这个宽容行为要锁住，免得以后被改坏。"""
        code, output, _ = self.run_script(page_text="1", tail=("",))
        self.assertIsNone(code, output)
        self.assertEqual(page_widths("out.pdf"), [100])

    def test_确认信息里报告了页数(self):
        _, output, _ = self.run_script(page_text="2,4")
        self.assertIn("共 2 页", output)

    def test_确认信息打印的是从1开始的页码(self):
        """用户必须看到 [2, 4] 才能核对输入对不对，而不是 0 基的 [1, 3]。

        ★ 这个测试目前会失败，因为它抓到了 pdf_extract.py 第 80 行：
              print(f"将提取第 {indexes + 1} 页 ...")
          list + int 会抛 TypeError，所以这一行根本打印不出来。
        """
        _, output, _ = self.run_script(page_text="2,4")
        self.assertIn("[2, 4]", output, f"实际输出：\n{output}")

    # ---------- 用户放弃 ----------

    def test_回答n则取消且不写文件(self):
        code, output, _ = self.run_script(page_text="1-2", tail=("n",))
        self.assertEqual(code, 0, "取消应是一种成功的退出，退出码 0")
        self.assertIn("已取消", output)
        self.assertFalse(Path("out.pdf").exists(), "取消后不该产生文件")

    def test_回答其他内容也取消(self):
        code, output, _ = self.run_script(page_text="1", tail=("随便什么",))
        self.assertEqual(code, 0)
        self.assertFalse(Path("out.pdf").exists())

    # ---------- 各种失败路径 ----------

    def test_源文件不存在(self):
        code, output, _ = self.run_script(input_name="不存在的文件.pdf")
        self.assertEqual(code, 1)
        self.assertIn("找不到文件", output)
        # 打印当前目录是原脚本的一个好设计：能立刻定位"文件放错文件夹"
        self.assertIn(os.getcwd(), output)

    def test_页码超出总页数则退出(self):
        code, output, _ = self.run_script(page_text="1-9")  # 只有 5 页
        self.assertEqual(code, 1)
        self.assertIn("页码输入错误", output)
        # 出错时要顺便告诉用户正确写法
        self.assertIn("支持格式", output)
        self.assertFalse(Path("out.pdf").exists())

    def test_页码为空则退出(self):
        code, output, _ = self.run_script(page_text="")
        self.assertEqual(code, 1)
        self.assertIn("没有选择任何页码", output)
        self.assertFalse(Path("out.pdf").exists())

    def test_文件不是有效PDF则退出(self):
        Path("假的.pdf").write_bytes(b"this is not a pdf at all")
        code, output, _ = self.run_script(input_name="假的.pdf")
        self.assertEqual(code, 1)
        self.assertIn("无法读取", output)

    # ---------- 输出文件相关 ----------

    def test_输出文件已存在时提示将被覆盖(self):
        Path("out.pdf").write_bytes(b"old content")
        code, output, _ = self.run_script(page_text="1")
        self.assertIsNone(code, output)
        self.assertIn("已存在，将被覆盖", output)
        self.assertEqual(page_widths("out.pdf"), [100], "旧文件应被新内容替换")

    def test_输出文件被其他程序占用(self):
        """模拟 Windows 上 PDF 阅读器开着目标文件的情况。

        真去锁文件在不同平台行为不一致，所以直接把 open() 换成抛
        PermissionError —— 测的是"我们有没有正确处理这个异常"，
        而不是"操作系统会不会报这个异常"。
        """
        Path("out.pdf").write_bytes(b"x")
        with mock.patch("builtins.open", side_effect=PermissionError("locked")):
            code, output, _ = self.run_script(page_text="1")

        self.assertEqual(code, 1)
        self.assertIn("正被其他程序占用", output)

    # ---------- 加密 PDF ----------

    def test_加密PDF能被常见密码自动解开(self):
        """密码用 123456（在脚本的常见密码表里），全程不需要用户输入密码。"""
        make_encrypted_pdf("locked.pdf", "123456", pages=3)
        code, output, _ = self.run_script(
            input_name="locked.pdf", page_text="1-2", tail=("y",),
        )
        self.assertIsNone(code, output)
        self.assertIn("自动解密成功", output)
        self.assertEqual(page_widths("out.pdf"), [100, 101])

    def test_加密PDF密码三次都错则退出(self):
        make_encrypted_pdf("locked.pdf", "这个密码不在常见表里", pages=3)
        # 密码输入发生在"确认提取"之前，所以 tail 里只有 3 个密码
        code, output, _ = self.run_script(
            input_name="locked.pdf", page_text="1", tail=("错1", "错2", "错3"),
        )
        self.assertEqual(code, 1)
        self.assertIn("密码错误次数过多", output)


# ============================================================================
# 四、decrypt_pdf：用假 reader 单独测
# ============================================================================
# 为什么不用真加密文件？因为这里要测的是"重试逻辑和提示文案"，
# 用假的 reader 可以精确控制第几次成功，比造多个加密 PDF 更快更清楚。
# ============================================================================

class FakeReader:
    """只实现 decrypt() 的假 reader，模拟"某次输入密码才成功"。"""

    def __init__(self, correct_password):
        self.correct_password = correct_password
        self.attempts = []          # 记录被尝试过的所有密码，方便断言

    def decrypt(self, password):
        self.attempts.append(password)
        return 1 if password == self.correct_password else 0


class TestDecryptPdf(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.module = load_parse_module()

    def test_常见密码表里的密码直接命中(self):
        reader = FakeReader("admin")   # admin 在脚本的常见密码表里
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.module.decrypt_pdf(reader)

        self.assertIn("自动解密成功", buffer.getvalue())
        self.assertIn("admin", buffer.getvalue())

    def test_常见密码都不中时转人工输入(self):
        reader = FakeReader("我的密码")
        buffer = io.StringIO()
        with feed_input("先错一次", "我的密码"):
            with contextlib.redirect_stdout(buffer):
                self.module.decrypt_pdf(reader)

        output = buffer.getvalue()
        self.assertIn("解密成功", output)
        self.assertIn("我的密码", output)
        # 输错的那一次应该被提示"还剩几次机会"
        self.assertIn("还剩", output)

    def test_三次人工输入都失败则退出(self):
        reader = FakeReader("永远不会被猜到的密码")
        buffer = io.StringIO()
        with feed_input("错1", "错2", "错3"):
            with contextlib.redirect_stdout(buffer):
                with self.assertRaises(SystemExit) as ctx:
                    self.module.decrypt_pdf(reader)

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("密码错误次数过多", buffer.getvalue())

    def test_自动尝试阶段也会试空密码(self):
        """脚本的常见密码表第一个是 ""，空密码 PDF 不该要求用户输入。"""
        reader = FakeReader("")        # 只有空密码能解
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.module.decrypt_pdf(reader)

        self.assertIn("空密码", buffer.getvalue())


# ============================================================================
# 五、命令行端到端测试
# ============================================================================
# 前面的测试都是 import 模块后调 main()，绕过了 __main__ 那层 try/except。
# 这里用子进程当真正的命令行程序跑一遍，确认"用户实际使用的方式"也没问题。
# ============================================================================

class TestCommandLine(unittest.TestCase):

    def setUp(self):
        self._original_cwd = os.getcwd()
        self.workdir = tempfile.mkdtemp(prefix="pdf_extract_cli_")
        os.chdir(self.workdir)
        make_pdf("src.pdf", pages=5)

    def tearDown(self):
        os.chdir(self._original_cwd)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def run_cli(self, stdin_text):
        env = dict(os.environ)
        # 强制子进程用 UTF-8 读写，避免 Windows 默认 GBK 让中文输出乱码
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self.workdir,
            env=env,
            timeout=120,
        )

    def test_完整交互流程(self):
        """依次回答：源文件名 / 输出文件名 / 页码 / 确认。"""
        result = self.run_cli("src.pdf\nout.pdf\n2,4\ny\n")

        self.assertEqual(result.returncode, 0, f"stderr:\n{result.stderr}")
        self.assertIn("完成！", result.stdout)
        self.assertEqual(page_widths("out.pdf"), [101, 103])

    def test_取消时退出码为0(self):
        result = self.run_cli("src.pdf\nout.pdf\n1-3\nn\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("已取消", result.stdout)
        self.assertFalse(Path("out.pdf").exists())

    def test_出错时退出码为1(self):
        """外部工具靠退出码判断成败，这个约定不能破。"""
        result = self.run_cli("不存在.pdf\nout.pdf\n1\ny\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("找不到文件", result.stdout)

    def test_未预料的异常会被兜底捕获(self):
        """__main__ 里的 except Exception 应该把意外错误打成一行提示，
        而不是甩一大段 traceback 给用户看。

        注意：这个兜底目前会吞掉"确认信息打印"那行的 TypeError，
        所以上面 test_完整交互流程 才会失败——两者是同一个 bug 的两面。
        """
        result = self.run_cli("src.pdf\nout.pdf\n2,4\ny\n")
        self.assertNotIn("Traceback (most recent call last)", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
