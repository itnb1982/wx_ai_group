# -*- coding: utf-8 -*-
r"""
WanxiangAI Backend Windows Service Wrapper
==========================

根治方案：把后端（supervisor 看门狗 + uvicorn + MT5 worker）注册为 Windows 服务。
- 完全无窗口（运行在 Session 0，无任何控制台/黑框）
- 开机自启，无需用户登录
- 用户注销 / 关闭任何窗口都不影响
- 服务管理器负责：supervisor 进程崩溃时自动重启（失败重启策略由 sc 配置）
- supervisor 仍负责 uvicorn 进程级看门狗，双层防护

注册（管理员 PowerShell）：
    F:\WanxiangAI\.venv\Scripts\python.exe F:\WanxiangAI\backend\windows_service.py install
    sc failure "WanxiangAIBackend" reset= 0 actions= restart/3000/restart/5000/restart/10000
    net start WanxiangAIBackend

卸载：
    net stop WanxiangAIBackend
    F:\WanxiangAI\.venv\Scripts\python.exe F:\WanxiangAI\backend\windows_service.py remove
"""
import os
import sys
import time
import subprocess

import win32serviceutil
import win32service
import win32event
import servicemanager

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.normpath(os.path.join(HERE, "..", ".venv", "Scripts", "python.exe"))
SUPERVISOR = os.path.join(HERE, "launch_supervisor.py")


class WanxiangAIBackendService(win32serviceutil.ServiceFramework):
    _svc_name_ = "WanxiangAIBackend"
    _svc_display_name_ = "WanxiangAI Trading Backend"
    _svc_description_ = "WanxiangAI XAUUSD AI trading system backend (Supervisor + uvicorn + MT5 workers)"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._child = None

    def _spawn_supervisor(self):
        """启动 supervisor 子进程（无窗口、脱离控制台）"""
        env = os.environ.copy()
        env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
        self._child = subprocess.Popen(
            [VENV_PY, SUPERVISOR],
            cwd=HERE,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
        servicemanager.LogInfoMsg(f"[WanxiangAI] supervisor started PID={self._child.pid}")

    def _kill_child_tree(self):
        if self._child is None:
            return
        pid = self._child.pid
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except Exception as e:
            servicemanager.LogErrorMsg(f"[WanxiangAI] failed to stop child tree: {e}")
        try:
            self._child.wait(timeout=5)
        except Exception:
            pass
        self._child = None

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self._spawn_supervisor()
        # 关键：启动 supervisor 后立刻报告 SCM 服务已运行，否则 30s 内会被判定无响应
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)

        # 主循环：等待停止信号；同时监测 supervisor 是否意外退出，退出则拉起
        while True:
            rc = win32event.WaitForSingleObject(self._stop_event, 5000)
            if rc == win32event.WAIT_OBJECT_0:
                # 收到停止信号
                break
            # 5s 心跳检测：supervisor 进程是否还活着
            if self._child is not None and self._child.poll() is not None:
                servicemanager.LogErrorMsg(
                    f"[WanxiangAI] supervisor exited code={self._child.returncode}, restarting in 5s"
                )
                time.sleep(5)
                self._spawn_supervisor()

        self._kill_child_tree()
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STOPPED,
            (self._svc_name_, ""),
        )

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self._stop_event)


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(WanxiangAIBackendService)
