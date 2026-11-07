"""Private localhost API for reading and changing the isolated guest clock."""
import hmac
import json
import logging
import os
from pathlib import Path
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

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


class CompanyNameRequest(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


@asynccontextmanager
async def lifespan(application):
    from company_environments import CompanyEnvironmentManager
    manager = CompanyEnvironmentManager(controller)
    application.state.company_environments = manager
    manager.start()
    try:
        yield
    finally:
        manager.stop()


app = FastAPI(title='Private virtual clock API', docs_url=None,
              redoc_url=None, openapi_url=None, lifespan=lifespan)


def environments():
    return app.state.company_environments


def clock_available():
    if environments().busy:
        raise HTTPException(status_code=409, detail='company_environment_changing')


def environment_operation(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except KeyError as error:
        raise HTTPException(status_code=404, detail='company_environment_not_found') from error
    except PermissionError as error:
        raise HTTPException(status_code=401, detail='company_job_token_invalid') from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail='company_environment_request_invalid') from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail='company_environment_conflict') from error


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


@app.get('/companies')
def list_companies(_authorized: None = Depends(require_bearer)):
    return environments().list()


@app.post('/companies/clone', status_code=202)
def clone_company(body: CompanyNameRequest,
                  idempotency_key: Annotated[str, StringConstraints(min_length=1)] = Header(alias='Idempotency-Key'),
                  _authorized: None = Depends(require_bearer)):
    return environment_operation(environments().submit, operation='clone', name=body.name,
                                 idempotency_key=idempotency_key)


@app.post('/companies/{identifier}/switch', status_code=202)
def switch_company(identifier: str,
                   idempotency_key: Annotated[str, StringConstraints(min_length=1)] = Header(alias='Idempotency-Key'),
                   _authorized: None = Depends(require_bearer)):
    return environment_operation(environments().submit, operation='switch', target_id=identifier,
                                 idempotency_key=idempotency_key)


@app.post('/companies/{identifier}/rename')
def rename_company(identifier: str, body: CompanyNameRequest,
                   _authorized: None = Depends(require_bearer)):
    return environment_operation(environments().rename, identifier, body.name)


@app.get('/company-switch/jobs/{identifier}')
def company_switch_job(identifier: UUID,
                       token: Annotated[str, Header(alias='X-Company-Job-Token')]):
    # The scoped read-only job token survives guest service shutdown. Mutations
    # above retain the private controller bearer and dashboard CSRF boundary.
    return environment_operation(environments().get_job, str(identifier), status_token=token)


@app.post('/company-switch/jobs/{identifier}/start', status_code=202)
def start_company_switch_job(identifier: UUID,
                             token: Annotated[str, Header(alias='X-Company-Job-Token')]):
    # Acknowledge the receipt before stopping the guest that returned it. The
    # capability cannot change the already-authorized job's company or action.
    return environment_operation(environments().start_job, str(identifier), token)


@app.get('/clock')
def get_clock(_authorized: None = Depends(require_bearer)):
    clock_available()
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
    clock_available()
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
    clock_available()
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
