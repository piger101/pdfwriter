from pypdf import PdfReader, PdfWriter
import os
import sys

INPUT_FILE = input("请输入要处理的 PDF 文件名（含后缀）：").strip()
OUTPUT_FILE = input("请输入输出 PDF 文件名（含后缀）：").strip()
START = int(input("请输入起始索引（含，0 开始计数）：").strip())
END = int(input("请输入结束索引（不含，0 开始计数）：").strip())

def main():
    # 检查输入文件是否存在
    if not os.path.exists(INPUT_FILE):
        print(f"找不到文件：{os.path.abspath(INPUT_FILE)}")
        print(f"当前工作目录是：{os.getcwd()}")
        print("请确认 PDF 和脚本在同一文件夹")
        sys.exit(1)  # 退出码 1 表示“出错结束”，方便外部工具判断

    # 检查输出文件是否已被占用（比如正被 PDF 阅读器开着，写入会失败）
    if os.path.exists(OUTPUT_FILE):
        try:
            # 尝试以追加模式打开，如果文件被别的程序锁定，这一步会抛异常
            with open(OUTPUT_FILE, "a"):
                pass
        except PermissionError:
            print(f"输出文件 {OUTPUT_FILE} 正被其他程序占用（可能开着 PDF 阅读器），请先关闭")
            sys.exit(1)
        # 文件已存在时提醒
        print(f"输出文件 {OUTPUT_FILE} 已存在，将被覆盖")

    try:
        reader = PdfReader(INPUT_FILE)
    except Exception as e:
        # 文件存在但可能损坏、加密、根本不是 PDF
        print(f"无法读取 {INPUT_FILE}：{e}")
        sys.exit(1)

    # 加密 PDF 检查（有些教材 PDF 带密码保护）
    if reader.is_encrypted:
        print(f"PDF 文件 {INPUT_FILE} 是加密的")
        decrypt_pdf(reader)

    # 页码范围合法性检查
    total = len(reader.pages)
    if START < 0 or END > total:
        print(f"页码范围越界！")
        print(f"你请求的索引范围：{START} ~ {END - 1}")
        print(f"但《{INPUT_FILE}》总页数只有：{total}")
        print(f"合法的索引范围是：0 ~ {total - 1}")
        sys.exit(1)
    if START >= END:
        print(f"起始页 {START} 不能大于等于结束页 {END}")
        sys.exit(1)

    # 操作前先给用户确认信息
    print(f"源文件：{INPUT_FILE}（共 {total} 页）")
    print(f"将提取索引 {START} ~ {END - 1}，即第 {START + 1} ~ {END} 页，共 {END - START} 页")
    print(f"输出到：{os.path.abspath(OUTPUT_FILE)}")
    answer = input("确认执行？(y/n)：").strip().lower()
    if answer not in ("y", "yes", ""):
        print("已取消")
        sys.exit(0)

    # 主流程
    writer = PdfWriter()
    for i in range(START, END):
        # 单页失败不中断整个任务，跳过并记录
        try:
            writer.add_page(reader.pages[i])
        except Exception as e:
            print(f"第 {i + 1} 页（索引 {i}）添加失败，已跳过：{e}")

    if len(writer.pages) == 0:
        print("没有任何页面被成功提取，不生成空文件")
        sys.exit(1)

    writer.write(OUTPUT_FILE)

    # 写完后验证输出文件真的存在且非空
    size = os.path.getsize(OUTPUT_FILE)
    print(f"完成！已保存到：{os.path.abspath(OUTPUT_FILE)}（{size / 1024:.1f} KB，{len(writer.pages)} 页）")

def decrypt_pdf(reader):
    """尝试解密 PDF：先用常见密码列表自动试，失败后让用户输入"""
    # 第一轮：自动尝试常见密码（可以按需增删）
    common_passwords = ["", "123456", "888888", "666666", "admin","123"]
    for pwd in common_passwords:
        if reader.decrypt(pwd):
            print(f"自动解密成功（密码：{pwd if pwd else '空密码'}）")
            return

    # 第二轮：让用户手动输入，最多试 3 次
    for attempt in range(3):
        pwd = input("请输入 PDF 密码：").strip()
        if reader.decrypt(pwd):
            print("解密成功")
            return
        remaining = 3 - attempt   # 还剩几次机会
        common_passwords.append(pwd)  # 记录尝试过的密码
        if remaining == 1:
            print(f"密码错误，还剩 {remaining} 次机会")
            print("已尝试密码：")
            for p in common_passwords:
                print(f"  - {p}")
        else:
            print("密码错误次数过多，程序退出")
            sys.exit(1)


# 有未预料到的异常
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已手动中断")
    except Exception as e:
        print(f"\n发生未预料的错误：{type(e).__name__}: {e}")
