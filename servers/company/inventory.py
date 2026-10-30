"""Read-only adoption inventory for the seeded company; never changes accounts."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import quote

import requests


def inventory(roster, chat, storage, storage_auth):
    users=chat._request('GET','users.list',params={'count':1000})['users']
    response=requests.get(storage.rstrip('/')+'/ocs/v1.php/cloud/users',
        auth=storage_auth,headers={'OCS-APIRequest':'true','Host':'the-agent-company.com:8092'},
        params={'format':'json'},timeout=20)
    response.raise_for_status()
    payload=response.json()['ocs']
    if int(payload['meta']['statuscode']) != 100:
        raise RuntimeError('ownCloud provisioning discovery rejected')
    storage_ids=set(payload['data']['users'])
    employees=[]
    for display_name, mapping in roster.items():
        username=mapping['username']
        matches=[u for u in users if u.get('username')==username]
        if len(matches)!=1:
            employees.append({'id':username.replace('_','-'),'displayName':display_name,
                'resolution':'ambiguous' if matches else 'missing','service':'rocketchat'})
            continue
        user=matches[0]
        addresses=[e['address'] for e in user.get('emails',[])]
        if len(addresses)!=1:
            raise RuntimeError('Seed employee must have one explicit primary chat email: '+username)
        employees.append({'id':username.replace('_','-'),'displayName':display_name,
            'email':addresses[0], 'accounts':{'rocketchat':{'id':user['_id'],'username':username},
                'owncloud':{'id':username,'username':username} if username in storage_ids else None,
                'mail':{'address':addresses[0]}},
            'unresolved':[] if username in storage_ids else ['No seeded individual ownCloud account']})
    return {'schemaVersion':1,'employees':employees,'storageAccountIds':sorted(storage_ids)}



def plane_members(base_url, slug, email, password):
    """Use the installed Plane app's native CSRF/session authentication for discovery."""
    session=requests.Session()
    response=session.get(base_url.rstrip('/')+'/auth/get-csrf-token/',timeout=20)
    response.raise_for_status()
    token=response.json()['csrf_token']
    response=session.post(base_url.rstrip('/')+'/auth/sign-in/',
        data={'email':email,'password':password,'csrfmiddlewaretoken':token},
        headers={'X-CSRFToken':token,'Origin':base_url,'Referer':base_url+'/'},
        allow_redirects=False,timeout=20)
    response.raise_for_status()
    response=session.get(base_url.rstrip('/')+f'/api/workspaces/{quote(slug,safe="")}/members/',timeout=20)
    response.raise_for_status()
    return response.json()


def link_plane(employees, members, expected_emails):
    """Correlate only the explicit seed email mapping, retaining account and membership IDs."""
    for employee in employees:
        expected=expected_emails.get(employee['id'])
        if expected is None:
            employee.setdefault('unresolved',[]).append('Plane mapping not configured')
            continue
        matches=[member for member in members if member['member']['email'].casefold()==expected.casefold()]
        if len(matches)!=1:
            employee['unresolved'].append('Plane mapping missing or ambiguous')
            continue
        membership=matches[0];account=membership['member']
        employee['accounts']['plane']={'id':account['id'],'email':account['email'],
            'membership_id':membership['id'],'role':membership['role']}


if __name__=='__main__':
    from servers.experiments.runner import RocketChatClient
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roster',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    # Only the explicit mapping is read; credential material never enters the result.
    raw=json.loads(args.roster.read_text())
    roster={name:{'username':value['username']} for name,value in raw.items()}
    chat=RocketChatClient.from_env();chat.login()
    result=inventory(roster,chat,os.environ['OWNCLOUD_BASE_URL'],
        (os.environ['OWNCLOUD_USERNAME'],os.environ['OWNCLOUD_PASSWORD']))
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print('Recorded',len(result['employees']),'employees; no service accounts changed.')
