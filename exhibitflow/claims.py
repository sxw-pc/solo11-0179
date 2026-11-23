"""事故级共享保额理赔:批单、逐件核损、限额分配、收付对账与残值修订。

以事故号(而非文物号)识别共同保额。同一车次的共享赔付上限在事故内
所有核损行之间分配,总额永不超过上限;旧 loan.liability 的
insured-carrier-review 只是定性来源,不代表已收到钱。所有金额以厘
(1/100 分)整数存储,重复批单页、重复回单与中断重试都不双计。
"""
import json
from . import storage as s
NAME='保险理赔'

def _j(x):return json.dumps(x,ensure_ascii=False,sort_keys=True)

def _endorsement(c,inc):
 return c.execute("SELECT * FROM documents WHERE subject=? AND kind='endorsement' ORDER BY version DESC,id DESC LIMIT 1",(inc,)).fetchone()

def _mode(c,inc,est):
 row=c.execute("SELECT state FROM documents WHERE subject=? AND kind='agreement' ORDER BY version DESC LIMIT 1",(inc,)).fetchone()
 return json.loads(row['state'])['mode'] if row else est.get('clause','priority')

def _claims(c,inc):
 """事故内每件文物的最新核损行(后到鉴价修订只升版本,旧单留痕)。

 修订继承首次确认的队列位置:first_confirmed/first_event 取各版本最早者,
 金额与版本取最新者——同一索赔的优先权不因鉴价修订而重排。
 """
 out={}
 for r in c.execute("SELECT * FROM documents WHERE kind='assessment'"):
  st=json.loads(r['state'])
  if st.get('incident')!=inc:continue
  cur=out.get(r['subject'])
  if cur is None or st['confirmed_at']<cur['first_confirmed']:
   first_at,first_ev=st['confirmed_at'],r['id']
  else:
   first_at,first_ev=cur['first_confirmed'],cur['first_event']
  if cur is None or r['version']>cur['version']:
   st['version']=r['version'];st['event']=r['id'];out[r['subject']]=st
  out[r['subject']]['first_confirmed']=first_at;out[r['subject']]['first_event']=first_ev
 return out

def _sums(c,inc,kinds):
 out={}
 for r in c.execute(f"SELECT * FROM receipts WHERE kind IN ({','.join('?'*len(kinds))})",kinds):
  st=json.loads(r['state'])
  if st.get('incident')!=inc:continue
  out[r['subject']]=out.get(r['subject'],0)+r['amount']
 return out

def allocate(nets,cap,mode,order):
 """把共享上限 cap(厘)按协议模式分给各核损行;合计永不超过 cap。

 priority :先确认先占用,按核损确认顺序依次占用剩余额度;
 pro-rata :按净损比例拆分,厘级余数分给小数余量最大者,保持合计相等。
 """
 if mode=='pro-rata' and sum(nets.values())>cap:
  total=sum(nets.values());base={a:cap*nets[a]//total for a in nets};rem=cap-sum(base.values())
  for a in sorted(nets,key=lambda a:(-((cap*nets[a])%total),order.index(a))):
   if rem<1:break
   base[a]+=1;rem-=1
  return base
 shares={};remaining=cap
 for a in order:
  shares[a]=min(nets[a],remaining);remaining-=shares[a]
 return shares

def _dunning(c,inc,artifact,outstanding,trigger):
 """催赔通知随分配因果修订:金额变化时旧通知作废留痕,新通知接替。"""
 open_d=[r for r in c.execute("SELECT * FROM receipts WHERE subject=? AND kind='dunning' AND state='open' ORDER BY rowid",(artifact,)) if r['id'].startswith(f'dun:{inc}:')]
 if outstanding>0:
  if open_d and open_d[0]['amount']==outstanding:return
  for r in open_d:c.execute("UPDATE receipts SET state='superseded' WHERE id=?",(r['id'],))
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(f'dun:{inc}:{artifact}:{trigger}',artifact,'dunning','open',outstanding))
 else:
  for r in open_d:c.execute("UPDATE receipts SET state='settled' WHERE id=?",(r['id'],))

