# Acceptance chain for external test sets. ASCII only (PowerShell 5.1 reads .ps1 as ANSI).
# Runs each stage in a supervisor loop: native crashes (0xC0000005) are resumed with --resume.
# Usage (repo root, hidden window recommended):
#   Start-Process pwsh -WindowStyle Hidden -ArgumentList '-NoProfile','-File','docs/research/benchmark-suite/runners/run_acceptance.ps1','-Stages','locomo,longmemeval','-EnvFile','<path to your .env>'
# -EnvFile: a dotenv file holding the API keys (default: .env in the repo root).
param(
    [string]$Stages = "personamem,locomo,longmemeval",
    [string]$EnvFile = ".env",
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
# Exit code 0 does not mean every question succeeded: failed questions are written as error rows.
# Keep resuming while error rows remain; give up after 4 clean exits that still leave errors.
function Count-ErrorRows($runId) {
    $f = "data/logs/aml_selftest/$runId/results.jsonl"
    if (-not (Test-Path $f)) { return 0 }
    $py = "import json,sys;d={};[d.__setitem__((r['question_id'],r['system']),r) for r in map(json.loads,open(sys.argv[1],encoding='utf-8'))];print(sum(1 for r in d.values() if r.get('error')))"
    return [int](uv run python -c $py $f)
}
function Run-Stage($runId, $extra) {
    $log = "data\logs\aml_selftest\$runId.log"
    $clean = 0
    for ($i = 0; $i -lt 80; $i++) {
        uv run python docs/research/benchmark-suite/runners/aml_selftest.py --run-id $runId @extra @common *>> $log
        $code = $LASTEXITCODE
        $errs = Count-ErrorRows $runId
        if ($code -eq 0 -and $errs -eq 0) { "SUPERVISOR: finished" >> $log; return }
        if ($code -eq 0) { $clean++ } else { $clean = 0 }
        if ($clean -ge 4) { "SUPERVISOR: giving up, $errs error rows after 4 clean passes" >> $log; return }
        "SUPERVISOR: exit $code, $errs error rows, resuming" >> $log
        Start-Sleep -Seconds 30
    }
}
foreach ($s in $Stages.Split(",")) {
    switch ($s.Trim()) {
        "personamem"  { Run-Stage "$Tag-personamem"  @("--data", "data/external/personamem/test_b.json", "--n-per-type", "99", "--top-k", "20", "--systems", "naive_rag,full_context,am", "--jobs", "1") }
        "locomo"      { Run-Stage "$Tag-locomo"      @("--data", "data/external/locomo/sample_b.json", "--n-per-type", "99", "--systems", "naive_rag,am", "--jobs", "1") }
        "longmemeval" { Run-Stage "$Tag-longmemeval" @("--data", "data/external/longmemeval/lme_s_sample_seed1_n10.json", "--n-per-type", "10", "--systems", "am", "--jobs", "$Jobs") }
    }
}
