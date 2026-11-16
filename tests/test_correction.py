import pytest
from copy import deepcopy
from fastapi.testclient import TestClient
from app import store
from app.main import app
from app.errors import RuleError
from app.config import SAMPLES
from app.modules.batches import correction as correction_mod
from app.modules.batches.correction import compare
from app.modules.model.calendar import apply_holiday_patch,digest


def base_id():return store.official()['manifest']['run_id']


def correct(client,add=None,remove=None,note='',rid=None,version=None):
    bundle=store.official();rid=rid or bundle['manifest']['run_id'];version=version if version is not None else bundle['version']
    return client.post('/api/batches/correct',json={'run_id':rid,'version':version,'add':add,'remove':remove,'note':note})


def test_new_official_and_old_preserved(client):
    old=store.official();r=correct(client,add=['2026-11-04']);assert r.status_code==200
    new=store.official();assert new['manifest']['run_id']!=old['manifest']['run_id'];assert new['status']=='official'
    kept=store.get(old['manifest']['run_id']);assert kept['status']=='superseded';assert kept['superseded_by']==new['manifest']['run_id']
    assert kept['calculate']['rows']==old['calculate']['rows']
    assert len(store.history())==2


def test_review_page_compares_before_after(client):
    correct(client,add=['2026-11-04'],note='11月4日临时停诊')
    page=client.get('/batches');assert page.status_code==200
    text=page.text
    assert '更正对照' in text and '净分钟（前 → 后）' in text and '来源（前 → 后）' in text and '11月4日临时停诊' in text
    changed=store.corrections()[0]
    old=store.get(changed['base_id']);new=store.get(changed['new_id'])
    d=compare(old,new)
    row=next(x for x in d['rows'] if x['specimen']=='000006')
    assert row['before_minutes']=='90.0' and row['after_minutes']=='30.0'
    assert row['before_source'].startswith(old['manifest']['run_id'])
    assert row['after_source'].startswith(new['manifest']['run_id'])


def test_idempotent_retry_adds_no_duplicate(client):
    old=store.official();first=correct(client,add=['2026-11-04']);second=correct(client,add=['2026-11-04'])
    assert first.status_code==second.status_code==200
    assert first.json()['run_id']==second.json()['run_id']
    assert second.json()['replayed'] is True
    assert len(store.history())==2 and len(store.corrections())==1
    assert len(store.history())==len({h['id'] for h in store.history()})
    assert store.get(old['manifest']['run_id'])['status']=='superseded'


def test_superseded_base_rejected(client):
    old=store.official();assert correct(client,add=['2026-11-04']).status_code==200
    r=correct(client,add=['2026-11-05'],rid=old['manifest']['run_id'],version=old['version'])
    assert r.status_code==409;assert r.json()['code']=='STALE'


def test_stale_version_rejected(client):
    old=store.official();r=correct(client,add=['2026-11-04'],version=old['version']+1);assert r.status_code==409
    assert store.official()['manifest']['run_id']==old['manifest']['run_id']


def test_midway_failure_keeps_previous_official(client,monkeypatch):
    old=store.official();before=len(store.history())
    def boom(*a,**k):raise RuleError('CALENDAR','模拟更正失败')
    monkeypatch.setattr(correction_mod,'execute',boom)
    r=correct(client,add=['2026-11-04']);assert r.status_code==422
    assert store.official()['manifest']['run_id']==old['manifest']['run_id']
    assert store.get(old['manifest']['run_id'])['status']=='official'
    assert len(store.history())==before and store.corrections()==[]


def test_transaction_failure_rolls_back(client,monkeypatch):
    old=store.official();before=len(store.history())
    def boom(*a,**k):raise RuntimeError('db down')
    monkeypatch.setattr(store,'_record_correction',boom)
    with pytest.raises(RuntimeError):
        store.apply_correction(old['manifest']['run_id'],{'manifest':{'run_id':'newid','created_at':'x'}},'k',{},'',b'x')
    assert len(store.history())==before
    assert store.official()['manifest']['run_id']==old['manifest']['run_id']
    assert store.get(old['manifest']['run_id'])['status']=='official'
    assert store.corrections()==[]


def test_missing_raw_blocks_replacement(client,monkeypatch):
    old=store.official();monkeypatch.setattr(store,'raw',lambda rid:None)
    r=correct(client,add=['2026-11-04']);assert r.status_code==409;assert r.json()['code']=='RAW_MISSING'
    assert store.official()['manifest']['run_id']==old['manifest']['run_id']


