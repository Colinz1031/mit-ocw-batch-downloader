@echo off
setlocal
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0mit_ocw_batch_downloader.py"
) else (
    python "%~dp0mit_ocw_batch_downloader.py"
)
if errorlevel 1 pause
endlocal
