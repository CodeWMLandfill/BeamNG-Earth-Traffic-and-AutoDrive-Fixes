@echo off
setlocal EnableExtensions DisableDelayedExpansion
title BeamNG.Earth - Traffic and AutoDrive fixes (1-Click Uninstall)

set "SCRIPT_DIR=%~dp0"
set "SETUP_PY=%SCRIPT_DIR%tools\easy_setup.py"

if exist "%SETUP_PY%" goto :find_python

echo.
echo  ========================================================================
echo   BeamNG.Earth - Traffic and AutoDrive fixes (Uninstall)
echo  ========================================================================
echo.
echo   It looks like this file was opened directly inside a ZIP archive.
echo.
echo   What to do:
echo     1. Close this window.
echo     2. Right-click the downloaded .zip file and click "Extract All..."
echo     3. Open the extracted folder and double-click 1_Click_Uninstall.bat.
echo.
if not "%BNE_ADF_NO_PAUSE%"=="1" pause
exit /b 1

:find_python
set "PY_CMD="
set "PY_ARG="

py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,12) else 1)" >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=py"
    set "PY_ARG=-3.12"
    goto :run_with_python
)

py -3 -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=py"
    set "PY_ARG=-3"
    goto :run_with_python
)

python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=python"
    goto :run_with_python
)

python3 -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if not errorlevel 1 (
    set "PY_CMD=python3"
    goto :run_with_python
)

for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do (
    if exist "%%~fD\python.exe" (
        "%%~fD\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
        if not errorlevel 1 (
            set "PY_CMD=%%~fD\python.exe"
            goto :run_with_python
        )
    )
)

for /d %%D in ("%ProgramFiles%\Python3*") do (
    if exist "%%~fD\python.exe" (
        "%%~fD\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
        if not errorlevel 1 (
            set "PY_CMD=%%~fD\python.exe"
            goto :run_with_python
        )
    )
)

goto :try_embedded_earth

:run_with_python
if defined PY_ARG (
    "%PY_CMD%" %PY_ARG% "%SETUP_PY%" uninstall %*
) else (
    "%PY_CMD%" "%SETUP_PY%" uninstall %*
)
set "EXIT_CODE=%ERRORLEVEL%"
goto :finish

:try_embedded_earth
set "BNE_EXE="
set "LAST_TARGET_FILE=%LocalAppData%\BeamNG-Earth-Autodrive-Fixes\last_target.txt"
if exist "%LAST_TARGET_FILE%" (
    set /p BNE_EXE=<"%LAST_TARGET_FILE%"
)
if defined BNE_EXE (
    if exist "%BNE_EXE%" goto :run_with_earth
    set "BNE_EXE="
)

for %%F in (
    "%SCRIPT_DIR%BeamNG.Earth*.exe"
    "%SCRIPT_DIR%..\BeamNG.Earth*.exe"
    "%USERPROFILE%\Downloads\BeamNG.Earth*.exe"
    "%USERPROFILE%\Desktop\BeamNG.Earth*.exe"
    "%OneDrive%\Desktop\BeamNG.Earth*.exe"
    "%LocalAppData%\Programs\BeamNG.Earth\BeamNG.Earth*.exe"
) do (
    if exist "%%~fF" (
        echo "%%~nxF" | findstr /i /c:".original" /c:".pending" /c:".swap." >nul
        if errorlevel 1 (
            set "BNE_EXE=%%~fF"
            goto :run_with_earth
        )
    )
)

echo.
echo  ========================================================================
echo   BeamNG.Earth - Traffic and AutoDrive fixes (Uninstall Help)
echo  ========================================================================
echo.
echo   Could not find Python or BeamNG.Earth.exe automatically.
echo.
echo   Easy ways to restore your original BeamNG.Earth:
echo     - Move this extracted folder next to your BeamNG.Earth.exe and
echo       double-click 1_Click_Uninstall.bat again, OR
echo     - Inside BeamNG.Earth -^> Add-ons -^> Traffic and AutoDrive fixes -^> Options:
echo       choose "Restore original app", click Save, click Start, and
echo       restart BeamNG.Earth.
echo.
if not "%BNE_ADF_NO_PAUSE%"=="1" pause
exit /b 1

:run_with_earth
"%BNE_EXE%" --run-addon "%SETUP_PY%" uninstall --self-exe "%BNE_EXE%" %*
set "EXIT_CODE=%ERRORLEVEL%"

:finish
echo.
if not "%BNE_ADF_NO_PAUSE%"=="1" pause
exit /b %EXIT_CODE%
