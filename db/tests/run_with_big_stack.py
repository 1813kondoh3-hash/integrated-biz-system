"""テストを、大きなスタック（作業領域）を持つスレッドで実行する。Windowsでのスタック不足の切り分け用。
使い方: python run_with_big_stack.py <テストのファイル> [引数...]
スタックの大きさは、環境変数 IBIS_STACK_MB で変えられる（既定 128MB。WindowsのPythonは256MB未満まで）"""
import os
import runpy
import sys
import threading

STACK_MB = int(os.environ.get("IBIS_STACK_MB", "128"))


def main():
    target = sys.argv[1]
    sys.argv = [target] + sys.argv[2:]
    sys.path.insert(0, os.path.dirname(os.path.abspath(target)))
    code = [0]

    def run():
        try:
            runpy.run_path(target, run_name="__main__")
        except SystemExit as e:
            code[0] = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except BaseException:
            import traceback
            traceback.print_exc()
            code[0] = 1

    threading.stack_size(STACK_MB * 1024 * 1024)
    t = threading.Thread(target=run, name="big-stack")
    t.start()
    t.join()
    print(f"（スタック {STACK_MB}MB のスレッドで実行しました）", file=sys.stderr)
    sys.exit(code[0])


if __name__ == "__main__":
    main()