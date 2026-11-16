from fastapi import APIRouter,Request,Body
from app.web import page
from app import store
router=APIRouter()
@router.get('/batches')
def view(request:Request):return page(request,'batches')
@router.get('/api/batches')
def result():return store.history()
@router.post('/api/batches/review')
def review(payload:dict=Body(...)):
    from app.modules.batches.service import approve
    if not isinstance(payload.get('version'),int):
        from app.errors import RuleError
        raise RuleError('VERSION','版本必须为整数')
    return approve(payload.get('run_id',''),payload['version'])
@router.post('/api/batches/correct')
def correct(payload:dict=Body(...)):
    from app.modules.batches.correction import correct as apply
    if not isinstance(payload.get('version'),int):
        from app.errors import RuleError
        raise RuleError('VERSION','版本必须为整数')
    bundle,replayed=apply(payload.get('run_id',''),payload['version'],payload.get('add'),payload.get('remove'),payload.get('note',''))
    return {'run_id':bundle['manifest']['run_id'],'base_run_id':payload.get('run_id',''),'status':bundle['status'],'version':bundle['version'],'replayed':replayed,'late':bundle['calculate']['late'],'total':bundle['calculate']['total']}
@router.get('/api/batches/corrections')
def corrections():return store.corrections()
