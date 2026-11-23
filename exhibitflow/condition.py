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
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'damage' if damaged else 'inspection',status,s.money(p.get('repair_quote',0)) if damaged else 0))
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
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')
