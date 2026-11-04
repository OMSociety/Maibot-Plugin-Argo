"""Argo 常驻 NDJSON 守护进程管理器（MaiBot 侧）。

长驻 ``mcp_server.py`` 子进程并维持单条 stdio NDJSON 管道：
- 启动时发送 ``initialize`` 触发 Argo 后台预热；
- 每次工具调用走毫秒级内存通信，自动崩溃自愈；
- 注入 ``ARGO_IMAGE_LOCAL_ROOTS`` 限定反搜本地图仅允许读取受控暂存目录；
- 物理清洗 ``NO_PROXY`` 中的 ``[::1]``，规避 httpx IPv6 端口解析异常。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def sanitize_no_proxy(val: str) -> str:
    """移除 NO_PROXY 中会导致 httpx URL 解析报错的 [::1] 项。"""
    if not val:
        return ""
    parts = [p.strip() for p in val.split(",") if p.strip() and p.strip() != "[::1]"]
    return ",".join(parts)


class ArgoWorker:
    """管理单个常驻 mcp_server.py 子进程的异步 JSON-RPC 客户端。"""

    def __init__(
        self,
        *,
        python_path: str,
        argo_dir: str | Path,
        argo_env_file: str | Path | None,
        temp_image_root: str | Path,
        default_timeout: float = 25.0,
    ) -> None:
        self.python_path = str(python_path or sys.executable)
        self.argo_dir = str(argo_dir)
        self.argo_env_file = str(argo_env_file) if argo_env_file else ""
        self.temp_image_root = str(Path(temp_image_root).resolve())
        self.default_timeout = float(default_timeout)

        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._req_id = 0
        self._stderr_task: asyncio.Task[None] | None = None

    def update_settings(
        self,
        *,
        python_path: str | None = None,
        argo_dir: str | Path | None = None,
        argo_env_file: str | Path | None = None,
        default_timeout: float | None = None,
    ) -> None:
        """热更新路径与超时参数。"""
        if python_path is not None:
            self.python_path = str(python_path or sys.executable)
        if argo_dir is not None:
            self.argo_dir = str(argo_dir)
        if argo_env_file is not None:
            self.argo_env_file = str(argo_env_file) if argo_env_file else ""
        if default_timeout is not None:
            self.default_timeout = float(default_timeout)

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if self.argo_env_file:
            env["ARGO_ENV_FILE"] = self.argo_env_file
        Path(self.temp_image_root).mkdir(parents=True, exist_ok=True)
        env["ARGO_IMAGE_LOCAL_ROOTS"] = self.temp_image_root
        for key in ("NO_PROXY", "no_proxy"):
            if key in env:
                env[key] = sanitize_no_proxy(env[key])
        return env

    async def start(self) -> None:
        """启动或确认子进程存活并完成握手预热。"""
        async with self._lock:
            await self._ensure_proc_locked()

    async def close(self) -> None:
        """优雅关闭常驻子进程。"""
        async with self._lock:
            await self._kill_proc_locked()

    async def _ensure_proc_locked(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return

        await self._kill_proc_locked()

        script_path = os.path.join(self.argo_dir, "scripts", "mcp_server.py")
        if not os.path.isfile(script_path):
            raise FileNotFoundError(f"找不到 Argo MCP 入口脚本: {script_path}")

        self._proc = await asyncio.create_subprocess_exec(
            self.python_path,
            "-X",
            "utf8",
            script_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.argo_dir,
            env=self._build_env(),
            limit=16 * 1024 * 1024,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._proc))

        self._req_id += 1
        init_req = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "maibot-plugin-argo", "version": "1.0.0"},
            },
        }
        await self._send_and_recv_locked(init_req, timeout=15.0)

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    logger.debug("[ArgoWorker stderr] %s", text)
        except Exception:
            pass

    async def _kill_proc_locked(self) -> None:
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except Exception:
                pass

    async def _send_and_recv_locked(
        self, payload: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("Argo 子进程未就绪")

        expected_id = payload.get("id")
        raw_line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._proc.stdin.write(raw_line.encode("utf-8"))
        await self._proc.stdin.drain()

        async def _read_until_match() -> dict[str, Any]:
            assert self._proc is not None and self._proc.stdout is not None
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    raise EOFError("Argo 子进程提前关闭了 stdout")
                text = line.decode("utf-8", errors="replace").strip()
                if not text or not text.startswith("{"):
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == expected_id:
                    return msg

        return await asyncio.wait_for(_read_until_match(), timeout=timeout)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """调用底层 Argo 工具并返回解析后的字典。"""
        effective_timeout = float(timeout or self.default_timeout)
        async with self._lock:
            for attempt in range(2):
                try:
                    await self._ensure_proc_locked()
                    self._req_id += 1
                    req = {
                        "jsonrpc": "2.0",
                        "id": self._req_id,
                        "method": "tools/call",
                        "params": {
                            "name": tool_name,
                            "arguments": arguments,
                        },
                    }
                    resp = await self._send_and_recv_locked(req, timeout=effective_timeout)
                    break
                except (EOFError, BrokenPipeError, ConnectionResetError, OSError) as exc:
                    logger.warning(
                        "[ArgoWorker] 通信中断 (%s)，正在重启子进程 (attempt=%d)",
                        exc,
                        attempt + 1,
                    )
                    await self._kill_proc_locked()
                    if attempt == 1:
                        return {"error": f"Argo 子进程通信异常: {exc}"}
                except asyncio.TimeoutError:
                    logger.warning(
                        "[ArgoWorker] 工具 %s 超时 (>%.1fs)，重置子进程",
                        tool_name,
                        effective_timeout,
                    )
                    await self._kill_proc_locked()
                    return {"error": f"工具 {tool_name} 执行超时 (>{effective_timeout:.0f}s)"}
            else:
                return {"error": "Argo 子进程调用失败"}

        if "error" in resp:
            err_obj = resp["error"]
            msg = (
                err_obj.get("message", str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
            )
            return {"error": msg}

        result = resp.get("result") or {}
        content_list = result.get("content") or []
        text_parts = [
            c.get("text", "")
            for c in content_list
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        joined = "\n".join(p for p in text_parts if p).strip()
        if not joined:
            return {"error": "Argo 未返回有效内容"}
        try:
            parsed = json.loads(joined)
            if isinstance(parsed, dict):
                return parsed
            return {"data": parsed}
        except json.JSONDecodeError:
            return {"raw_text": joined}
