@echo off
chcp 65001 >nul
title AI平台 - 前端服务

echo ============================================
echo   AI 通用能力大平台 - 前端服务启动
echo ============================================
echo.

cd /d "%~dp0frontend"

:: 检查 node_modules
if not exist "node_modules" (
    echo [INFO] 首次运行，安装依赖...
    npm install
    echo.
)

echo [INFO] 工作目录: %CD%
echo [INFO] 启动 Vite 开发服务器...
echo [INFO] 访问地址: http://localhost:5173
echo.

npm run dev

echo.
echo [INFO] 服务已停止
pause
