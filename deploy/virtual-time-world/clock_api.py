"""Private localhost API for reading and changing the isolated guest clock."""
import hmac
import json
import logging
import os
from pathlib import Path
import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, Field, StrictInt, StringConstraints

from clock_control import Controller, DEFAULT_NAME, DEFAULT_PORT, DEFAULT_ROOT

LOG = logging.getLogger('virtual_time_world.clock_api')
BEARER = HTTPBearer(auto_error=False)
controller = Controller(
    Path(os.environ.get('VIRTUAL_TIME_ROOT', str(DEFAULT_ROOT))),
    os.environ.get('VIRTUAL_TIME_DOMAIN', DEFAULT_NAME),
    int(os.environ.get('VIRTUAL_TIME_SSH_PORT', str(DEFAULT_PORT))),
)


class SpeedRequest(BaseModel):
    speed: StrictInt = Field(gt=0)


class IssueDeadlineRequest(BaseModel):
    issue_key: str = Field(pattern=r'^[0-9a-f]{64}$')
    deadline_at: AwareDatetime


app = FastAPI(title='Private virtual clock API', docs_url=None,
              redoc_url=None, openapi_url=None)


def expected_token():
    direct = os.environ.get('VIRTUAL_TIME_API_TOKEN')
    if direct:
        return direct
    token_path = os.environ.get(
        'VIRTUAL_TIME_API_TOKEN_FILE',
        str(controller.config / 'clock-api-token'))
    token_file = Path(token_path)
    details = token_file.stat()
    if details.st_mode & 0o077:
        raise PermissionError('clock API token file must be private to its owner')
    token = token_file.read_text(encoding='utf-8').strip()
    if not token:
        raise ValueError('clock API token file is empty')
    return token


def require_bearer(credentials: HTTPAuthorizationCredentials | None = Depends(BEARER)):
    try:
        token = expected_token()
    except Exception as error:
        LOG.exception('Clock API bearer token is unavailable')
        raise HTTPException(status_code=503,
                            detail='Clock API authentication is unavailable') from error
    if (credentials is None or credentials.scheme.lower() != 'bearer' or
            not hmac.compare_digest(credentials.credentials.encode('utf-8'),
                                    token.encode('utf-8'))):
        raise HTTPException(status_code=401, detail='Invalid bearer token',
                            headers={'WWW-Authenticate': 'Bearer'})


@app.get('/clock')
def get_clock(_authorized: None = Depends(require_bearer)):
    try:
        with controller.operation_lock():
            return controller.clock_snapshot()
    except Exception as error:
        LOG.exception('Clock snapshot failed')
        raise HTTPException(status_code=503,
                            detail='Clock state is temporarily unavailable') from error


@app.get('/clock/real-time')
def get_real_time(_authorized: None = Depends(require_bearer)):
    return {'epoch_ns': time.time_ns()}


@app.post('/issue-deadlines')
def register_issue_deadline(request: IssueDeadlineRequest,
                            _authorized: None = Depends(require_bearer)):
    deadline = request.deadline_at.astimezone(timezone.utc).isoformat()
    try:
        with controller.operation_lock():
            controller.register_issue_deadline(request.issue_key, deadline)
        return {'issue_key': request.issue_key, 'deadline_at': deadline}
    except Exception as error:
        LOG.exception('Issue deadline registration failed')
        raise HTTPException(status_code=503,
                            detail='Issue deadline registration failed') from error


@app.post('/clock/speed')
def set_clock_speed(
    request: SpeedRequest,
    _authorized: None = Depends(require_bearer),
    idempotency_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = Header(alias='Idempotency-Key'),
):
    with controller.operation_lock():
        try:
            with controller.state() as db:
                existing = db.execute(
                    '''SELECT requested_speed, status, response_json
                       FROM clock_api_requests WHERE idempotency_key=?''',
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    old_speed, status, response = existing
                    if old_speed != request.speed:
                        raise HTTPException(status_code=409,
                                            detail='Idempotency-Key was used with a different speed')
                    if status == 'complete':
                        return json.loads(response)
                    raise HTTPException(status_code=503,
                                        detail='Clock update outcome is unresolved; it will not be repeated')
            # Require an active guest and a reconciled anchor before reserving the key.
            controller.clock_snapshot()
            with controller.state() as db:
                db.execute(
                    '''INSERT INTO clock_api_requests(
                           idempotency_key, requested_speed, status, response_json, created_ns)
                       VALUES(?, ?, 'processing', NULL, ?)''',
                    (idempotency_key, request.speed, time.time_ns()),
                )
                db.commit()
        except HTTPException:
            raise
        except Exception as error:
            LOG.exception('Clock update could not be reserved')
            raise HTTPException(status_code=503,
                                detail='Clock update could not be started') from error

        try:
            controller.set_rate(request.speed)
            result = {'speed': request.speed}
            with controller.state() as db:
                db.execute(
                    '''UPDATE clock_api_requests
                       SET status='complete', response_json=? WHERE idempotency_key=?''',
                    (json.dumps(result, separators=(',', ':')), idempotency_key),
                )
                db.commit()
            return result
        except Exception as error:
            try:
                with controller.state() as db:
                    db.execute(
                        '''UPDATE clock_api_requests SET status='uncertain'
                           WHERE idempotency_key=?''', (idempotency_key,))
                    db.commit()
            except Exception:
                LOG.exception('Clock update receipt could not be marked uncertain')
            LOG.exception('Clock speed update failed')
            raise HTTPException(status_code=503,
                                detail='Clock update outcome is uncertain') from error
