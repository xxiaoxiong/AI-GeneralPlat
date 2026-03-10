@echo off
chcp 65001 >nul
title AI平台 - 后端服务

echo ============================================
echo   AI 通用能力大平台 - 后端服务启动
echo ============================================
echo.

cd /d "%~dp0backend"

:: 检查 Python 是否可用
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未找到 Python！请确保 Python 已安装并添加到 PATH
    echo.
    pause
    exit /b 1
)

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
echo [INFO] Python: 
python --version
echo.
echo [INFO] 启动 FastAPI 服务 (http://0.0.0.0:8002) ...
echo [INFO] API 文档: http://127.0.0.1:8002/docs
echo.

:: 使用 call 捕获错误，防止闪退
python -m uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
if errorlevel 1 (
    echo.
    echo [ERROR] 服务启动失败！错误代码: %errorlevel%
    echo [HINT] 请检查：
    echo   1. 依赖是否已安装: pip install -r requirements.txt
    echo   2. .env 文件是否存在并配置正确
    echo   3. 端口 8002 是否被占用
    echo.
    pause
    exit /b 1
)

echo.
echo [INFO] 服务已停止
pause
