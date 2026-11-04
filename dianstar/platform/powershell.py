"""A single native PowerShell bootstrap; no model-controlled runtime options."""
from pathlib import Path
import os
from .shells import find_powershell


def powershell_argv(command, root):
    if not command.strip() or '\0' in command or len(command)>16000:
        raise ValueError('Invalid PowerShell script')
    location=str(root).replace("'","''")
    script=("$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        "New-PSDrive -Name DSWork -PSProvider FileSystem -Root '"+location+"' | Out-Null; "
        "Set-Location -LiteralPath 'DSWork:\\'; & { "+command+
        " } | Out-String -Stream -Width 240; if (-not $?) { exit 1 }; "
        "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }")
    return [str(find_powershell()),'-NoLogo','-NoProfile','-NonInteractive',
        '-OutputFormat','XML','-Command',script]


def command_from_argv(argv):
    selected=find_powershell()
    legacy=Path(os.environ['SYSTEMROOT'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    if Path(argv[0]).resolve(strict=True) not in {selected,legacy.resolve()}:
        raise ValueError('Unsupported PowerShell runtime')
    index=1
    while index<len(argv):
        option=argv[index].casefold()
        if option in {'-nologo','-noprofile','-noninteractive'}:
            index+=1
        elif option=='-outputformat' and index+1<len(argv) and argv[index+1].casefold() in {'text','xml'}:
            index+=2  # Output is always transported as Unicode CLIXML.
        elif option in {'-command','-c'} and index+2==len(argv) and argv[index+1]!='-':
            return argv[index+1]
        else:
            raise ValueError('Unsupported PowerShell arguments')
    raise ValueError('PowerShell requires a command')
