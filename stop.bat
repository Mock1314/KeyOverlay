@echo off
rem Force-close every key/mouse overlay instance (no hotkey needed).
rem Pure ASCII on purpose.
cd /d "%~dp0"
set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0stop.py"
ping -n 3 127.0.0.1 >nul
