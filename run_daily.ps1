# MOTrade 每日定时任务入口。由 Windows 任务计划程序调用，非交互模式跑一次分析。
# 只在本机执行，不涉及任何云端/第三方账号。

$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$logDir = Join-Path $PSScriptRoot "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("run-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

$prompt = Get-Content -Raw (Join-Path $PSScriptRoot "daily_prompt.txt")

$allowedTools = "Bash,Read,Write,Edit,Glob,Grep,Artifact,PushNotification"

("=== MOTrade daily run " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " ===") | Out-File -Append -Encoding utf8 $logFile

claude -p $prompt `
  --allowedTools $allowedTools `
  --permission-mode acceptEdits `
  --permission-prompts none `
  --output-format text `
  2>&1 | Out-File -Append -Encoding utf8 $logFile
