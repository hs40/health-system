@echo off
REM ============================================================
REM 健康运动管理系统 · 本地开发一键启动（可双击）
REM 打开一个 Git Bash 窗口运行 scripts/dev.sh（后端 :8000 + 前端 :5173）
REM ============================================================
setlocal
set "REPO=%~dp0"
set "REPO=%REPO:\=/%"
set "BASH=%ProgramFiles%\Git\bin\bash.exe"
if not exist "%BASH%" set "BASH=%ProgramFiles(x86)%\Git\bin\bash.exe"
if not exist "%BASH%" (
  echo [错误] 未找到 Git Bash，请先安装 Git for Windows: https://git-scm.com/download/win
  pause
  exit /b 1
)
echo 正在启动本地服务（后端 http://127.0.0.1:8000  前端 http://localhost:5173）...
start "HealthSystem Dev" "%BASH%" -lc "cd '%REPO%' && ./scripts/dev.sh"
endlocal