def _recompute(c,inc,trigger):
 """按当前批单、协议、核损、残值与付款重算事故级分配与逐件余额。"""
 end=_endorsement(c,inc)
 if not end:return None
 est=json.loads(end['state']);cap=est['cap'];mode=_mode(c,inc,est)
 claims=_claims(c,inc);salv=_sums(c,inc,('salvage',));paid=_sums(c,inc,('payment','payment-void'))
 nets={a:max(0,cl['assessed']-cl['deductible']-salv.get(a,0)) for a,cl in claims.items()}
 order=sorted(claims,key=lambda a:(claims[a]['first_confirmed'],claims[a]['first_event']))
 shares=allocate(nets,cap,mode,order)
 prev=s.latest(c,'allocations',f'cap:{inc}');rev=prev['version']+1 if prev else 1
 for a in order:
  cl=claims[a];out=shares[a]-paid.get(a,0)
  row={'net':s.points(nets[a]),'share':s.points(shares[a]),'paid':s.points(paid.get(a,0)),'outstanding':s.points(out),'salvage':s.points(salv.get(a,0)),'carrier':s.points(max(0,cl['assessed']-salv.get(a,0)-shares[a])),'mode':mode}
  c.execute('INSERT OR REPLACE INTO allocations VALUES(?,?,?,?,?)',(f'claim:{inc}:{a}',a,f'incident:{inc}',_j(row),rev))
  _dunning(c,inc,a,out,trigger)
 summary={'cap':s.points(cap),'allocated':s.points(sum(shares.values())),'remaining':s.points(cap-sum(shares.values())),'mode':mode,'claims':len(claims),'revision':rev}
 c.execute('INSERT OR REPLACE INTO allocations VALUES(?,?,?,?,?)',(f'cap:{inc}',inc,'shared-cap',_j(summary),rev))
 return summary