def test_evidence_preserved_after_correction(client):
    old=store.official();correct(client,add=['2026-11-04']);new=store.official()
    assert store.raw(new['manifest']['run_id'])==store.raw(old['manifest']['run_id'])
    assert new['manifest']['sha256']==old['manifest']['sha256']
    old_evidence={(r['line'],r['code']) for r in old['quarantine']['rows']}
    new_evidence={(r['line'],r['code']) for r in new['quarantine']['rows']}
    assert old_evidence<=new_evidence
    assert len(store.raw(old['manifest']['run_id']))>0


def test_invalid_patch_rejected(client):
    old=store.official()
    assert correct(client,add=['2026-11-05'],remove=['2026-11-05']).status_code==422
    assert correct(client,add=['11/05']).status_code==422
    assert correct(client,add=[],remove=[]).status_code==422
    assert store.official()['manifest']['run_id']==old['manifest']['run_id']
    assert store.corrections()==[]


def test_corrected_calendar_survives_rerun(client):
    old=store.official();correct(client,add=['2026-11-05'])
    new=store.official()
    stored,_=store.active_calendar();assert '2026-11-05' in stored['holidays']
    rerun=client.post('/api/runs',json={'sample':'dirty'}).json()
    assert rerun['model']['calendar']['holidays']==new['model']['calendar']['holidays']
    assert rerun['calculate']['late']==new['calculate']['late']
    assert store.get(old['manifest']['run_id'])['status']=='superseded'


def test_holiday_patch_validation():
    cal={'weekdays':[0,1,2,3,4],'hours':[['09:00','12:00']],'holidays':['2026-11-03']}
    assert apply_holiday_patch(cal,add=['2026-11-05'])['holidays']==['2026-11-03','2026-11-05']
    assert apply_holiday_patch(cal,remove=['2026-11-03'])['holidays']==[]
    assert apply_holiday_patch(cal,add=['2026-11-05'])['hours']==cal['hours']
    with pytest.raises(RuleError):apply_holiday_patch(cal,add=['2026-11-03','2026-11-03'])
    with pytest.raises(RuleError):apply_holiday_patch(cal,add=['2026-11-03'],remove=['2026-11-03'])


def test_calendar_digest_is_canonical():
    a={'weekdays':[0,1],'hours':[['09:00','17:00']],'holidays':['2026-11-03']}
    b={'holidays':['2026-11-03'],'hours':[['09:00','17:00']],'weekdays':[0,1]}
    assert digest(a)==digest(b)


def test_noop_patch_adds_nothing(client):
    old=store.official();r=correct(client,add=['2026-11-03'])
    assert r.status_code==200;assert r.json()['replayed'] is True
    assert r.json()['run_id']==old['manifest']['run_id']
    assert len(store.history())==1 and store.corrections()==[]


def test_review_of_superseded_rejected(client):
    old=store.official();assert correct(client,add=['2026-11-05']).status_code==200
    r=client.post('/api/batches/review',json={'run_id':old['manifest']['run_id'],'version':old['version']})
    assert r.status_code==409;assert r.json()['code']=='STALE'


def test_chained_corrections_keep_every_generation(client):
    first=store.official();correct(client,add=['2026-11-05'])
    second=store.official();correct(client,add=['2026-11-04'])
    third=store.official()
    assert len(store.history())==3 and len(store.corrections())==2
    assert store.get(first['manifest']['run_id'])['superseded_by']==second['manifest']['run_id']
    assert store.get(second['manifest']['run_id'])['superseded_by']==third['manifest']['run_id']
    assert store.get(first['manifest']['run_id'])['calculate']['late']==first['calculate']['late']
    assert store.raw(third['manifest']['run_id'])==store.raw(first['manifest']['run_id'])


def test_persisted_input_supports_correction_after_reopen(client):
    old=store.official()
    store.connect().close()
    reopened=store.get(old['manifest']['run_id'])
    assert store.raw(reopened['manifest']['run_id']) is not None
    r=correct(client,add=['2026-11-05'],rid=reopened['manifest']['run_id'],version=reopened['version'])
    assert r.status_code==200;assert store.official()['manifest']['run_id']==r.json()['run_id']


def test_correcting_back_restores_original_official(client):
    original=store.official();oid=original['manifest']['run_id']
    assert correct(client,add=['2026-11-05']).status_code==200
    mid=store.official()
    r=correct(client,remove=['2026-11-05'],rid=mid['manifest']['run_id'],version=mid['version'])
    assert r.status_code==200;assert r.json()['run_id']==oid
    restored=store.official();assert restored['manifest']['run_id']==oid and restored['status']=='official'
    assert restored['calculate']['rows']==original['calculate']['rows']
    assert store.get(mid['manifest']['run_id'])['status']=='superseded'
    assert len(store.corrections())==2
    # audit: the original is official again but the intermediate stays visible as superseded
    assert {h['id'] for h in store.history()}=={oid,mid['manifest']['run_id']}


