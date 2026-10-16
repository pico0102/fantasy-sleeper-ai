# Automated Deploy Gate for fantasy-ai-service
# Enforces pre-deploy test gate, zero-traffic canary deployment, live probe verification, and conditional traffic promotion.

param(
    [string]$Region = "us-east1",
    [string]$ServiceName = "fantasy-ai-service",
    [string]$Tag = "candidate"
)

$ErrorActionPreference = "Stop"
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "STAGE 1: Pre-Deploy In-Process Regression Gate" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# Run full test suite locally first
& .\.venv\Scripts\pytest.exe -v
if ($LASTEXITCODE -ne 0) {
    Write-Error "Pre-deploy test gate failed! Aborting deployment before building container."
    exit 1
}
Write-Host "✅ Stage 1 Pre-Deploy Gate Passed.`n" -ForegroundColor Green

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "STAGE 2: Zero-Traffic Canary Deployment to Cloud Run" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# Deploy new revision with --no-traffic and --tag candidate
gcloud run deploy $ServiceName `
    --source . `
    --region $Region `
    --platform managed `
    --allow-unauthenticated `
    --no-traffic `
    --tag $Tag `
    --set-secrets "GEMINI_API_KEY=gemini-api-key:latest,ODDS_API_KEY=odds-api-key:latest" `
    --set-env-vars "SLEEPER_LEAGUE_ID=1389359956657967104,SLEEPER_SURVIVOR_LEAGUE_ID=1393775027442167808,SLEEPER_USER_ID=pinec0ne" `
    --quiet

if ($LASTEXITCODE -ne 0) {
    Write-Error "Cloud Run container build/deploy failed. Existing production traffic untouched."
    exit 1
}

# Resolve candidate revision URL
$CandidateUrl = "https://$Tag---$ServiceName-519501469706.$Region.run.app"
Write-Host "✅ Stage 2 Complete. Candidate revision deployed at: $CandidateUrl (0% traffic)`n" -ForegroundColor Green

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "STAGE 3: Live Deploy-Gate Probes Against Candidate Revision" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$env:LIVE_SERVICE_URL = $CandidateUrl
& .\.venv\Scripts\pytest.exe tests/test_qa_probes.py -v

if ($LASTEXITCODE -ne 0) {
    Write-Host "`n🚨 DEPLOY GATE FAILED! 🚨" -ForegroundColor Red
    Write-Host "One or more live QA probes failed on candidate revision: $CandidateUrl" -ForegroundColor Red
    Write-Host "TRAFFIC WAS NOT SWITCHED. Production remains 100% on the prior healthy revision." -ForegroundColor Yellow
    exit 1
}
Write-Host "✅ Stage 3 Live Deploy Gate Passed (18/18 probes verified).`n" -ForegroundColor Green

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "STAGE 4: Promoting Candidate Revision to 100% Traffic" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

gcloud run services update-traffic $ServiceName `
    --region $Region `
    --to-latest `
    --quiet

if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed updating traffic to latest revision."
    exit 1
}

Write-Host "`n🎉 DEPLOY SUCCESSFUL! All gates passed and traffic promoted to 100%." -ForegroundColor Green
