@echo off
if not exist "C:\Users\sam\cena-ai-assistant\logs" mkdir "C:\Users\sam\cena-ai-assistant\logs"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0toast_mirror_poll_run.ps1" -Mode dimensions 1>> "C:\Users\sam\cena-ai-assistant\logs\toast_mirror_dimension_sync.out.log" 2>> "C:\Users\sam\cena-ai-assistant\logs\toast_mirror_dimension_sync.err.log"
