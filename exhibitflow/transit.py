from . import storage as s
from . import route_capture as rc

NAME='包装护运'


def _signoffs(c,protocol=None):
 rows=c.execute("SELECT * FROM documents WHERE kind='signoff' ORDER BY version,id").fetchall()
 out={}
 for r in rows:
  route,proto=r['state'].split('|',1)
  if protocol and proto!=protocol:continue
  out[r['subject']]={'route':route,'protocol':proto,'id':r['id'],'version':r['version']}
 return out


def _open_order(c,trip):
 return c.execute("SELECT * FROM handoff_orders WHERE trip=? AND superseded_by='' AND state='issued'",(trip,)).fetchone()


def _issue(c,e,plan):
 """Write a handoff order, preserving any superseded instruction as history."""
 trip=plan['trip']
 prior=_open_order(c,trip)
 if prior and prior['route']==plan['route']:
  return prior['id'],False
 if prior:
  c.execute("UPDATE handoff_orders SET superseded_by=? WHERE id=?",(e['id'],prior['id']))
  c.execute('INSERT OR REPLACE INTO compensation_events VALUES(?,?,?,?,?,?,?,?)',
            (e['id']+'-comp-'+prior['id'],e['id'],prior['subject'],trip,prior['route'],
             'reroute-standby-compensation',1,e['effective_at']))
 c.execute('INSERT OR REPLACE INTO handoff_orders VALUES(?,?,?,?,?,?,?,?)',
           (e['id']+'-'+trip,e['id'],plan['artifact'],trip,plan['route'],'issued',e['effective_at'],''))
 return e['id']+'-'+trip,True


def apply(c,e):
 a=e['action'];p=e.get('data',{});sid=e['subject'];loan=s.latest(c,'subjects',sid)
 if a=='capture':return rc.capture(c,e)
 if a=='seal':
  if not loan or loan['state']!='authorized':return s.decide(c,e,NAME,'blocked','contract-not-authorized','blocked')
  permits=c.execute("SELECT state FROM documents WHERE subject=? AND kind='permit'",(sid,)).fetchall()
  if not any(int(x['state'].split(':')[0])<=e['effective_at']<=int(x['state'].split(':')[1]) for x in permits):return s.decide(c,e,NAME,'blocked','outside-loan-period','blocked')
  if not p.get('seal') or int(p.get('items',0))<1:return s.decide(c,e,NAME,'blocked','box-list-or-seal-missing','blocked')
  c.execute('INSERT OR REPLACE INTO allocations VALUES(?,?,?,?,?)',(e['id'],sid,p['seal'],'sealed',loan['version']))
  return s.decide(c,e,NAME,'sealed','box-ready-for-release','sealed')
 if a=='handoff':
  box=s.latest(c,'allocations',p.get('box',''))
  if not box or box['subject']!=sid or box['state'] not in ('sealed','in-transit','broken'):return s.decide(c,e,NAME,'blocked','no-valid-box','blocked')
  if not p.get('from') or not p.get('to') or p['from']==p['to']:return s.decide(c,e,NAME,'blocked','custody-gap','blocked')
  previous=c.execute("SELECT state FROM receipts WHERE subject=? AND kind='handoff' ORDER BY rowid DESC LIMIT 1",(sid,)).fetchone()
  parties=c.execute("SELECT state FROM documents WHERE subject=? AND kind='parties' ORDER BY version DESC LIMIT 1",(sid,)).fetchone()
  lender,borrower=parties['state'].split(':') if parties else ('museum-A','museum-B')
  if not previous and p['from']!=lender:return s.decide(c,e,NAME,'blocked','initial-custodian-mismatch','blocked')
  if previous and previous['state']!=p['from']:return s.decide(c,e,NAME,'blocked','previous-custodian-mismatch','blocked')
  if p.get('trip'):
   order=_open_order(c,p['trip'])
   if not order:return s.decide(c,e,NAME,'blocked','no-current-dispatch-order','blocked')
   if p.get('route') and p['route']!=order['route']:return s.decide(c,e,NAME,'blocked','route-order-mismatch','blocked')
   # a receiver handoff is only legal after this trip's dispatch order exists;
   # `inspect` is gated on the completed order that this same event produces
   c.execute("UPDATE handoff_orders SET state='completed' WHERE id=?",(order['id'],))
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
  c.execute('INSERT OR REPLACE INTO allocations VALUES(?,?,?,?,?)',(e['id'],sid,p['new_seal'],'sealed',box['version']+1))
  return s.decide(c,e,NAME,'resealed','old-broken-box-retained','resealed')
 if a=='dispatch':
  protocol=p.get('protocol') or rc.LOCKED
  if protocol not in (rc.LOCKED,rc.TRANSFERABLE):return s.decide(c,e,NAME,'blocked','unknown-endorsement-protocol','blocked')
  options,edges=rc.prepare(c)
  pins={trip:info['route'] for trip,info in _signoffs(c,rc.LOCKED).items() if trip in options}
  plan=rc.solve(options,edges,pinned=pins if protocol==rc.LOCKED else None)
  c.execute('DELETE FROM conflict_edges')
  for label,graph in (('current',edges),('blocked',plan['edges'] if not plan['ok'] else [])):
   for row in rc.conflict_graph_rows(graph,label):
    c.execute('INSERT OR REPLACE INTO conflict_edges VALUES(?,?,?,?,?,?,?)',row)
  for row in plan['plans']:
   c.execute('INSERT OR REPLACE INTO convoy_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
             (e['id']+'-'+row['trip'],e['id'],protocol,p.get('algorithm','global-exchange'),
              row['trip'],row['vehicle'],row['route'] or '',row['artifact'],row['outcome'],
              row['reason'],e['effective_at'],row['sequence']))
   if row['outcome']=='routable':
    order_id,new=_issue(c,e,row)
  # a transferable protocol may supersede a signed route; it is recorded, not hidden
  state='dispatched' if plan['ok'] else 'infeasible'
  return s.decide(c,e,NAME,state,plan['reason'] if plan['ok'] else 'conflict-graph-blocks-plan',
                  'dispatched' if plan['ok'] else 'infeasible')
 if a=='sign_route':
  trip=c.execute('SELECT * FROM trips WHERE id=?',(p.get('trip',''),)).fetchone()
  if not trip:return s.decide(c,e,NAME,'blocked','unknown-trip','blocked')
  protocol=p.get('protocol') or rc.LOCKED
  if protocol not in (rc.LOCKED,rc.TRANSFERABLE):return s.decide(c,e,NAME,'blocked','unknown-endorsement-protocol','blocked')
  routes=[o['route'] for o in rc.route_options(c).get(trip['id'],{}).get('options',[])]
  if p.get('route') not in routes:return s.decide(c,e,NAME,'blocked','route-not-feasible-for-trip','blocked')
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],trip['id'],'signoff',e['effective_at'],f"{p['route']}|{protocol}"))
  return s.decide(c,e,NAME,'signed',f'{protocol}-route-acknowledged','signed')
 if a=='return_plan':
  if not loan or loan['state'] not in ('authorized','paused','revoked'):return s.decide(c,e,NAME,'blocked','no-return-contract','blocked')
  state='urgent-return' if loan['state'] in ('paused','revoked') else 'scheduled-return'
  c.execute('INSERT OR REPLACE INTO allocations VALUES(?,?,?,?,?)',(e['id'],sid,p.get('route','return'),state,loan['version']))
  return s.decide(c,e,NAME,state,'contract-status-drives-return',state)
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')
