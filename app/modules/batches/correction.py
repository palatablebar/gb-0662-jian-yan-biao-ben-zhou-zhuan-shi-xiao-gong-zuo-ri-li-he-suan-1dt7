import hashlib,json
from app.config import RULE_VERSION
from app.errors import RuleError
from app.pipeline import execute
from app.modules.model.calendar import apply_holiday_patch,digest as calendar_digest
from app.modules.batches.review import check
from app import store


def _dates(values,label):
    values=[] if values is None else values
    if isinstance(values,str):values=[v.strip() for v in values.split(',') if v.strip()]
    if not isinstance(values,list) or any(not isinstance(v,str) for v in values):
        raise RuleError('CALENDAR',label+'必须是日期列表')
    values=[v.strip() for v in values]
    if any(not v for v in values):raise RuleError('CALENDAR',label+'不能为空')
    if len(set(values))!=len(values):raise RuleError('CALENDAR',label+'不能重复')
    return sorted(values)


def _key(base,add,remove):
    canonical=json.dumps({'base':base['manifest']['run_id'],'add':add,'remove':remove,'input':base['manifest']['sha256'],'rule':RULE_VERSION},sort_keys=True,ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _evidence(bundle):
    return {(r['line'],r['code'],json.dumps(r['raw'],sort_keys=True,ensure_ascii=False)) for r in bundle['quarantine']['rows']}


def correct(base_id,version,add=None,remove=None,note=''):
    """Publish a corrected official conclusion, superseding but never deleting the old one.

    Retrying the same base + patch returns the already-published result instead of
    creating a duplicate. Any failure before the store transaction commits leaves the
    previous official bundle and its raw evidence untouched.
    """
    add=_dates(add,'停诊日期');remove=_dates(remove,'恢复工作日期')
    if not add and not remove:raise RuleError('CALENDAR','更正必须新增停诊或恢复工作日')
    if not isinstance(note,str):raise RuleError('PARAMETER','更正说明必须是文本')
    note=note.strip()
    if len(note)>200:raise RuleError('PARAMETER','更正说明最多200字')
    base=store.get(base_id)
    if not base:raise RuleError('NOT_FOUND','当前批次不存在',404)
    key=_key(base,add,remove)
    existing=store.correction(key)
    if existing:return store.get(existing['new_id']),True
    if base['status']!='official':raise RuleError('STALE','该批复已被更正，请刷新后复核',409)
    current=store.official()
    if not current or current['manifest']['run_id']!=base_id:raise RuleError('STALE','存在更新的正式结论，请刷新后复核',409)
    if not isinstance(version,int) or base['version']!=version:raise RuleError('STALE','记录已变更，请刷新后复核',409)
    raw=store.raw(base_id)
    if raw is None:raise RuleError('RAW_MISSING','原始标本缺失，不能替换正式结论',409)
    calendar=apply_holiday_patch(base['model']['calendar'],add,remove)
    bundle=execute(raw,base['manifest']['parameters']['grace'],calendar)
    if bundle['manifest']['run_id']==base_id:return base,True
    check(bundle)
    if bundle['manifest']['sha256']!=base['manifest']['sha256']:raise RuleError('RAW_MISSING','原始标本摘要不一致，已取消更正',409)
    if bundle['intake']['input']!=base['intake']['input']:raise RuleError('EVIDENCE_LOST','原始标本行数不一致，已取消更正',409)
    missing=_evidence(base)-_evidence(bundle)
    if missing:raise RuleError('EVIDENCE_LOST','已确认证据缺失，已取消更正',409)
    patch={'add':add,'remove':remove,'calendar_before':calendar_digest(base['model']['calendar']),'calendar_after':calendar_digest(calendar)}
    return store.apply_correction(base_id,bundle,key,patch,note,raw,version,calendar)


def compare(base,new):
    """Per-specimen before/after values for the review page."""
    before={r['specimen']:r for r in base['calculate']['rows']}
    after={r['specimen']:r for r in new['calculate']['rows']}
    old_cal=base['manifest']['references']['calendar'];new_cal=new['manifest']['references']['calendar']
    base_source=base['manifest']['run_id']+' · 日历 '+old_cal[:10]
    new_source=new['manifest']['run_id']+' · 日历 '+new_cal[:10]
    isolated={r['raw'].get('specimen') for r in new['quarantine']['rows'] if isinstance(r.get('raw'),dict)}
    rows=[]
    for specimen in list(after)+[s for s in before if s not in after]:
        new_row=after.get(specimen);old=before.get(specimen)
        if new_row is None:
            rows.append({'specimen':specimen,'department':old['department'],'clock':old['clock'],
                'before_minutes':old['elapsed_minutes'],'after_minutes':'—',
                'before_overdue':old['overdue_minutes'],'after_overdue':'—',
                'before_state':old['state'],'after_state':'已隔离' if specimen in isolated else '不再计入',
                'before_source':base_source,'after_source':new_source})
            continue
        if old is None:
            rows.append({'specimen':specimen,'department':new_row['department'],'clock':new_row['clock'],
                'before_minutes':'—','after_minutes':new_row['elapsed_minutes'],
                'before_overdue':'—','after_overdue':new_row['overdue_minutes'],
                'before_state':'未计入','after_state':new_row['state'],
                'before_source':base_source,'after_source':new_source})
            continue
        if old['elapsed_minutes']==new_row['elapsed_minutes'] and old['state']==new_row['state'] and old['overdue_minutes']==new_row['overdue_minutes']:continue
        rows.append({
            'specimen':specimen,'department':new_row['department'],'clock':new_row['clock'],
            'before_minutes':old['elapsed_minutes'],'after_minutes':new_row['elapsed_minutes'],
            'before_overdue':old['overdue_minutes'],'after_overdue':new_row['overdue_minutes'],
            'before_state':old['state'],'after_state':new_row['state'],
            'before_source':base_source,'after_source':new_source,
        })
    return {'rows':rows,'before_late':base['calculate']['late'],'after_late':new['calculate']['late'],'before_total':base['calculate']['total'],'after_total':new['calculate']['total']}


def corrections_for_page():
    out=[]
    for entry in store.corrections():
        base=store.get(entry['base_id']);new=store.get(entry['new_id'])
        if not base or not new:continue
        out.append({'key':entry['key'],'base_id':entry['base_id'],'new_id':entry['new_id'],'created':entry['created'],'note':entry['note'],'patch':entry['patch'],'base':base,'new':new,'diff':compare(base,new)})
    return out
