@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set "HN_PYTHON=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [HarnessNovel] Python 3 was not found. Install Python 3.9 or newer first.
        pause
        exit /b 1
    )
    set "HN_PYTHON=python"
)

%HN_PYTHON% -c "import webview, uvicorn, fastapi, openai" >nul 2>nul
if errorlevel 1 (
    echo [HarnessNovel] Installing desktop dependencies for this source checkout...
    %HN_PYTHON% -m pip install --upgrade ".[desktop]"
    if errorlevel 1 (
        echo [HarnessNovel] Installation failed. Check the network and the error above.
        pause
        exit /b 1
    )
)

for /f "delims=" %%I in ('%HN_PYTHON% -c "import pathlib, sys; p=pathlib.Path(sys.executable); w=p.with_name('pythonw.exe'); print(w if w.exists() else p)"') do set "HN_PYTHONW=%%I"
start "HarnessNovel" "%HN_PYTHONW%" "%~dp0start_desktop.pyw"
