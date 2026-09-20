@echo off
chcp 936 >nul
title iPod 音乐管理器
cd /d "%~dp0"

echo.
echo   iPod 音乐管理器
echo   ==========================================================
echo.

rem ── (1) 检查 uv：后端靠它跑 ─────────────────────────────
where uv >nul 2>&1
if errorlevel 1 goto NO_UV
echo   [√] uv 已就绪

rem ── (2) 检查 node：只有"从网易云下载"需要它 ─────────────
where node >nul 2>&1
if errorlevel 1 goto NO_NODE
echo   [√] node 已就绪

rem ── (3) 后端源码在不在（exe 靠它往上找 pyproject.toml）──
if not exist "pyproject.toml" goto NO_SRC
if not exist "src\ipod_web\app.py" goto NO_SRC
echo   [√] 后端源码就位

echo.
echo   正在启动……首次启动要装 Python 依赖（约 1 分钟，只需一次）
echo.
start "" "ipod_manager.exe"
exit /b 0


:NO_UV
echo   [×] 没找到 uv —— 后端需要它来运行。
echo.
echo       安装方法（任选一条，在 PowerShell 里执行）：
echo.
echo         winget install astral-sh.uv
echo.
echo       装完请**重新打开**这个窗口，再双击一次。
echo.
pause
exit /b 1


:NO_NODE
echo   [!] 没找到 node。
echo.
echo       不影响"把本地音乐文件导进 iPod"，
echo       但"从网易云下载歌"需要它跑 api-enhanced 这个本地 API。
echo.
echo       要装的话：winget install OpenJS.NodeJS.LTS
echo.
pause
exit /b 0


:NO_SRC
echo   [×] 找不到后端源码（pyproject.toml / src\ipod_web\app.py）。
echo.
echo       这个程序把"界面"和"后端"分开了：exe 只画界面，
echo       真正干活的是同目录下的 Python 源码。
echo       请不要只把 exe 单独拷出去，保持整个文件夹完整。
echo.
pause
exit /b 1
