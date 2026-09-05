' 万象Ai 后端开机自启（无窗口、与启动控制台解耦）
' 位置：F:\WanxiangAI\backend\WanxiangAI_Start.vbs
' 由启动文件夹的 .bat 调用，或直接放启动文件夹

Set WshShell = CreateObject("WScript.Shell")

' 检查 8080 是否已监听（避免双开抢端口）
portCheck = "powershell -NoProfile -Command ""if (Test-NetConnection -ComputerName 127.0.0.1 -Port 8080 -InformationLevel Quiet -WarningAction SilentlyContinue) { exit 0 } else { exit 1 }"""
exitCode = WshShell.Run(portCheck, 0, True)

If exitCode = 0 Then
    ' 后端已在运行，直接退出
    WScript.Quit 0
End If

' 启动后端守护进程（无窗口、独立进程）
WshShell.Run """F:\WanxiangAI\.venv\Scripts\python.exe"" ""F:\WanxiangAI\backend\launch_supervisor.py""", 0, False

Set WshShell = Nothing