def test_rerun_of_same_input_does_not_duplicate_history(client):
    old=store.official();correct(client,add=['2026-11-05'])
    before=len(store.history())
    again=client.post('/api/runs',json={'sample':'dirty'}).json()
    assert again['manifest']['run_id']==store.official()['manifest']['run_id']
    assert len(store.history())==before
    assert len(store.corrections())==1


def test_raw_input_persisted_for_every_batch(client):
    old=store.official();assert store.raw(old['manifest']['run_id'])==(SAMPLES/'dirty.csv').read_bytes()
    correct(client,add=['2026-11-05']);new=store.official()
    assert store.raw(new['manifest']['run_id'])==store.raw(old['manifest']['run_id'])


def test_compare_source_includes_calendar_digest(client):
    old=store.official();correct(client,add=['2026-11-04']);new=store.official()
    from app.modules.batches.correction import compare
    d=compare(old,new);row=d['rows'][0]
    assert old['manifest']['references']['calendar'][:10] in row['before_source']
    assert new['manifest']['references']['calendar'][:10] in row['after_source']
    assert old['manifest']['references']['calendar']!=new['manifest']['references']['calendar']


def test_compare_only_reports_changed_specimens(client):
    old=store.official();correct(client,add=['2026-11-04']);new=store.official()
    from app.modules.batches.correction import compare
    d=compare(old,new)
    assert all(r['before_minutes']!=r['after_minutes'] or r['before_state']!=r['after_state'] or r['before_overdue']!=r['after_overdue'] for r in d['rows'])
    unchanged=[s for s,r in {x['specimen']:x for x in new['calculate']['rows']}.items()]
    assert len(new['calculate']['rows'])>len(d['rows'])


def test_evidence_loss_cancels_correction(client,monkeypatch):
    old=store.official()
    real=correction_mod.execute
    def lossy(raw,grace,calendar):
        bundle=real(raw,grace,calendar)
        bundle['quarantine']['rows']=bundle['quarantine']['rows'][1:]
        return bundle
    monkeypatch.setattr(correction_mod,'execute',lossy)
    r=correct(client,add=['2026-11-05'])
    assert r.status_code==409;assert r.json()['code']=='EVIDENCE_LOST'
    assert store.official()['manifest']['run_id']==old['manifest']['run_id']
    assert store.get(old['manifest']['run_id'])['status']=='official'
    assert store.corrections()==[] and store.raw(old['manifest']['run_id']) is not None


def test_comma_separated_dates_accepted(client):
    r=correct(client,add='2026-11-05, 2026-11-06')
    assert r.status_code==200
    holidays=store.official()['model']['calendar']['holidays']
    assert '2026-11-05' in holidays and '2026-11-06' in holidays
    assert holidays==sorted(holidays)


def test_backfill_legacy_raw_only_on_digest_match(client):
    old=store.official()
    # simulate a legacy database written before raw bytes were stored
    con=store.connect();con.execute('UPDATE runs SET raw=NULL WHERE id=?',(old['manifest']['run_id'],));con.commit();con.close()
    assert store.raw(old['manifest']['run_id']) is None
    store.backfill_raw([b'not the right bytes',(SAMPLES/'dirty.csv').read_bytes()])
    assert store.raw(old['manifest']['run_id'])==(SAMPLES/'dirty.csv').read_bytes()
    con=store.connect();con.execute('UPDATE runs SET raw=NULL WHERE id=?',(old['manifest']['run_id'],));con.commit();con.close()
    store.backfill_raw([b'wrong bytes only'])
    assert store.raw(old['manifest']['run_id']) is None


def test_compare_reports_specimen_leaving_calculation(client):
    old=store.official();correct(client,add=['2026-11-05']);new=store.official()
    from app.modules.batches.correction import compare
    fake_new=deepcopy(new);fake_new['calculate']['rows']=[r for r in fake_new['calculate']['rows'] if r['specimen']!='000006']
    fake_new['quarantine']['rows']=fake_new['quarantine']['rows']+[{'line':99,'code':'TIME','raw':{'specimen':'000006'}}]
    d=compare(old,fake_new)
    row=next(r for r in d['rows'] if r['specimen']=='000006')
    assert row['after_minutes']=='—';assert row['after_state']=='已隔离'
    fake_new['quarantine']['rows']=fake_new['quarantine']['rows'][:-1]
    d=compare(old,fake_new);row=next(r for r in d['rows'] if r['specimen']=='000006')
    assert row['after_state']=='不再计入'
