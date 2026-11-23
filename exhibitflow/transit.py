from . import storage as s
from . import customs
NAME='包装护运'

def _customs_gate(c,e,sid,p):
    """Per-item certificate gate for sealing and custody handoff.

    A trip-level release QR is not per-item clearance: seal/handoff must be
    backed by a ``confirmed`` certificate for this exact artifact.  Artifacts
    that the customs sheet does not name are unaffected.
    """
    return customs.release_check(c,sid,
                                 trip=p.get('trip') or p.get('qr'),
                                 box=p.get('box'),seal=p.get('seal') or p.get('new_seal'),
                                 document=p.get('customs_document'))

def apply(c,e):
 a=e['action'];p=e.get('data',{});sid=e['subject']; loan=s.latest(c,'subjects',sid)
 if a=='seal':
  if not loan or loan['state']!='authorized':return s.decide(c,e,NAME,'blocked','contract-not-authorized','blocked')
  permits=c.execute("SELECT state FROM documents WHERE subject=? AND kind='permit'",(sid,)).fetchall()
  if not any(int(x['state'].split(':')[0])<=e['effective_at']<=int(x['state'].split(':')[1]) for x in permits):return s.decide(c,e,NAME,'blocked','outside-loan-period','blocked')
  if not p.get('seal') or int(p.get('items',0))<1:return s.decide(c,e,NAME,'blocked','box-list-or-seal-missing','blocked')
  gate=_customs_gate(c,e,sid,p)
  if gate.get('required') and not gate.get('allowed'):
   return s.decide(c,e,NAME,'blocked','customs-item-'+gate.get('status','unresolved'),'blocked')
  c.execute('INSERT OR REPLACE INTO allocations VALUES(?,?,?,?,?)',(e['id'],sid,p['seal'],'sealed',loan['version']))
  if gate.get('status')==customs.STATUS_CONFIRMED:
   c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',
             (e['id']+'-customs',sid,'customs-clearance',loan['version'],
              f"{gate['column']}:{gate.get('destination')}:{gate['attribution_id']}"))
  return s.decide(c,e,NAME,'sealed','box-ready-for-release','sealed')
 if a=='handoff':
  box=s.latest(c,'allocations',p.get('box',''))
  if not box or box['subject']!=sid or box['state'] not in ('sealed','in-transit','broken'):return s.decide(c,e,NAME,'blocked','no-valid-box','blocked')
  if not p.get('from') or not p.get('to') or p['from']==p['to']:return s.decide(c,e,NAME,'blocked','custody-gap','blocked')
  gate=_customs_gate(c,e,sid,p)
  if gate.get('required') and not gate.get('allowed'):
   return s.decide(c,e,NAME,'blocked','customs-item-'+gate.get('status','unresolved'),'blocked')
  previous=c.execute("SELECT state FROM receipts WHERE subject=? AND kind='handoff' ORDER BY rowid DESC LIMIT 1",(sid,)).fetchone()
  parties=c.execute("SELECT state FROM documents WHERE subject=? AND kind='parties' ORDER BY version DESC LIMIT 1",(sid,)).fetchone()
  lender,borrower=parties['state'].split(':') if parties else ('museum-A','museum-B')
  if not previous and p['from']!=lender:return s.decide(c,e,NAME,'blocked','initial-custodian-mismatch','blocked')
  if previous and previous['state']!=p['from']:return s.decide(c,e,NAME,'blocked','previous-custodian-mismatch','blocked')
  state='broken-arrived' if p['to']==borrower and box['state']=='broken' else 'arrived' if p['to']==borrower else 'broken' if box['state']=='broken' else 'in-transit'
  c.execute('UPDATE allocations SET state=? WHERE id=?',(state,box['id']))
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'handoff',p['to'],0))
  return s.decide(c,e,NAME,state,'custody-transferred',state)
 if a=='break':
  box=s.latest(c,'allocations',p.get('box',''))
  if not box or box['subject']!=sid or box['state']!='in-transit':return s.decide(c,e,NAME,'blocked','no-in-transit-box','blocked')
  c.execute("UPDATE allocations SET state='broken' WHERE id=?",(box['id'],))
  return s.decide(c,e,NAME,'quarantine','seal-broken-inspection-required','broken')
 if a=='reseal':
  box=s.latest(c,'allocations',p.get('box',''))
  approval=c.execute("SELECT 1 FROM documents WHERE subject=? AND kind='reseal-approval' AND state=?",(sid,p.get('box',''))).fetchone()
  if not box or box['state']!='broken' or not approval or not p.get('new_seal'):return s.decide(c,e,NAME,'blocked','reseal-not-approved','blocked')
  gate=_customs_gate(c,e,sid,p)
  if gate.get('required') and not gate.get('allowed'):
   return s.decide(c,e,NAME,'blocked','customs-item-'+gate.get('status','unresolved'),'blocked')
  c.execute('INSERT OR REPLACE INTO allocations VALUES(?,?,?,?,?)',(e['id'],sid,p['new_seal'],'sealed',box['version']+1))
  return s.decide(c,e,NAME,'resealed','old-broken-box-retained','resealed')
 if a=='return_plan':
  if not loan or loan['state'] not in ('authorized','paused','revoked'):return s.decide(c,e,NAME,'blocked','no-return-contract','blocked')
  state='urgent-return' if loan['state'] in ('paused','revoked') else 'scheduled-return'
  c.execute('INSERT OR REPLACE INTO allocations VALUES(?,?,?,?,?)',(e['id'],sid,p.get('route','return'),state,loan['version']))
  return s.decide(c,e,NAME,state,'contract-status-drives-return',state)
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')
