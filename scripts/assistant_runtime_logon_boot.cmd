@echo off
rem Boot-gap closer: starts the CENA assistant runtime at logon (companion to
rem the CenasAssistantRuntime8782 task, which has no boot trigger).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\sam\cenas-kitchen-runtime\scripts\assistant_ck_runtime_run.ps1" -ProjectRoot "C:\Users\sam\cena-ai-assistant" -RepoRoot "C:\Users\sam\cenas-kitchen-runtime" -Hosts "127.0.0.1,100.73.38.82"