def apply(c,e):
 a=e['action'];p=e.get('data',{});sid=e['subject']
 if a=='endorsement':
  inc=sid;cap=s.money(p.get('cap',0))
  if cap<=0 or not p.get('policy') or int(p.get('end',0))<int(p.get('start',0)):return s.decide(c,e,NAME,'blocked','invalid-endorsement','blocked')
  if p.get('clause') and p['clause'] not in ('priority','pro-rata'):return s.decide(c,e,NAME,'blocked','unknown-clause','blocked')
  page=int(p.get('page',1))
  state=_j({'cap':cap,'policy':p['policy'],'start':int(p.get('start',0)),'end':int(p.get('end',0)),'clause':p.get('clause','priority')})
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(f'endorsement:{inc}:{page}',inc,'endorsement',page,state))
  _recompute(c,inc,e['id'])
  return s.decide(c,e,NAME,'endorsed','shared-cap-bound-to-incident','cap-bound')
 if a=='assess':
  inc=p.get('incident','')
  end=_endorsement(c,inc)
  if not end:return s.decide(c,e,NAME,'blocked','no-endorsement','blocked')
  dmg=c.execute("SELECT id FROM receipts WHERE subject=? AND kind='damage'",(sid,)).fetchall()
  if p.get('damage_event'):
   if not any(r['id']==p['damage_event'] for r in dmg):return s.decide(c,e,NAME,'blocked','no-damage-evidence','blocked')
  elif not dmg:return s.decide(c,e,NAME,'blocked','no-damage-evidence','blocked')
  brk=c.execute("SELECT id,effective_at FROM events WHERE subject=? AND subsystem='transit' AND action='break' ORDER BY effective_at,id",(sid,)).fetchall()
  if p.get('break_event'):
   hit=[b for b in brk if b['id']==p['break_event']]
   if not hit:return s.decide(c,e,NAME,'blocked','no-transit-incident','blocked')
   btime=hit[0]['effective_at']
  elif brk:btime=brk[-1]['effective_at']
  else:return s.decide(c,e,NAME,'blocked','no-transit-incident','blocked')
  incdoc=c.execute("SELECT state FROM documents WHERE subject=? AND kind='incident' ORDER BY version DESC LIMIT 1",(sid,)).fetchone()
  if not incdoc or incdoc['state']!=inc:return s.decide(c,e,NAME,'blocked','incident-mismatch','blocked')
  est=json.loads(end['state'])
  if not est['start']<=btime<=est['end']:return s.decide(c,e,NAME,'blocked','outside-coverage','blocked')
  if not p.get('assessor'):return s.decide(c,e,NAME,'blocked','unsigned-assessment','blocked')
  assessed=s.money(p.get('amount',0));ded=s.money(p.get('deductible',0))
  if assessed<=0 or ded<0 or ded>assessed:return s.decide(c,e,NAME,'blocked','invalid-amount','blocked')
  inv=p.get('invoice')
  if inv:
   for r in c.execute("SELECT subject,state FROM documents WHERE kind='assessment'"):
    st=json.loads(r['state'])
    if st.get('invoice')==inv and not (r['subject']==sid and st.get('incident')==inc):
     return s.decide(c,e,NAME,'blocked','invoice-already-claimed','blocked')
  prior=[r for r in c.execute("SELECT state FROM documents WHERE subject=? AND kind='assessment'",(sid,)) if json.loads(r['state']).get('incident')==inc]
  state=_j({'incident':inc,'assessed':assessed,'deductible':ded,'invoice':inv,'assessor':p['assessor'],'damage_event':p.get('damage_event'),'break_event':p.get('break_event'),'confirmed_at':int(e.get('effective_at',0))})
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],sid,'assessment',len(prior)+1,state))
  _recompute(c,inc,e['id'])
  return s.decide(c,e,NAME,'reassessed' if prior else 'assessed','loss-bound-to-damage-and-transit-evidence','allocated')
 if a=='agreement':
  inc=sid
  if not _endorsement(c,inc):return s.decide(c,e,NAME,'blocked','no-endorsement','blocked')
  if p.get('mode') not in ('priority','pro-rata'):return s.decide(c,e,NAME,'blocked','unknown-mode','blocked')
  if len(set(p.get('signed_by',[])))<2:return s.decide(c,e,NAME,'blocked','agreement-not-countersigned','blocked')
  n=len(c.execute("SELECT id FROM documents WHERE subject=? AND kind='agreement'",(inc,)).fetchall())
  c.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',(e['id'],inc,'agreement',n+1,_j({'mode':p['mode'],'signed_by':sorted(set(p['signed_by']))})))
  _recompute(c,inc,e['id'])
  return s.decide(c,e,NAME,'agreed','settlement-mode-countersigned',p['mode'])
 if a=='payment':
  inc=p.get('incident','');claims=_claims(c,inc)
  if not _endorsement(c,inc):return s.decide(c,e,NAME,'blocked','no-endorsement','blocked')
  if sid not in claims:return s.decide(c,e,NAME,'blocked','no-assessed-claim','blocked')
  amt=s.money(p.get('amount',0));ref=p.get('ref','')
  if amt<=0 or not ref:return s.decide(c,e,NAME,'blocked','invalid-payment','blocked')
  for r in c.execute("SELECT state FROM receipts WHERE kind='payment'"):
   if json.loads(r['state']).get('ref')==ref:return s.decide(c,e,NAME,'blocked','duplicate-remittance','blocked')
  row=s.latest(c,'allocations',f'claim:{inc}:{sid}')
  share=s.money(json.loads(row['state'])['share']) if row else 0
  if _sums(c,inc,('payment','payment-void')).get(sid,0)+amt>share:return s.decide(c,e,NAME,'blocked','exceeds-entitlement','blocked')
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'payment',_j({'incident':inc,'ref':ref}),amt))
  _recompute(c,inc,e['id'])
  return s.decide(c,e,NAME,'paid','remittance-posted-against-share','posted')
 if a=='void_payment':
  target=p.get('payment','')
  row=c.execute("SELECT * FROM receipts WHERE id=? AND kind='payment'",(target,)).fetchone()
  if not row:return s.decide(c,e,NAME,'blocked','no-such-payment','blocked')
  st=json.loads(row['state']);inc=st['incident']
  for r in c.execute("SELECT state FROM receipts WHERE kind='payment-void'"):
   if json.loads(r['state']).get('payment')==target:return s.decide(c,e,NAME,'blocked','already-voided','blocked')
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],row['subject'],'payment-void',_j({'incident':inc,'payment':target,'reason':p.get('reason','')}),-row['amount']))
  _recompute(c,inc,e['id'])
  return s.decide(c,e,NAME,'voided','payment-reversed-original-kept','reversed')
 if a=='salvage':
  inc=p.get('incident','');claims=_claims(c,inc)
  if sid not in claims:return s.decide(c,e,NAME,'blocked','no-assessed-claim','blocked')
  amt=s.money(p.get('amount',0))
  if amt<=0:return s.decide(c,e,NAME,'blocked','invalid-salvage','blocked')
  if _sums(c,inc,('salvage',)).get(sid,0)+amt>claims[sid]['assessed']:return s.decide(c,e,NAME,'blocked','salvage-exceeds-loss','blocked')
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'salvage',_j({'incident':inc,'ref':p.get('ref','')}),amt))
  _recompute(c,inc,e['id'])
  return s.decide(c,e,NAME,'salvage-credited','recovered-value-releases-shared-cap','reallocated')
 if a=='carrier_confirm':
  inc=p.get('incident','')
  row=s.latest(c,'allocations',f'claim:{inc}:{sid}')
  if not row:return s.decide(c,e,NAME,'blocked','no-allocation','blocked')
  amt=s.money(p['amount']) if p.get('amount') is not None else s.money(json.loads(row['state'])['carrier'])
  c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',(e['id'],sid,'carrier-confirm',_j({'incident':inc}),amt))
  return s.decide(c,e,NAME,'carrier-acknowledged','carrier-residual-confirmed','acknowledged')
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')

