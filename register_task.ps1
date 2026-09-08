<#
.SYNOPSIS
    스크리너 데이터 자동 갱신을 Windows 작업 스케줄러에 등록한다.

.DESCRIPTION
    auto_update.py를 평일 지정 시각에 실행한다. 두 스크리너를 돌리고 결과 JSON이 바뀌었으면
    커밋·푸시하며, GitHub Pages 워크플로가 배포를 이어받는다.

    로그인한 사용자 계정으로, 로그인 상태일 때만 돌도록 등록한다. git push가 Windows 자격 증명
    관리자에 저장된 GitHub 토큰을 써야 하는데, "로그인하지 않아도 실행"으로 걸면 그 자격증명에
    접근하지 못해 푸시가 실패하기 때문이다.

    기본 시각을 18:10으로 잡은 이유: 국내 장 마감(15:30) 이후라 당일 일봉이 확정되고,
    미국은 직전 거래일 종가가 반영된다. 스윙 스크리닝이라 하루 한 번이면 충분하다.

.PARAMETER Time
    실행 시각 (HH:mm). 기본 18:10.

.PARAMETER Remove
    등록된 작업을 삭제한다.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File register_task.ps1
    powershell -ExecutionPolicy Bypass -File register_task.ps1 -Time 07:30
    powershell -ExecutionPolicy Bypass -File register_task.ps1 -Remove
#>
param(
    [string]$Time = "18:10",
    [string]$TaskName = "StockScreener-AutoUpdate",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "작업을 삭제했습니다: $TaskName"
    } else {
        Write-Host "등록된 작업이 없습니다: $TaskName"
    }
    return
}

$python = (Get-Command python).Source
if (-not $python) { throw "python을 찾지 못했습니다. PATH를 확인하세요." }

# pythonw.exe가 있으면 콘솔 창을 띄우지 않는다. 로그는 logs/auto_update.log에 남는다.
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
$exe = if (Test-Path $pythonw) { $pythonw } else { $python }

$action = New-ScheduledTaskAction -Execute $exe `
    -Argument "auto_update.py --quiet" -WorkingDirectory $repo

$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "스크리너 데이터 갱신 후 GitHub에 푸시 (Pages 자동 배포)" | Out-Null

Write-Host "등록 완료: $TaskName"
Write-Host "  실행     : $exe auto_update.py --quiet"
Write-Host "  작업 폴더: $repo"
Write-Host "  일정     : 평일 $Time (놓치면 다음 로그인 시 실행)"
Write-Host "  로그     : $repo\logs\auto_update.log"
Write-Host ""
Write-Host "지금 한 번 돌려보려면:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
