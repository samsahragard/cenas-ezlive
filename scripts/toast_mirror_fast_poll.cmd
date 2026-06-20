@echo off
if not exist "C:\Users\sam\cena-ai-assistant\logs" mkdir "C:\Users\sam\cena-ai-assistant\logs"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0toast_mirror_poll_run.ps1" -Mode fast 1>> "C:\Users\sam\cena-ai-assistant\logs\toast_mirror_fast_poll.out.log" 2>> "C:\Users\sam\cena-ai-assistant\logs\toast_mirror_fast_poll.err.log"
