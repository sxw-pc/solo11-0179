from . import storage as s
NAME='展陈状况'
def apply(c,e):
 a=e['action'];p=e.get('data',{});sid=e['subject'];loan=s.latest(c,'subjects',sid)
 if a=='inspect':
  box=s.latest(c,'allocations',p.get('box',''))
  if not box or box['subject']!=sid or box['state'] not in ('arrived','broken-arrived'):return s.decide(c,e,NAME,'blocked','no-receiving-handoff','blocked')
  baseline=c.execute("SELECT state FROM documents WHERE subject=? AND kind='baseline' ORDER BY version DESC LIMIT 1",(sid,)).fetchone()
  damaged=bool(p.get('damage')) or (baseline is not None and int(p.get('condition_score',100))<int(baseline['state']))
  status='damage' if damaged else 'sound'
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'damage' if damaged else 'inspection',status,0))
  outcome='hold' if damaged or box['state']=='broken-arrived' or not loan or loan['state']!='authorized' else 'inspected'
  return s.decide(c,e,NAME,outcome,'condition-and-custody-compared',outcome)
 if a=='environment':
  temp=int(p.get('temp',99));humidity=int(p.get('humidity',99))
  condition=c.execute("SELECT 1 FROM receipts WHERE subject=? AND kind='inspection'",(sid,)).fetchone()
  ok=loan and loan['state']=='authorized' and condition and 18<=temp<=24 and 40<=humidity<=60
  state='display' if ok else 'hold'
  if not ok and loan and loan['state']=='authorized' and condition and not (18<=temp<=24 and 40<=humidity<=60):
   c.execute("UPDATE subjects SET state='paused',version=version+1 WHERE id=?",(sid,))
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'environment',state,0))
  return s.decide(c,e,NAME,state,'contract-condition-climate-gate',state)
 if a=='baseline':
  if not loan or loan['state']!='authorized':return s.decide(c,e,NAME,'blocked','no-contract-baseline','blocked')
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],sid,'baseline',loan['version'],str(int(p['condition_score']))))
  return s.decide(c,e,NAME,'baseline-recorded','departure-condition-frozen','baseline-recorded')
 if a=='return':
  damaged=c.execute("SELECT 1 FROM receipts WHERE subject=? AND kind='damage'",(sid,)).fetchone()
  result='claim' if damaged else 'returned'
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'return',result,0))
  return s.decide(c,e,NAME,result,'return-condition-compared',result)
 if a=='display':
  start,end=int(p.get('start',0)),int(p.get('end',0))
  if end<start:return s.decide(c,e,NAME,'blocked','invalid-segment','blocked')
  c.execute('INSERT INTO display_segments(subject,seg_start,seg_end,source) VALUES(?,?,?,?)',(sid,start,end,e['id']))
  return s.decide(c,e,NAME,'display-segment','raw-display-period-recorded','display-segment')
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')

def display_windows(c,sid):
 """原始展出时段(展厅日志采录);只用于限定可计数的时空窗,不改变展陈状态。"""
 return [(r['seg_start'],r['seg_end']) for r in c.execute('SELECT seg_start,seg_end FROM display_segments WHERE subject=? ORDER BY seg_start',(sid,))]
