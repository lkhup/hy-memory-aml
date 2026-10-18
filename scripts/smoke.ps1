param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][string]$Token
)

$base = $BaseUrl.TrimEnd('/')
$suffix = [Guid]::NewGuid().ToString('N')
$userId = "smoke:user:$suffix"
$sessionId = "smoke:session:$suffix"
$requestId = "smoke:add:$suffix"
$headers = @{ Authorization = "Bearer $Token" }

Write-Host "Checking health..."
Invoke-RestMethod -Method Get -Uri "$base/health" | ConvertTo-Json -Depth 8

$addBody = @{
    request_id = $requestId
    user_id = $userId
    session_id = $sessionId
    messages = @(
        @{
            role = 'user'
            content = 'My favorite observatory is on Mauna Kea.'
            timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        }
    )
} | ConvertTo-Json -Depth 8

Write-Host "Writing memory..."
Invoke-RestMethod -Method Post -Uri "$base/add" -Headers $headers `
    -ContentType 'application/json' -Body $addBody | ConvertTo-Json -Depth 8

$searchBody = @{
    query = 'Which observatory location does the user prefer?'
    options = @('A. Mauna Kea', 'B. Atacama')
    user_id = $userId
    top_k = 100
} | ConvertTo-Json -Depth 8

Write-Host "Searching memory..."
$result = Invoke-RestMethod -Method Post -Uri "$base/search" -Headers $headers `
    -ContentType 'application/json' -Body $searchBody
$result | ConvertTo-Json -Depth 8

if (-not $result.data -or $result.data.Count -eq 0) {
    throw 'Smoke failed: Search returned no memories.'
}

Write-Host 'Smoke passed.'

