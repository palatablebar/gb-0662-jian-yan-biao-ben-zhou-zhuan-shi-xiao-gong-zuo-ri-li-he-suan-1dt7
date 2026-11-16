from app import store
from app.errors import RuleError
from app.modules.batches.review import check

def persist(bundle,raw=None):return store.save(bundle,raw)
def approve(rid,version):
    bundle=store.get(rid)
    if not bundle:raise RuleError('NOT_FOUND','当前批次不存在',404)
    if bundle['status']!='official':raise RuleError('STALE','该批复已被更正，请复核当前正式结论',409)
    check(bundle);return store.review(rid,version)
