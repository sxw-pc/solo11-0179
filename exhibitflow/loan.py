from . import storage as s
NAME='借展契约'
def apply(c,e):
 a=e['action'];p=e.get('data',{});sid=e['subject']; old=s.latest(c,'subjects',sid)
 if a=='authorize':
  begin,end=int(p['start']),int(p['end'])
  if end<begin:return s.decide(c,e,NAME,'blocked','invalid-period','blocked')
  revoked=c.execute("SELECT MAX(version) v FROM documents WHERE subject=? AND kind='permit-revocation'",(sid,)).fetchone()['v'] or 0
  periods=c.execute("SELECT state FROM documents WHERE subject=? AND kind='permit' AND version>?",(sid,revoked)).fetchall()
  if any(not (end<int(x['state'].split(':')[0]) or begin>int(x['state'].split(':')[1])) for x in periods):return s.decide(c,e,NAME,'blocked','overlapping-loan','blocked')
  version=old['version']+1 if old else int(p.get('version',1))
  c.execute('INSERT OR REPLACE INTO subjects VALUES(?,?,?,?)',(sid,'artifact','authorized',version))
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],sid,'permit',version,f'{begin}:{end}'))
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id']+'-parties',sid,'parties',version,f'{p.get("lender","museum-A")}:{p.get("borrower","museum-B")}'))
  return s.decide(c,e,NAME,'authorized','period-and-object-approved','authorized')
 if a=='revoke':
  if not old:return s.decide(c,e,NAME,'blocked','no-loan','blocked')
  c.execute("UPDATE subjects SET state='revoked',version=version+1 WHERE id=?",(sid,))
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],sid,'permit-revocation',old['version']+1,'all-prior-permits'))
  c.execute("UPDATE allocations SET state='intercept' WHERE subject=? AND state IN ('sealed','in-transit')",(sid,))
  return s.decide(c,e,NAME,'revoked','in-transit-intercept-required','revoked')
 if a=='extend':
  if not old or old['state'] not in ('authorized','paused') or int(p.get('end',0))<=e['effective_at']:return s.decide(c,e,NAME,'blocked','invalid-extension','blocked')
  version=old['version']+1
  c.execute("UPDATE subjects SET state='authorized',version=? WHERE id=?",(version,sid))
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],sid,'permit',version,f'{e["effective_at"]}:{p["end"]}'))
  return s.decide(c,e,NAME,'extended','exhibition-term-renegotiated','authorized')
 if a=='liability':
  damaged=c.execute("SELECT 1 FROM receipts WHERE subject=? AND kind='damage'",(sid,)).fetchone()
  broken=c.execute("SELECT 1 FROM events WHERE subject=? AND subsystem='transit' AND action='break'",(sid,)).fetchone()
  cover=c.execute("SELECT state FROM documents WHERE subject=? AND kind='insurance' ORDER BY version DESC LIMIT 1",(sid,)).fetchone()
  covered=bool(cover and int(cover['state'].split(':')[0])<=e['effective_at']<=int(cover['state'].split(':')[1]))
  result='insured-carrier-review' if damaged and broken and covered else 'carrier-review' if damaged and broken else 'lender-review' if damaged else 'clear'
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'liability',result,0))
  return s.decide(c,e,NAME,result,'seal-and-condition-evidence-compared',result)
 if a=='insurance':
  if not old or old['state']!='authorized' or int(p.get('end',0))<int(p.get('start',0)):return s.decide(c,e,NAME,'blocked','invalid-coverage','blocked')
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],sid,'insurance',old['version'],f'{p["start"]}:{p["end"]}'))
  return s.decide(c,e,NAME,'covered','insurance-window-bound-to-loan','covered')
 if a=='approve_reseal':
  broken=s.latest(c,'allocations',p.get('box',''))
  if not broken or broken['subject']!=sid or broken['state']!='broken':return s.decide(c,e,NAME,'blocked','no-broken-box','blocked')
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],sid,'reseal-approval',old['version'] if old else 0,broken['id']))
  return s.decide(c,e,NAME,'approved','broken-seal-remains-in-history','approved')
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')
