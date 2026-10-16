@echo off
chcp 65001 >nul
rem 日志级别：DEBUG / INFO，默认 INFO。想看 SQL 与请求明细就把下面这行改成 DEBUG
if "%STOCKMINING_LOG_LEVEL%"=="" set STOCKMINING_LOG_LEVEL=INFO
set PY=C:\Users\ming\.workbuddy\binaries\python\envs\stockmining\Scripts\python.exe
if not exist "%PY%" (
  echo 未找到虚拟环境，请先执行:
  echo   "C:\Users\ming\.workbuddy\binaries\python\versions\3.13.12\python.exe" -m venv "C:\Users\ming\.workbuddy\binaries\python\envs\stockmining"
  echo   "C:\Users\ming\.workbuddy\binaries\python\envs\stockmining\Scripts\python.exe" -m pip install -r requirements.txt
  pause
  exit /b 1
)
"%PY%" "%~dp0run.py"
pause
