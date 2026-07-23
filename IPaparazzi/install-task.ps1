[CmdletBinding()]
param(
    [ValidateSet(1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60)]
    [int]$IntervalMinutes = 15,

    [string]$PythonExe = "python",

    [string]$ScriptPath = (Join-Path $PSScriptRoot "IPaparazzi.py"),

    [string]$ConfigPath = (Join-Path $PSScriptRoot "IPaparazzi.toml"),

    [string]$TaskName = "IPaparazzi",

    [switch]$RunAsSystem,

    [switch]$SkipAclHardening
)

$ErrorActionPreference = "Stop"

function Resolve-RequiredFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label not found: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Protect-ConfigFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    $administrators = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)

    foreach ($identity in @($currentUser, $system, $administrators)) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $identity,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

$resolvedScript = Resolve-RequiredFile -Path $ScriptPath -Label "IPaparazzi script"
$resolvedConfig = Resolve-RequiredFile -Path $ConfigPath -Label "IPaparazzi config"
$pythonCommand = Get-Command -Name $PythonExe -ErrorAction Stop
$resolvedPython = $pythonCommand.Source

& $resolvedPython $resolvedScript --config $resolvedConfig --check-config
if ($LASTEXITCODE -ne 0) {
    throw "IPaparazzi configuration validation failed with exit code $LASTEXITCODE"
}

if (-not $SkipAclHardening) {
    Protect-ConfigFile -Path $resolvedConfig
}

$arguments = '"{0}" --config "{1}"' -f $resolvedScript, $resolvedConfig
$action = New-ScheduledTaskAction `
    -Execute $resolvedPython `
    -Argument $arguments `
    -WorkingDirectory (Split-Path -Parent $resolvedScript)
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

if ($RunAsSystem) {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest
}
else {
    $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal `
        -UserId $currentIdentity `
        -LogonType Interactive `
        -RunLevel Limited
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Monitors the public IPv4 and reconciles configured Cloudflare A records." `
    -Force | Out-Null

Write-Host "IPaparazzi scheduled task installed: $TaskName"
Write-Host "Interval: $IntervalMinutes minute(s)"
Write-Host "Config: $resolvedConfig"
if (-not $RunAsSystem) {
    Write-Host "The task runs for $currentIdentity while that user is logged on."
}
