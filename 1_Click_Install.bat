@echo off
setlocal EnableExtensions DisableDelayedExpansion
title BeamNG.Earth - Traffic and AutoDrive fixes (1-Click Install)

set "SCRIPT_DIR=%~dp0"
set "SETUP_PY=%SCRIPT_DIR%tools\easy_setup.py"

if exist "%SETUP_PY%" goto :find_python

echo.
echo  ========================================================================
echo   BeamNG.Earth - Traffic and AutoDrive fixes
echo  ========================================================================
echo.
echo   It looks like this file was opened directly inside a ZIP archive.
echo.
echo   What to do:
echo     1. Close this window.
echo     2. Right-click the downloaded .zip file and click "Extract All..."
echo     3. Open the extracted folder and double-click 1_Click_Install.bat.
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
    "%PY_CMD%" %PY_ARG% "%SETUP_PY%" install %*
) else (
    "%PY_CMD%" "%SETUP_PY%" install %*
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
echo   BeamNG.Earth - Traffic and AutoDrive fixes (Setup Help)
echo  ========================================================================
echo.
echo   Python was not found on this PC, and BeamNG.Earth.exe was not in
echo   your Downloads or Desktop folder yet.
echo.
echo   Choose ANY ONE of these easy options (no coding needed):
echo.
echo     Option A (No Python needed):
echo       Move this extracted folder into the same folder as your
echo       BeamNG.Earth.exe, then double-click 1_Click_Install.bat again.
echo.
echo     Option B (Inside BeamNG.Earth):
echo       Open BeamNG.Earth -^> Add-ons -^> Add from GitHub, paste:
echo         CodeWMLandfill/BeamNG-Earth-Traffic-and-AutoDrive-Fixes
echo       Click Install, then click Start.
echo.
echo     Option C (Install Python 3.12):
echo       Download Python 3.12 from https://www.python.org/downloads/
echo       (check "Add python.exe to PATH" during setup), then run this again.
echo.
if not "%BNE_ADF_NO_PAUSE%"=="1" pause
exit /b 1

:run_with_earth
"%BNE_EXE%" --run-addon "%SETUP_PY%" install --self-exe "%BNE_EXE%" %*
set "EXIT_CODE=%ERRORLEVEL%"

:finish
echo.
if not "%BNE_ADF_NO_PAUSE%"=="1" pause
exit /b %EXIT_CODE%
