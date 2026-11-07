# Start RuleTrace for a demo with a clean slate: no old designs, no old check history.
#   powershell -ExecutionPolicy Bypass -File .\start-demo.ps1            (port 8002)
#   powershell -ExecutionPolicy Bypass -File .\start-demo.ps1 -Port 8010 -Keep   (keep existing data)
param([int]$Port = 8002, [switch]$Keep)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (Test-Path ".\.venv\Scripts\Activate.ps1") { . .\.venv\Scripts\Activate.ps1 }
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) { Write-Host "Port $Port is in use; stopping that process."; Stop-Process -Id $busy.OwningProcess -Force; Start-Sleep 1 }
if (-not $Keep) {
  Remove-Item -Recurse -Force .\data -ErrorAction SilentlyContinue
  Remove-Item -Force .\vartija.db -ErrorAction SilentlyContinue
  Write-Host "Clean start: old designs and check history removed."
}
if (Get-Command ollama -ErrorAction SilentlyContinue) { Write-Host "AI helper: Ollama found." } else { Write-Host "AI helper: Ollama not found (explanations fall back to the standard text)." }
if (Get-Command tesseract -ErrorAction SilentlyContinue) { Write-Host "OCR: Tesseract found." } else { Write-Host "OCR: Tesseract not found (drawings without text can't be read)." }
Start-Job -ScriptBlock { param($p) Start-Sleep 4; Start-Process "http://localhost:$p/ruletrace" } -ArgumentList $Port | Out-Null
python -m uvicorn ruletrace.app:app --port $Port
