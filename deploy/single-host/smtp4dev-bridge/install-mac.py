#!/usr/bin/env python3
"""Install private, user-owned Kubernetes and SSH forwards for the company mail server."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--ssh-key',type=Path,required=True)
parser.add_argument('--mini',default='company@192.0.2.10')
args=parser.parse_args()
base=Path.home()/'.local/share/tac-smtp4dev';base.mkdir(parents=True,exist_ok=True);base.chmod(0o700)
# launchd cannot read scripts in macOS-protected Documents without broad permissions.
shutil.copy2(Path(__file__).with_name('forward.py'),base/'forward.py')
launch=Path.home()/'Library/LaunchAgents';launch.mkdir(parents=True,exist_ok=True)
programs={
 'org.example.tac-smtp4dev-forward':['/usr/bin/python3',str(base/'forward.py')],
 'org.example.tac-smtp4dev-tunnel':['/usr/bin/ssh','-F','/dev/null','-i',str(args.ssh_key.expanduser()),'-N','-T',
    '-o','BatchMode=yes','-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
    '-R','127.0.0.1:19125:127.0.0.1:19126','-R','127.0.0.1:19157:127.0.0.1:19158',args.mini]}
for label,argv in programs.items():
    path=launch/(label+'.plist')
    path.write_bytes(plistlib.dumps({'Label':label,'ProgramArguments':argv,'RunAtLoad':True,'KeepAlive':True,
        'ThrottleInterval':10,'StandardOutPath':str(base/(label+'.out.log')),
        'StandardErrorPath':str(base/(label+'.err.log'))}))
    loaded=subprocess.run(['launchctl','print',f'gui/{os.getuid()}/{label}'],capture_output=True).returncode==0
    if loaded: subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}/{label}'],check=True)
    subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)],check=True)
    print('Installed',label)
