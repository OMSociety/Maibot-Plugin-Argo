"""作业运行器：在脱离式进程里执行一条 argo 命令。

由 ``argo_cli.submit_job`` 以独立进程拉起（Windows 上带 DETACHED_PROCESS），
自身不持有任何对话连接，因此会话关闭、宿主重启都不影响它跑完。

作业目录协议：

- 输入：``argv.json``（要执行的完整 argv）
- 输出：``stdout.log`` / ``stderr.log``（子进程输出）
- 完成信号：``rc``（文件内容即退出码）
- 自身异常：``runner.log`` + ``rc=127``
"""

from __future__ import annotations

import json
import subprocess
import sys
import traceback
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: job_runner.py <job_dir>", file=sys.stderr)
        return 2

    job_dir = Path(sys.argv[1])
    job_dir.mkdir(parents=True, exist_ok=True)
    rc_path = job_dir / "rc"
    rc_path.unlink(missing_ok=True)

    rc = 127
    try:
        argv = json.loads((job_dir / "argv.json").read_text(encoding="utf-8"))
        with (job_dir / "stdout.log").open("wb") as out, (job_dir / "stderr.log").open("wb") as err:
            proc = subprocess.Popen(argv, stdout=out, stderr=err, stdin=subprocess.DEVNULL)
            rc = proc.wait()
    except Exception:  # noqa: BLE001 - 任何异常都要落盘并给出退出码
        rc = 127
        try:
            (job_dir / "runner.log").write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
    finally:
        try:
            rc_path.write_text(str(rc), encoding="utf-8")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
