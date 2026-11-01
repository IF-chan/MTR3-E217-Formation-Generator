@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   请选择语言 / Select language
echo ========================================
echo   1. 中文
echo   2. English
echo.
choice /c 12 /n /m "请选择 / Select [1/2]: "
if errorlevel 3 exit /b 1
if errorlevel 2 goto english
if errorlevel 1 goto chinese
exit /b 1

:chinese
set "script=build_pack_cn.py"
goto find_python

:english
set "script=build_pack_en.py"

:find_python
if not exist "%script%" (
  echo 错误 / Error: "%script%" not found.
  set "exit_code=1"
  goto finish
)

py -3 -B -X utf8 -c "import sys" >nul 2>nul
if not errorlevel 1 goto run_py

for /f "delims=" %%P in ('where python 2^>nul') do (
  "%%P" -B -X utf8 -c "import sys" >nul 2>nul
  if not errorlevel 1 (
    set "python_exe=%%P"
    goto run_python
  )
)

echo 未找到可用的 Python 3，请安装 Python 后重试。
echo No working Python 3 found. Please install Python and try again.
set "exit_code=1"
goto finish

:run_py
py -3 -B -X utf8 "%script%" %*
set "exit_code=%errorlevel%"
goto finish

:run_python
"%python_exe%" -B -X utf8 "%script%" %*
set "exit_code=%errorlevel%"

:finish
echo.
pause
endlocal & exit /b %exit_code%
