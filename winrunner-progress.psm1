# Set defaults for this PowerShell process without filtering the captured pipes.
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$global:ProgressPreference = 'SilentlyContinue'
