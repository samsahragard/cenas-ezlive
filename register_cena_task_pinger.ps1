# Register CenaTaskPinger as a user-scope scheduled task at logon.
# Companion to register_cena_task.ps1 + register_cena_chat_watcher.ps1.
# Per Cena #1816: 10-minute progress ping while a task is marked active.

try {
    $existing = Get-ScheduledTask -TaskName 'CenaTaskPinger' -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Output "existing principal RunLevel: $($existing.Principal.RunLevel)"
        try {
            Unregister-ScheduledTask -TaskName 'CenaTaskPinger' -Confirm:$false
            Write-Output 'unregistered old task'
        } catch {
            Write-Output "unregister failed: $_"
        }
    } else {
        Write-Output 'no existing CenaTaskPinger task'
    }
    # Full python.exe path — Task Scheduler PATH lookup is unreliable
    # for per-user Python installs (same gotcha as register_cena_task.ps1).
    $Action = New-ScheduledTaskAction `
        -Execute 'C:\Users\sam\AppData\Local\Programs\Python\Python314\python.exe' `
        -Argument 'C:\Users\sam\cena\cena_task_pinger.py' `
        -WorkingDirectory 'C:\Users\sam\cena'
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $Settings = New-ScheduledTaskSettingsSet `
        -RestartCount 5 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Days 365) `
        -StartWhenAvailable
    $Principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited
    Register-ScheduledTask -TaskName 'CenaTaskPinger' `
        -Action $Action -Trigger $Trigger `
        -Settings $Settings -Principal $Principal `
        -Force | Out-Null
    Write-Output 'registered new task (RunLevel Limited)'
    $t = Get-ScheduledTask -TaskName 'CenaTaskPinger'
    Write-Output "verified RunLevel: $($t.Principal.RunLevel)"
    Write-Output "verified UserId:   $($t.Principal.UserId)"
    Write-Output "verified State:    $($t.State)"
} catch {
    Write-Output "FAILED: $_"
}
