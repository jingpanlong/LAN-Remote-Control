"""
在后台线程中运行一个 asyncio 事件循环。

GUI（PyQt）与异步网络代码之间的桥梁：
    - 所有网络协程都提交到同一个循环执行，避免反复 asyncio.run()
      导致 websocket 绑定到已关闭的循环；
    - submit() 线程安全，可从任意线程调用。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading

logger = logging.getLogger("LAN_REMOTE")


class AsyncLoopRunner:
    """独占线程的 asyncio 事件循环。"""

    def __init__(self, name: str = "AsyncLoop"):
        self._name = name
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._ready = threading.Event()
        self._closed = False

    # ------------------------------------------------------------------ 生命周期
    def start(self, timeout: float = 5.0) -> bool:
        if self._closed:
            raise RuntimeError("AsyncLoopRunner 已关闭，无法重新启动")
        if not self._thread.is_alive():
            self._thread.start()
        return self._ready.wait(timeout)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        except Exception as exc:
            logger.exception("事件循环异常退出：%s", exc)
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            finally:
                self._loop.close()
                logger.info("%s 已退出", self._name)

    def stop(self, timeout: float = 3.0) -> None:
        if self._closed:
            return
        self._closed = True

        async def _cancel_all() -> None:
            current = asyncio.current_task()
            tasks = [t for t in asyncio.all_tasks() if t is not current]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        if not self._loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(_cancel_all(), self._loop).result(timeout=timeout)
            except (concurrent.futures.TimeoutError, RuntimeError, Exception):
                pass
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass

        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=timeout)

    # ------------------------------------------------------------------ 调度
    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    @property
    def is_running(self) -> bool:
        return self._loop.is_running() and not self._closed

    def submit(self, coro) -> concurrent.futures.Future:
        """线程安全地调度协程，返回 concurrent.futures.Future。"""
        if self._closed or self._loop.is_closed():
            coro.close()
            raise RuntimeError("事件循环已关闭")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def fire_and_forget(self, coro, on_error=None) -> concurrent.futures.Future | None:
        """调度协程且不关心返回值，异常通过日志或回调上报。"""
        try:
            future = self.submit(coro)
        except RuntimeError:
            return None

        def _done(fut: concurrent.futures.Future) -> None:
            try:
                fut.result()
            except concurrent.futures.CancelledError:
                pass
            except Exception as exc:
                if on_error is not None:
                    try:
                        on_error(exc)
                        return
                    except Exception:
                        pass
                logger.error("后台任务执行失败：%s", exc)

        future.add_done_callback(_done)
        return future

    def call_soon_threadsafe(self, callback, *args) -> None:
        if not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(callback, *args)
            except RuntimeError:
                pass
