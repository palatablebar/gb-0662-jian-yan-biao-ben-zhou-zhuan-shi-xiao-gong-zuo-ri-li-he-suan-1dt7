import hashlib,json
from datetime import date,datetime,timedelta,time
from zoneinfo import ZoneInfo
from decimal import Decimal
from app.errors import RuleError
ZONE=ZoneInfo('Asia/Shanghai')

def canonical(calendar):
    """Stable serialisation used for the calendar reference digest."""
    return json.dumps(calendar,sort_keys=True,ensure_ascii=False,separators=(',',':'))

def digest(calendar):
    return hashlib.sha256(canonical(calendar).encode('utf-8')).hexdigest()

def validate(calendar):
    """Reject malformed calendars before they can replace an official result."""
    if not isinstance(calendar,dict):raise RuleError('CALENDAR','日历必须是对象')
    weekdays=calendar.get('weekdays');hours=calendar.get('hours');holidays=calendar.get('holidays')
    if not isinstance(weekdays,list) or any(not isinstance(d,int) or d<0 or d>6 for d in weekdays) or len(set(weekdays))!=len(weekdays):
        raise RuleError('CALENDAR','工作日必须是0至6的不重复整数')
    if not isinstance(hours,list) or not hours:
        raise RuleError('CALENDAR','工作区间不能为空')
    for pair in hours:
        if not isinstance(pair,list) or len(pair)!=2:raise RuleError('CALENDAR','工作区间必须是起止对')
        try:a=time.fromisoformat(pair[0]);b=time.fromisoformat(pair[1])
        except (ValueError,TypeError):raise RuleError('CALENDAR','工作区间时间格式无效')
        if b<=a:raise RuleError('CALENDAR','工作区间必须同日且有序')
    if not isinstance(holidays,list):raise RuleError('CALENDAR','节假日必须是日期列表')
    seen=set()
    for value in holidays:
        if not isinstance(value,str):raise RuleError('CALENDAR','节假日必须是ISO日期')
        try:parsed=date.fromisoformat(value)
        except ValueError:raise RuleError('CALENDAR','节假日必须是ISO日期')
        if parsed.isoformat()!=value:raise RuleError('CALENDAR','节假日必须是ISO日期')
        if value in seen:raise RuleError('CALENDAR','节假日不能重复')
        seen.add(value)
    return calendar

def apply_holiday_patch(calendar,add=None,remove=None):
    """Return a validated copy with holidays added/removed, preserving all other rules."""
    add=[] if add is None else add;remove=[] if remove is None else remove
    if not isinstance(add,list) or not isinstance(remove,list):raise RuleError('CALENDAR','节假日更正必须是日期列表')
    for values,label in ((add,'停诊日期'),(remove,'恢复工作日期')):
        if len(set(values))!=len(values):raise RuleError('CALENDAR',label+'不能重复')
    overlap=set(add)&set(remove)
    if overlap:raise RuleError('CALENDAR','同一日期不能同时设为停诊和恢复工作')
    updated=dict(calendar);current=set(calendar.get('holidays',[]));current.update(add);current.difference_update(remove)
    updated['holidays']=sorted(current)
    validate(updated)
    return updated

def merge(intervals):
    out=[]
    for start,end in sorted(intervals):
        if end<=start:raise RuleError('PAUSE','暂停结束必须晚于开始')
        if out and start<=out[-1][1]:out[-1]=(out[-1][0],max(end,out[-1][1]))
        else:out.append((start,end))
    return out

def intersection(a,b):
    start=max(a[0],b[0]);end=min(a[1],b[1]);return (start,end) if start<end else None

def minutes(intervals):
    return sum((Decimal(str((b-a).total_seconds()))/60 for a,b in intervals),Decimal(0))

def windows(start,end,calendar):
    day=start.astimezone(ZONE).date();last=end.astimezone(ZONE).date();out=[]
    while day<=last:
        if day.weekday() in calendar['weekdays'] and day.isoformat() not in calendar['holidays']:
            for a,b in calendar['hours']:
                left=datetime.combine(day,time.fromisoformat(a),ZONE);right=datetime.combine(day,time.fromisoformat(b),ZONE)
                if right<=left:raise RuleError('CALENDAR','工作区间必须同日且有序')
                hit=intersection((start,end),(left,right))
                if hit:out.append(hit)
        day+=timedelta(days=1)
    return merge(out)
