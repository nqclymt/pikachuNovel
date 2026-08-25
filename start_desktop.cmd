@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "HN_PYTHON="
set "HN_PYTHON_EXE="
set "HN_PYTHONW="

rem Prefer the Python launcher, but verify that it is executable.
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "HN_PYTHON=py -3"
)

rem The Microsoft Store python.exe alias can exist even when Python is absent.
rem Check common per-user and system installation locations before using PATH.
if not defined HN_PYTHON if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    set "HN_PYTHON_EXE=%LocalAppData%\Programs\Python\Python312\python.exe"
    set HN_PYTHON="%LocalAppData%\Programs\Python\Python312\python.exe"
    set "HN_PYTHONW=%LocalAppData%\Programs\Python\Python312\pythonw.exe"
)
if not defined HN_PYTHON if exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
    set "HN_PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\python.exe"
    set HN_PYTHON="%LocalAppData%\Programs\Python\Python311\python.exe"
    set "HN_PYTHONW=%LocalAppData%\Programs\Python\Python311\pythonw.exe"
)
if not defined HN_PYTHON if exist "%ProgramFiles%\Python312\python.exe" (
    set "HN_PYTHON_EXE=%ProgramFiles%\Python312\python.exe"
    set HN_PYTHON="%ProgramFiles%\Python312\python.exe"
    set "HN_PYTHONW=%ProgramFiles%\Python312\pythonw.exe"
)
if not defined HN_PYTHON if exist "%ProgramFiles%\Python311\python.exe" (
    set "HN_PYTHON_EXE=%ProgramFiles%\Python311\python.exe"
    set HN_PYTHON="%ProgramFiles%\Python311\python.exe"
    set "HN_PYTHONW=%ProgramFiles%\Python311\pythonw.exe"
)

if not defined HN_PYTHON (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys" >nul 2>nul
        if not errorlevel 1 set "HN_PYTHON=python"
    )
)

if not defined HN_PYTHON (
    echo [PikachuNovel] A working Python 3.9+ interpreter was not found.
    echo [PikachuNovel] Install Python from https://www.python.org/downloads/windows/ and run this file again.
    pause
    exit /b 1
)

%HN_PYTHON% -c "import webview, uvicorn, fastapi, openai; from openai.resources.responses.responses import Responses" >nul 2>nul
if errorlevel 1 (
    echo [PikachuNovel] Installing desktop dependencies for this source checkout...
    %HN_PYTHON% -m pip install --upgrade ".[desktop]"
    if errorlevel 1 (
        echo [PikachuNovel] Installation failed. Check the network and the error above.
        pause
        exit /b 1
    )
)

if not defined HN_PYTHONW for /f "delims=" %%I in ('%HN_PYTHON% -c "import pathlib, sys; p=pathlib.Path(sys.executable); w=p.with_name('pythonw.exe'); print(w if w.exists() else p)"') do set "HN_PYTHONW=%%I"
start "PikachuNovel" "%HN_PYTHONW%" "%~dp0start_desktop.pyw"
