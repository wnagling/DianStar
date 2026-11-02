"""Bounded foreground execution shared by argv commands and Bash scripts."""
import asyncio
import os
import signal
import subprocess
import time
from dianstar.platform import command_reads  # bind the host-lifetime state root before per-task overrides
from dianstar.security.system_paths import HostSafetyError

MAX_OUTPUT = 2 * 1024 * 1024


async def run_process(argv, root, environment, timeout, *, protected=(), network=False, managed=False, scanner=None):
    from dianstar.platform.command_isolation import write_scope, IsolationError
    from .files.io import blocking
    scope, prepared = None, {}
    started = time.monotonic()
    attempted = False
    try:
        def prepare(cancel):
            value = write_scope(root, protected=protected, executable=argv[0], environment=environment, managed=managed, scanner=scanner)
            prepared["scope"] = value
            return value
        scope = await blocking(prepare)
        worker, launch_input = scope.launch(argv, environment, network=network)
        attempted = True
        result = await _run_process(worker, root, environment, timeout, launch_input=launch_input)
        marker = ("DIANSTAR_ISOLATION_ERROR:" + scope.launch_nonce + ":").encode()
        if result["exit_code"] == 125 and result["output"].startswith(marker):
            phase, _, detail = result["output"][len(marker):].partition(b":")
            if phase == b"before":
                result.update(error="command_isolation_unavailable", success=False,
                    message="Windows 命令隔离建立失败，未运行目标程序；不会降级为无限制运行。")
            else:
                result.update(error="command_execution_incomplete", success=False,
                    message="命令执行状态未完整确认，可能已发生修改；请核对当前状态，不要盲目重试。")
            result["output"] = b"Isolated launcher failed: " + detail
        result["isolation"] = "windows_appcontainer"
        result["isolation_profile"] = "lpac"
        result["network_allowed"] = network
        result["command_duration_ms"] = result["duration_ms"]
        return result
    except HostSafetyError:
        raise
    except (ValueError, OSError) as error:
        if attempted:
            result = {"error": "command_execution_incomplete", "message": "命令启动或收尾未完整确认，可能已发生修改；请核对当前状态，不要盲目重试。",
                "success": False, "exit_code": None, "timed_out": False, "output_limit_exceeded": False, "output": b"",
                "duration_ms": round((time.monotonic()-started)*1000)}
            return result
        raise IsolationError() from error
    finally:
        scope = scope or prepared.get("scope")
        if scope is not None:
            await blocking(lambda cancel: scope.close())
            if scope.cleanup_pending and "result" in locals():
                result["isolation_cleanup_pending"] = True
        if "result" in locals():
            result["duration_ms"] = round((time.monotonic() - started) * 1000)


async def _run_process(argv, root, environment, timeout, *, launch_input=None):
    job, process = None, None
    started = time.monotonic()
    if os.name == "nt":
        from dianstar.platform.process_job import WindowsJob
        job = WindowsJob()
    async def stop():
        if job:
            job.stop()
        elif process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process is not None and process.returncode is None:
            process.kill()

    output, exceeded, timed_out = bytearray(), False, False
    async def drain():
        while await process.stdout.read(8192):
            pass
    async def read():
        nonlocal exceeded
        while data := await process.stdout.read(8192):
            remaining = MAX_OUTPUT - len(output)
            output.extend(data[:max(0, remaining)])
            if len(data) > remaining:
                exceeded = True
                await stop()
                await asyncio.wait_for(drain(), 5)
                return
    async def lifetime():
        # Process.wait() can wait for inherited pipes after the parent exits.
        # Close the owned tree promptly when its foreground process exits.
        while process.returncode is None:
            await asyncio.sleep(.02)
        await stop()
    try:
        process = await asyncio.create_subprocess_exec(*argv, cwd=root, env=environment, stdin=asyncio.subprocess.PIPE if launch_input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            **({"creationflags": subprocess.CREATE_NO_WINDOW | 0x4} if os.name == "nt" else {"start_new_session": True}))
        if job:
            job.attach_and_resume(process.pid)
        try:
            async with asyncio.timeout(timeout):
                if launch_input is not None:
                    process.stdin.write(launch_input)
                    await process.stdin.drain()
                    process.stdin.close()
                    await process.stdin.wait_closed()
                async with asyncio.TaskGroup() as group:
                    group.create_task(read())
                    group.create_task(lifetime())
                await process.wait()
        except TimeoutError:
            timed_out = True
            await stop()
            await asyncio.wait_for(drain(), 5)
            await asyncio.wait_for(process.wait(), 5)
        except BaseException:
            await stop()
            await asyncio.wait_for(drain(), 5)
            await asyncio.wait_for(process.wait(), 5)
            raise
        return {"exit_code": process.returncode, "timed_out": timed_out,
                "output_limit_exceeded": exceeded, "output": bytes(output),
                "success": process.returncode == 0 and not timed_out and not exceeded,
                "duration_ms": round((time.monotonic() - started) * 1000)}
    finally:
        if process is not None:
            try:
                await stop()
                await asyncio.wait_for(drain(), 5)
                await asyncio.wait_for(process.wait(), 5)
            finally:
                if job:
                    job.close()
        elif job:
            job.close()
