"""匿名闸机观众核计:离线分页导出采录、域内去重、跨域重叠证书与可证上下界。

隐私约束(两条路线共用同一批原始闸机页与 display 时段):
- 匿名票码只在签发它的密钥域内可去重;跨域不得用字符串相等/不等判断同人,
  也不得以时间相近硬连(家庭团体会被误合并)。违反者一律 blocked。
- 路线一(精确):托管方盲化交集证书只发布交集基数,不发布个人票码,
  唯一人数 = |A| + |B| - |A∩B|。
- 路线二(区间):不取任何可联结个人令牌,只取带签章的分时进出总量、容量与
  签名重复上界,与 A/B 边际 行放在同一约束中,输出可复算的上下界;
  分时总量不能伪装成逐人精确交集,合同按保守下界结算并注明不确定宽度。
- 证明不可覆写:后到 证据(如密钥轮换页)缩窄区间时只追加新版本并保留修订缘由。
"""
import json, hashlib, hmac, calendar
from . import storage as s
from . import loan as _loan
from . import condition as _condition

NAME='观众核计'
FORBIDDEN_METHODS={'string-compare':'cross-domain-string-compare-forbidden','time-proximity':'time-proximity-linking-forbidden'}

def canonical(obj):return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest_of(obj):return hashlib.sha256(canonical(obj).encode()).hexdigest()
def sign(key,obj):return hmac.new(str(key).encode(),canonical(obj).encode(),hashlib.sha256).hexdigest()

def day_bounds(day):
 y,m,d=(int(x) for x in str(day).split('-'))
 t0=calendar.timegm((y,m,d,0,0,0))
 return t0,t0+86400

def _session_overlaps(first_in,last_out,w0,w1):
 return (first_in<0 or first_in<=w1) and (last_out<0 or last_out>=w0)

def countable_windows(c,sid,day):
 """有效借展 ∩ 实际 display 时段 ∩ 当日;展品未开放的日子没有可计窗。"""
 d0,d1=day_bounds(day)
 out=[]
 for p0,p1 in _loan.permit_windows(c,sid):
  for g0,g1 in _condition.display_windows(c,sid):
   lo,hi=max(p0,g0,d0),min(p1,g1,d1)
   if lo<=hi:out.append((lo,hi))
 return sorted(set(out))

def distinct_tokens(c,key_domain,windows):
 """某密钥域内、会话落入可计窗的去重票码集合(域内去重,不跨域比较)。"""
 tokens=set()
 for r in c.execute('SELECT token,first_in,last_out FROM attendance_sessions WHERE key_domain=?',(key_domain,)):
  if any(_session_overlaps(r['first_in'],r['last_out'],w0,w1) for w0,w1 in windows):tokens.add(r['token'])
 return tokens

def _pair_sessions(rows):
 """单次会话进出配对;单边行(正门进侧门出)保留为开口区间,不丢弃来源。"""
 by={}
 for r in rows:
  by.setdefault(str(r.get('token','')),[]).append((int(r.get('ts',0)),r.get('dir')))
 out=[]
 for tok,items in by.items():
  items.sort(key=lambda x:(x[0],0 if x[1]=='out' else 1))
  open_in=[]
  for ts,d in items:
   if d=='in':open_in.append(ts)
   elif d=='out':
    if open_in:out.append((tok,open_in.pop(0),ts))
    else:out.append((tok,-1,ts))
  for ts in open_in:out.append((tok,ts,-1))
 return out

def _recorrect(c,counter):
 """计数器回卷修正:按业务时刻重排全量读数,raw/wraps/modulus 全部留档。"""
 rows=c.execute('SELECT rowid,* FROM counter_readings WHERE counter=? ORDER BY effective_at,rowid',(counter,)).fetchall()
 wraps=0;prev=None;mod=0
 for r in rows:
  if r['modulus']:mod=r['modulus']
  if mod and prev is not None and r['raw']<prev:wraps+=1
  corrected=r['raw']+wraps*mod if mod else r['raw']
  c.execute('UPDATE counter_readings SET wraps=?,corrected=?,modulus=? WHERE rowid=?',(wraps,corrected,mod,r['rowid']))
  prev=r['raw']

