@echo off
setlocal
if defined ORTHO_AUDIT_PYTHON goto run
set "ORTHO_AUDIT_PYTHON=%~dp0..\..\venv\Scripts\python.exe"
"%ORTHO_AUDIT_PYTHON%" -c "import numpy, rasterio" >nul 2>nul
if not errorlevel 1 goto run
set "ORTHO_AUDIT_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%ORTHO_AUDIT_PYTHON%" goto run
echo A working Python with NumPy was not found.
echo Set ORTHO_AUDIT_PYTHON to a working Python executable and launch again.
if not defined ORTHO_CLASSIFIER_NO_PAUSE pause
exit /b 1

:run
echo Checking installed DSF heights. Scenery and Ortho4XP files are read-only.
echo Press Ctrl+C to stop. Launch again to resume using cached evidence.
"%ORTHO_AUDIT_PYTHON%" "%~dp0classifier.py" %*
set "ORTHO_AUDIT_EXIT=%ERRORLEVEL%"
if not defined ORTHO_CLASSIFIER_NO_PAUSE pause
exit /b %ORTHO_AUDIT_EXIT%