def report(c,inc):
 """事故层分配表:可赔总额、逐件保险分配、承运剩余义务、待核费用与银行实付对账。"""
 end=_endorsement(c,inc)
 if not end:return {'incident':inc,'status':'no-endorsement'}
 est=json.loads(end['state']);claims=_claims(c,inc)
 caprow=s.latest(c,'allocations',f'cap:{inc}')
 summary=json.loads(caprow['state']) if caprow else {'cap':s.points(est['cap']),'allocated':0.0,'remaining':s.points(est['cap']),'mode':est.get('clause','priority'),'revision':0}
 items=[]
 for a in sorted(claims,key=lambda x:(claims[x]['first_confirmed'],claims[x]['first_event'])):
  row=s.latest(c,'allocations',f'claim:{inc}:{a}');st=json.loads(row['state']) if row else {};cl=claims[a]
  items.append({'artifact':a,'assessed':s.points(cl['assessed']),'deductible':s.points(cl['deductible']),'invoice':cl.get('invoice'),'revision':cl['version'],'net':st.get('net'),'share':st.get('share'),'paid':st.get('paid'),'outstanding':st.get('outstanding'),'carrier':st.get('carrier')})
 pending=[]
 for r in c.execute("SELECT * FROM receipts WHERE kind='damage' AND amount>0"):
  incdoc=c.execute("SELECT state FROM documents WHERE subject=? AND kind='incident' ORDER BY version DESC LIMIT 1",(r['subject'],)).fetchone()
  if incdoc and incdoc['state']==inc and r['subject'] not in claims:
   pending.append({'artifact':r['subject'],'quote':s.points(r['amount']),'source':r['id']})
 recon=[]
 for r in c.execute("SELECT * FROM receipts WHERE kind IN ('payment','payment-void','salvage','carrier-confirm') ORDER BY rowid"):
  st=json.loads(r['state'])
  if st.get('incident')!=inc:continue
  recon.append({'id':r['id'],'artifact':r['subject'],'kind':r['kind'],'amount':s.points(r['amount']),'ref':st.get('ref') or st.get('payment')})
 duns=[{'id':r['id'],'artifact':r['subject'],'amount':s.points(r['amount']),'state':r['state']} for r in c.execute("SELECT * FROM receipts WHERE kind='dunning' ORDER BY rowid") if r['id'].startswith(f'dun:{inc}:')]
 return {'incident':inc,'policy':est.get('policy'),'mode':summary['mode'],'cap':summary['cap'],'total_payable':summary['allocated'],'cap_remaining':summary['remaining'],'items':items,'carrier_total':round(sum(i['carrier'] or 0 for i in items),2),'pending_expenses':pending,'reconciliation':recon,'dunning':duns}
