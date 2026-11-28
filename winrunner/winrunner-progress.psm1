# Set defaults for this PowerShell process without filtering the captured pipes.
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$global:OutputEncoding = [Console]::OutputEncoding
$global:ProgressPreference = 'SilentlyContinue'
