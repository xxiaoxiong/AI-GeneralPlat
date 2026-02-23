@echo off
chcp 65001 >nul
title AI平台 - 后端服务

echo ============================================
echo   AI 通用能力大平台 - 后端服务启动
echo ============================================
echo.

cd /d "%~dp0backend"

:: 检查虚拟环境
if exist ".venv\Scripts\activate.bat" (
    echo [INFO] 激活虚拟环境 .venv ...
    call .venv\Scripts\activate.bat
) else if exist "..\venv\Scripts\activate.bat" (
    echo [INFO] 激活虚拟环境 venv ...
    call ..\venv\Scripts\activate.bat
) else (
    echo [WARN] 未找到虚拟环境，使用系统 Python
)

echo.
echo [INFO] 工作目录: %CD%
echo [INFO] 启动 FastAPI 服务 (http://0.0.0.0:8002) ...
echo [INFO] API 文档: http://127.0.0.1:8002/docs
echo.

python -m uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload

echo.
echo [INFO] 服务已停止
pause