def _issue(c,e,sid,day,kind,lo,hi,basis,revision_reason):
 """出具或修订证明:已出证明只增不改,修订必须带缘由;界不变则幂等。"""
 settlement=lo
 raw=canonical(basis)
 latest=c.execute('SELECT * FROM attendance_proofs WHERE subject=? AND day=? ORDER BY version DESC LIMIT 1',(sid,day)).fetchone()
 if latest:
  if latest['kind']==kind and latest['lower_bound']==lo and latest['upper_bound']==hi:
   return s.decide(c,e,NAME,'idempotent','proof-already-issued','issued')
  if not revision_reason:return s.decide(c,e,NAME,'blocked','revision-reason-required','blocked')
  version=latest['version']+1;pid=f'proof-{sid}-{day}-v{version}'
  c.execute('INSERT INTO attendance_proofs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(pid,sid,day,kind,lo,hi,settlement,raw,version,latest['id'],revision_reason,int(e.get('effective_at',0))))
  return s.decide(c,e,NAME,'proof-revised',f'v{version}-bounds-{lo}-{hi}','issued')
 pid=f'proof-{sid}-{day}-v1'
 c.execute('INSERT INTO attendance_proofs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(pid,sid,day,kind,lo,hi,settlement,raw,1,'','',int(e.get('effective_at',0))))
 return s.decide(c,e,NAME,'proof-issued',f'v1-bounds-{lo}-{hi}','issued')

def apply(c,e):
 a=e['action'];p=e.get('data',{});sid=e['subject']
 if a=='register_key':
  gate=p.get('gate');dom=p.get('key_domain');vk=p.get('verify_key')
  if not gate or not dom or not vk:return s.decide(c,e,NAME,'blocked','malformed-key','blocked')
  old=c.execute('SELECT verify_key FROM gate_keys WHERE gate=? AND key_domain=?',(gate,dom)).fetchone()
  if old:
   if old['verify_key']!=str(vk):return s.decide(c,e,NAME,'blocked','key-conflict','blocked')
   return s.decide(c,e,NAME,'idempotent','key-already-registered','registered')
  c.execute('INSERT INTO gate_keys VALUES(?,?,?,?,?)',(gate,dom,str(vk),int(p.get('valid_from',0)),int(p.get('valid_to',0))))
  return s.decide(c,e,NAME,'key-registered','rotation-version-recorded','registered')
 if a=='archive_page':
  page=p.get('page');gate=p.get('gate');dom=p.get('key_domain');seq=int(p.get('seq',0));rows=p.get('rows') or []
  if not page or not gate or not dom or not isinstance(rows,list) or any(not isinstance(r,dict) or 'token' not in r or 'ts' not in r for r in rows):return s.decide(c,e,NAME,'blocked','malformed-page','blocked')
  payload={'gate':gate,'key_domain':dom,'seq':seq,'rows':rows}
  digest=digest_of(payload)
  old=c.execute('SELECT digest,state FROM attendance_pages WHERE id=?',(page,)).fetchone()
  if old:
   if old['digest']!=digest:return s.decide(c,e,NAME,'blocked','page-content-conflict','blocked')
   return s.decide(c,e,NAME,'idempotent','page-already-archived',old['state'])
  key=c.execute('SELECT verify_key,valid_from,valid_to FROM gate_keys WHERE gate=? AND key_domain=?',(gate,dom)).fetchone()
  state,reason='archived','page-archived'
  if not key:state,reason='unprovable','unregistered-key-domain'
  elif sign(key['verify_key'],payload)!=p.get('signature'):state,reason='unprovable','bad-signature'
  elif key['valid_to'] and any(not(key['valid_from']<=int(r.get('ts',-1))<=key['valid_to']) for r in rows):state,reason='unprovable','outside-key-validity'
  c.execute('INSERT INTO attendance_pages VALUES(?,?,?,?,?,?,?,?,?)',(page,gate,dom,seq,digest,state,canonical(rows),int(e.get('effective_at',0)),int(e.get('received_at',0))))
  return s.decide(c,e,NAME,state,reason,state)
 if a=='commit_page':
  row=c.execute('SELECT * FROM attendance_pages WHERE id=?',(p.get('page',''),)).fetchone()
  if not row:return s.decide(c,e,NAME,'blocked','no-such-page','blocked')
  if row['state']=='unprovable':return s.decide(c,e,NAME,'blocked','page-unprovable','blocked')
  if row['state']=='committed':return s.decide(c,e,NAME,'idempotent','page-already-committed','committed')
  sessions=_pair_sessions(json.loads(row['rows_json']))
  for tok,fi,lo in sessions:
   c.execute('INSERT OR IGNORE INTO attendance_sessions VALUES(?,?,?,?,?,?)',(row['gate'],row['key_domain'],tok,fi,lo,row['id']))
  c.execute("UPDATE attendance_pages SET state='committed' WHERE id=?",(row['id'],))
  return s.decide(c,e,NAME,'committed',f'sessions-{len(sessions)}','committed')
 if a=='counter_reading':
  counter=p.get('counter')
  if not counter:return s.decide(c,e,NAME,'blocked','malformed-reading','blocked')
  raw=int(p.get('raw',0));modulus=int(p.get('modulus',0));at=int(p.get('at',e.get('effective_at',0)))
  c.execute('INSERT OR REPLACE INTO counter_readings VALUES(?,?,?,?,?,?,?)',(counter,at,raw,0,raw,modulus,e['id']))
  _recorrect(c,counter)
  return s.decide(c,e,NAME,'reading-recorded','wrap-corrected' if modulus else 'occupancy-recorded','recorded')
 if a=='bucket_totals':
  day=p.get('day');buckets=p.get('buckets') or [];cap=int(p.get('capacity',0));q=int(p.get('max_repeat',0))
  start=int(p.get('start',0));end=int(p.get('end',0));counter=p.get('counter','')
  if not day or not isinstance(buckets,list) or any(not isinstance(b,dict) for b in buckets):return s.decide(c,e,NAME,'blocked','malformed-buckets','blocked')
  payload={'subject':sid,'day':day,'start':start,'end':end,'buckets':buckets,'capacity':cap,'max_repeat':q,'counter':counter}
  key=c.execute('SELECT verify_key FROM gate_keys WHERE gate=? AND key_domain=?',('gallery-authority',p.get('signer_domain',''))).fetchone()
  if not key or sign(key['verify_key'],payload)!=p.get('signature'):return s.decide(c,e,NAME,'blocked','bad-signature','blocked')
  if q<0:return s.decide(c,e,NAME,'blocked','invalid-repeat-bound','blocked')
  occ=0;total_in=0;total_out=0;occs=[]
  for b in buckets:
   ins=int(b.get('in',0));outs=int(b.get('out',0))
   occ+=ins-outs;total_in+=ins;total_out+=outs;occs.append(occ)
   if occ<0 or (cap and occ>cap):return s.decide(c,e,NAME,'blocked','capacity-exceeded','blocked')
  if total_in!=total_out or occ!=0:return s.decide(c,e,NAME,'blocked','conservation-violation','blocked')
  if q>total_in:return s.decide(c,e,NAME,'blocked','invalid-repeat-bound','blocked')
  bs=[(int(b.get('start',0)),int(b.get('end',0))) for b in buckets]
  if not buckets or bs[0][0]!=start or bs[-1][1]!=end or any(bs[i][1]!=bs[i+1][0] for i in range(len(bs)-1)):return s.decide(c,e,NAME,'blocked','bucket-gap','blocked')
  if counter:
   for r in c.execute('SELECT effective_at,corrected FROM counter_readings WHERE counter=? AND effective_at>=? AND effective_at<=?',(counter,start,end)):
    t=r['effective_at'];match=0 if t==start else None
    for i in range(len(bs)):
     if bs[i][1]==t:match=occs[i]
    if match is not None and match!=r['corrected']:return s.decide(c,e,NAME,'blocked','counter-mismatch','blocked')
  c.execute('INSERT INTO bucket_documents VALUES(?,?,?,?,?,?,?,?,?,?,?)',(e['id'],sid,day,start,end,cap,q,total_in,total_out,'accepted',int(e.get('effective_at',0))))
  for i,b in enumerate(buckets):c.execute('INSERT INTO gallery_buckets VALUES(?,?,?,?,?,?,?)',(e['id'],i,bs[i][0],bs[i][1],int(b.get('in',0)),int(b.get('out',0)),occs[i]))
  return s.decide(c,e,NAME,'bucket-totals-accepted',f'entries-{total_in}-repeat-{q}','accepted')
 if a=='overlap_certificate':
  day=p.get('day');ad=p.get('a_domain');bd=p.get('b_domain');inter=int(p.get('intersection',0));blind=p.get('blind_domain','')
  pages=sorted(p.get('pages') or [])
  payload={'subject':sid,'day':day,'a_domain':ad,'b_domain':bd,'intersection':inter,'blind_domain':blind,'pages':pages}
  key=c.execute('SELECT verify_key FROM gate_keys WHERE gate=? AND key_domain=?',('custodian',blind)).fetchone()
  if not key or sign(key['verify_key'],payload)!=p.get('signature'):return s.decide(c,e,NAME,'blocked','bad-custodian-signature','blocked')
  committed=sorted(r['id'] for r in c.execute("SELECT id FROM attendance_pages WHERE key_domain IN (?,?) AND state='committed'",(ad,bd)))
  if not committed or committed!=pages:return s.decide(c,e,NAME,'blocked','certificate-page-mismatch','blocked')
  ca=c.execute('SELECT COUNT(DISTINCT token) n FROM attendance_sessions WHERE key_domain=?',(ad,)).fetchone()['n']
  cb=c.execute('SELECT COUNT(DISTINCT token) n FROM attendance_sessions WHERE key_domain=?',(bd,)).fetchone()['n']
  if inter<0 or inter>min(ca,cb):return s.decide(c,e,NAME,'blocked','impossible-cardinality','blocked')
  c.execute('INSERT INTO overlap_certificates VALUES(?,?,?,?,?,?,?,?,?,?)',(e['id'],sid,day,ad,bd,inter,blind,canonical(pages),'certified',int(e.get('effective_at',0))))
  return s.decide(c,e,NAME,'overlap-certified',f'intersection-{inter}','certified')
 if a=='prove':
  day=p.get('day');route=p.get('route');method=p.get('method')
  if method in FORBIDDEN_METHODS:return s.decide(c,e,NAME,'blocked',FORBIDDEN_METHODS[method],'blocked')
  windows=countable_windows(c,sid,day)
  if not windows:return s.decide(c,e,NAME,'blocked','no-display-window','blocked')
  if route=='exact':
   cert=c.execute('SELECT * FROM overlap_certificates WHERE id=?',(p.get('certificate',''),)).fetchone()
   if not cert or cert['state']!='certified':return s.decide(c,e,NAME,'blocked','overlap-unproven','blocked')
   if cert['subject']!=sid or cert['day']!=day:return s.decide(c,e,NAME,'blocked','certificate-scope-mismatch','blocked')
   committed=sorted(r['id'] for r in c.execute("SELECT id FROM attendance_pages WHERE key_domain IN (?,?) AND state='committed'",(cert['a_domain'],cert['b_domain'])))
   if committed!=json.loads(cert['pages']):return s.decide(c,e,NAME,'blocked','certificate-pages-changed','blocked')
   ta=distinct_tokens(c,cert['a_domain'],windows);tb=distinct_tokens(c,cert['b_domain'],windows)
   x=cert['intersection']
   if x>min(len(ta),len(tb)):return s.decide(c,e,NAME,'blocked','impossible-cardinality','blocked')
   lo=hi=len(ta)+len(tb)-x
   basis={'route':'exact','windows':[list(w) for w in windows],'marginals':{cert['a_domain']:len(ta),cert['b_domain']:len(tb)},'intersection':x,'certificate':cert['id'],'formula':'U=|A|+|B|-|A∩B|','uncertainty_width':0}
  elif route=='interval':
   doc=c.execute("SELECT * FROM bucket_documents WHERE subject=? AND day=? AND state='accepted' ORDER BY effective_at DESC,rowid DESC LIMIT 1",(sid,day)).fetchone()
   if not doc:return s.decide(c,e,NAME,'blocked','insufficient-evidence','blocked')
   if not any(doc['seg_start']>=w0 and doc['seg_end']<=w1 for w0,w1 in windows):return s.decide(c,e,NAME,'blocked','bucket-window-mismatch','blocked')
   domains=[r['key_domain'] for r in c.execute("SELECT DISTINCT key_domain FROM attendance_pages WHERE state='committed' ORDER BY key_domain")]
   counts=[len(distinct_tokens(c,d,windows)) for d in domains]
   total=sum(counts);biggest=max(counts) if counts else 0
   entries=doc['entries'];q=doc['max_repeat']
   lo=max(biggest,entries-q);hi=min(total,entries)
   if lo>hi:return s.decide(c,e,NAME,'blocked','inconsistent-evidence','blocked')
   basis={'route':'interval','windows':[list(w) for w in windows],'marginals':dict(zip(domains,counts)),'bucket_doc':doc['id'],'entries':entries,'max_repeat':q,'capacity':doc['capacity'],'formula':'V in [max(max_domain,entries-max_repeat),min(total_tokens,entries)]','overlap':[total-hi,total-lo],'uncertainty_width':hi-lo}
  else:return s.decide(c,e,NAME,'blocked','unknown-route','blocked')
  return _issue(c,e,sid,day,route,lo,hi,basis,p.get('revision_reason',''))
 return s.decide(c,e,NAME,'blocked','unknown-action','blocked')
