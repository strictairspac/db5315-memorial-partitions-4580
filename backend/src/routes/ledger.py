from fastapi import APIRouter, Depends

from src.schemas.ledger import IngestForm, MigrationForm, MonthForm
from src.services import ledger
from src.services.security import current_user, operator

router = APIRouter()


@router.get('/ledger/overview')
def ledger_overview(_=Depends(current_user)):
    return ledger.overview()


@router.get('/ledger/slips')
def ledger_slips(date_from: str, date_to: str, _=Depends(current_user)):
    return ledger.query_slips(date_from, date_to)


@router.post('/ledger/ingest', status_code=201)
def ledger_ingest(form: IngestForm, actor=Depends(operator)):
    return {'acks': ledger.ingest(form.records, actor)}


@router.post('/ledger/months', status_code=201)
def ledger_register_month(form: MonthForm, actor=Depends(operator)):
    return ledger.register_month(form.month, actor)


@router.post('/ledger/migrations', status_code=202)
def ledger_split(form: MigrationForm, actor=Depends(operator)):
    return ledger.start_migration(form.month, actor)
