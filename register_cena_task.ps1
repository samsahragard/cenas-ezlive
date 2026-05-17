# Re-register CenaGateway scheduled task at user-level (no elevation).
# Used by aick to drop the legacy RunLevel Highest setup.

try {
    $existing = Get-ScheduledTask -TaskName 'CenaGateway' -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Output "existing principal RunLevel: $($existing.Principal.RunLevel)"
        try {
            Unregister-ScheduledTask -TaskName 'CenaGateway' -Confirm:$false
            Write-Output 'unregistered old task'
        } catch {
            Write-Output "unregister failed: $_"
        }
    } else {
        Write-Output 'no existing task to unregister'
    }
    # Full python.exe path because Task Scheduler's PATH doesn't
    # include the per-user Python install (causes 0x80070002 /
    # ERROR_FILE_NOT_FOUND on Last Result when relative).
    $Action = New-ScheduledTaskAction `
        -Execute 'C:\Users\sam\AppData\Local\Programs\Python\Python314\python.exe' `
        -Argument 'C:\Users\sam\cena\cena_gateway.py' `
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
    Register-ScheduledTask -TaskName 'CenaGateway' `
        -Action $Action -Trigger $Trigger `
        -Settings $Settings -Principal $Principal `
        -Force | Out-Null
    Write-Output 'registered new task (RunLevel Limited)'
    $t = Get-ScheduledTask -TaskName 'CenaGateway'
    Write-Output "verified RunLevel: $($t.Principal.RunLevel)"
    Write-Output "verified UserId:   $($t.Principal.UserId)"
    Write-Output "verified State:    $($t.State)"
} catch {
    Write-Output "FAILED: $_"
}
