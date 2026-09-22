# Acceptance chain for external test sets. ASCII only (PowerShell 5.1 reads .ps1 as ANSI).
# Runs each stage in a supervisor loop: native crashes (0xC0000005) are resumed with --resume.
# Usage (repo root, hidden window recommended):
#   Start-Process pwsh -WindowStyle Hidden -ArgumentList '-NoProfile','-File','docs/research/benchmark-suite/runners/run_acceptance.ps1','-Stages','locomo,longmemeval','-EnvFile','D:\4_Projects\.env'
param(
    [string]$Stages = "personamem,locomo,longmemeval",
    [string]$EnvFile = "D:\4_Projects\.env",
    [string]$Tag = "final",
    [int]$Jobs = 1
)
$ErrorActionPreference = "Continue"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\.."))
$env:AGENT_MEMORY_EMBEDDING_MAX_SEQ_LENGTH = "512"
$env:PYTHONIOENCODING = "utf-8"
$env:MC_CODEBUDDY_CONCURRENCY = "4"
$common = @("--system-via", "codebuddy", "--system-model", "deepseek-v4.1-flash",
            "--answer-via", "codebuddy", "--answer-model", "deepseek-v4.1-flash",
            "--judge-role", "judge_codebuddy", "--judge-model", "glm-5.3-flash",
            "--env-file", $EnvFile, "--resume")
function Run-Stage($runId, $extra) {
    $log = "data\logs\aml_selftest\$runId.log"
    for ($i = 0; $i -lt 80; $i++) {
        uv run python docs/research/benchmark-suite/runners/aml_selftest.py --run-id $runId @extra @common *>> $log
        if ($LASTEXITCODE -eq 0) { "SUPERVISOR: finished" >> $log; return }
        "SUPERVISOR: exit $LASTEXITCODE, resuming" >> $log
        Start-Sleep -Seconds 5
    }
}
foreach ($s in $Stages.Split(",")) {
    switch ($s.Trim()) {
        "personamem"  { Run-Stage "$Tag-personamem"  @("--data", "data/external/personamem/test_b.json", "--n-per-type", "99", "--top-k", "20", "--systems", "naive_rag,full_context,am", "--jobs", "1") }
        "locomo"      { Run-Stage "$Tag-locomo"      @("--data", "data/external/locomo/sample_b.json", "--n-per-type", "99", "--systems", "naive_rag,am", "--jobs", "1") }
        "longmemeval" { Run-Stage "$Tag-longmemeval" @("--data", "data/external/longmemeval/lme_s_sample_seed1_n10.json", "--n-per-type", "10", "--systems", "am", "--jobs", "$Jobs") }
    }
}
