@echo off
setlocal
call "%~dp0Utils\copernicus-classifier\Check-Copernicus.cmd" --ortho "%~dp0." --output "%~dp0Height-Reports" %*
exit /b %ERRORLEVEL%
