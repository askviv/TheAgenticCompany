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
parser.add_argument('--smtp-port',type=int,default=19126)
parser.add_argument('--api-port',type=int,default=19158)
parser.add_argument('--direct',action='store_true',help='Forward directly to an existing local mailbox service')
parser.add_argument('--label-prefix',default='org.example')
parser.add_argument('--reply-listen-port',type=int,help='Optional Mac loopback port for company SMTP submission')
parser.add_argument('--reply-smtp-host',help='Company submission hostname reachable from the SSH server')
parser.add_argument('--reply-smtp-port',type=int,help='Company submission port reachable from the SSH server')
args=parser.parse_args()
reply_options=(args.reply_listen_port,args.reply_smtp_host,args.reply_smtp_port)
if any(value is not None for value in reply_options) and not all(value is not None for value in reply_options):
    parser.error('reply-listen-port, reply-smtp-host and reply-smtp-port must be supplied together')
base=Path.home()/'.local/share/tac-smtp4dev';base.mkdir(parents=True,exist_ok=True);base.chmod(0o700)
# launchd cannot read scripts in macOS-protected Documents without broad permissions.
shutil.copy2(Path(__file__).with_name('forward.py'),base/'forward.py')
launch=Path.home()/'Library/LaunchAgents';launch.mkdir(parents=True,exist_ok=True)
programs={
 f'{args.label_prefix}.tac-smtp4dev-forward':['/usr/bin/python3',str(base/'forward.py')],
 f'{args.label_prefix}.tac-smtp4dev-tunnel':['/usr/bin/ssh','-F','/dev/null','-i',str(args.ssh_key.expanduser()),'-N','-T',
    '-o','BatchMode=yes','-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
    '-R',f'127.0.0.1:19125:127.0.0.1:{args.smtp_port}','-R',f'127.0.0.1:19157:127.0.0.1:{args.api_port}',args.mini]}
if args.reply_listen_port is not None:
    tunnel=programs[f'{args.label_prefix}.tac-smtp4dev-tunnel']
    tunnel[-1:-1]=['-L',f'127.0.0.1:{args.reply_listen_port}:{args.reply_smtp_host}:{args.reply_smtp_port}']
if args.direct:
    label=f'{args.label_prefix}.tac-smtp4dev-forward'
    if subprocess.run(['launchctl','print',f'gui/{os.getuid()}/{label}'],capture_output=True).returncode==0:
        subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}/{label}'],check=True)
    programs.pop(label)
    (launch/(label+'.plist')).unlink(missing_ok=True)
for label,argv in programs.items():
    path=launch/(label+'.plist')
    path.write_bytes(plistlib.dumps({'Label':label,'ProgramArguments':argv,'RunAtLoad':True,'KeepAlive':True,
        'ThrottleInterval':10,'StandardOutPath':str(base/(label+'.out.log')),
        'StandardErrorPath':str(base/(label+'.err.log'))}))
    loaded=subprocess.run(['launchctl','print',f'gui/{os.getuid()}/{label}'],capture_output=True).returncode==0
    if loaded: subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}/{label}'],check=True)
    subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)],check=True)
    print('Installed',label)
