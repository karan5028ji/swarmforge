# SwarmForge — Windows setup helper
# Detects AI CLI tools and optionally installs missing ones.
# Usage:  .\setup.ps1          (check only)
#         .\setup.ps1 -Install (interactive install)

param([switch]$Install)

$ErrorActionPreference = "Stop"

$tools = @(
    @{ Name = "opencode"; Check = "opencode"; Npm = "npm i -g opencode-ai"; Curl = "" },
    @{ Name = "agy (Google Antigravity)"; Check = "agy"; Npm = ""; Curl = "curl -fsSL https://antigravity.google/cli/install.sh | bash" },
    @{ Name = "gemini"; Check = "gemini"; Npm = "npm i -g @google/gemini-cli"; Curl = "" },
    @{ Name = "grok (xAI)"; Check = "grok"; Npm = ""; Curl = "curl -fsSL https://x.ai/cli/install.sh | bash" },
    @{ Name = "copilot (GitHub)"; Check = "copilot"; Npm = "npm i -g @github/copilot"; Curl = "" }
)

Write-Host ""
Write-Host "SwarmForge — tool check" -ForegroundColor Cyan
Write-Host ("-" * 50)

$missing = @()
foreach ($t in $tools) {
    $found = Get-Command $t.Check -ErrorAction SilentlyContinue
    if ($found) {
        Write-Host ("  [ok]  {0,-28} -> {1}" -f $t.Name, $found.Source) -ForegroundColor Green
    }
    else {
        Write-Host ("  [x]   {0,-28} missing" -f $t.Name) -ForegroundColor Red
        $missing += $t
    }
}

if ($missing.Count -eq 0) {
    Write-Host ""
    Write-Host "Sab available. Chalo: python forge.py `"<task>`"" -ForegroundColor Green
    exit 0
}

if (-not $Install) {
    Write-Host ""
    Write-Host "Install ke liye: .\setup.ps1 -Install" -ForegroundColor Yellow
    exit 0
}

foreach ($t in $missing) {
    Write-Host ""
    Write-Host "[$($t.Name)]" -ForegroundColor Cyan
    $cmd = if (Get-Command npm -ErrorAction SilentlyContinue) { $t.Npm } else { $t.Curl }
    if ($cmd) {
        Write-Host "  install: $cmd"
        $ans = Read-Host "  Install? (y/n)"
        if ($ans -eq "y") {
            Write-Host "  Running: $cmd"
            Invoke-Expression $cmd
        }
    }
    else {
        Write-Host "  (koi install command is platform pe available nahi)"
    }
}

Write-Host ""
Write-Host "Done. Naya terminal kholo aur phir se check karo: .\setup.ps1" -ForegroundColor Yellow
