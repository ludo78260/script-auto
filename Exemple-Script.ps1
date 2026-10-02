<#
 Exemple-Script.ps1  -  script distingue a deployer avec RemoteDeploy.
 Il sert de test de bout en bout : execute sur chaque poste distant, il ecrit
 une trace locale et cree un fichier "preuve" que RemoteDeploy peut verifier.
#>
[CmdletBinding()]
param([string]$Message = 'Deploiement RemoteDeploy')

$debut = Get-Date
$ligne = '[{0}] {1} - poste {2} - utilisateur {3}' -f $debut.ToString('dd/MM/yyyy HH:mm:ss'), $Message, $env:COMPUTERNAME, $env:USERNAME
Write-Output $ligne

$trace = 'C:\ProgramData\_DeployRun\trace.txt'
try {
    $dir = Split-Path -Parent $trace
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Add-Content -LiteralPath $trace -Value $ligne -Encoding UTF8
} catch { Write-Output ("  ! trace impossible : " + $_.Exception.Message) }

try {
    $cible = 'C:\ProgramData\_DeployRun'
    Set-Content -LiteralPath (Join-Path $cible 'done.flag') -Value $ligne -Encoding UTF8
    Write-Output '  OK : fichier preuve cree (C:\ProgramData\_DeployRun\done.flag)'
} catch {
    Write-Output ("  ! echec de la creation du fichier preuve : " + $_.Exception.Message)
    exit 1
}

Write-Output ('  duree du script : {0:N1} s' -f ((Get-Date) - $debut).TotalSeconds)
exit 0

# VARIANTES UTILES (decommentez si besoin) :
#   exit 1                        -> force un echec pour tester la relance automatique
#   Start-Sleep -Seconds 300      -> script long (ajustez "duree max d'execution")
#   Restart-Computer -Force       -> redemarre le poste en fin de traitement
# Vos scripts doivent etre idempotents : un poste peut recevoir le script plusieurs fois.