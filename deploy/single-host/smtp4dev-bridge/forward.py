#!/usr/bin/env python3
"""Expose the one shared local smtp4dev service only on Mac loopback."""
import json
import os
import subprocess

kubectl='/usr/local/bin/kubectl'
# Use the inspected local API listener while retaining its certificate name.
# Keep certificate verification against the configured local API certificate.
base=[kubectl,'--context','docker-desktop','--server=https://127.0.0.1:6443',
    '--tls-server-name=kubernetes.docker.internal']
services=json.loads(subprocess.check_output(base+['--request-timeout=10s',
    'get','--raw','/api/v1/services?labelSelector=app.kubernetes.io%2Fname%3Dsmtp4dev'],text=True))['items']
if len(services)!=1:
    raise RuntimeError('Expected exactly one shared smtp4dev service; resolve the local service binding')
service=services[0]['metadata']
os.execv(kubectl,base+['--namespace',service['namespace'],
    'port-forward','--address','127.0.0.1','service/'+service['name'],'19126:25','19158:80','9167:80'])
