"""匿名闸机观众核计测试:两隐私路线、域内去重、回卷、跨午夜轮换、幂等与证明可审计。"""
import unittest,tempfile,json,calendar
from pathlib import Path
from exhibitflow import cli,storage,attendance

T0=calendar.timegm((2026,11,23,10,0,0))
DAY='2026-11-23'
KEYA,KEYB,CUST,AUTH='gateA-key-1','gateB-key-1','custodian-key-1','gallery-auth-key-1'
FIXTURES=Path(__file__).resolve().parent.parent/'fixtures'

def rows_for(tokens,start,step=60,dur=900):
 rows=[]
 for i,t in enumerate(tokens):
  rows.append({'token':t,'ts':start+i*step,'dir':'in'})
  rows.append({'token':t,'ts':start+i*step+dur,'dir':'out'})
 return rows

class GalleryAttendance(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'db.sqlite';self.con=storage.connect(self.db);self.n=0
 def tearDown(self):self.con.close();self.tmp.cleanup()
 def go(self,action,subject='artifact-1',sub='attendance',eff=None,**data):
  self.n+=1
  return cli.run_event(self.con,sub,{'id':f'e{self.n}','subject':subject,'action':action,'data':data,'effective_at':eff if eff is not None else self.n,'received_at':self.n})
 def chain(self):
  """授权-护运-检查-环境基线;许可窗覆盖两天,测试各自添加 display 时段。"""
  self.assertEqual(self.go('authorize',sub='loan',eff=T0-7200,start=T0-7200,end=T0+3*86400,version=1)['outcome'],'authorized')
  box=self.go('seal',sub='transit',eff=T0-7100,seal='seal-G',items=1)['event']
  self.go('handoff',sub='transit',eff=T0-7000,box=box,**{'from':'museum-A','to':'carrier'})
  self.go('handoff',sub='transit',eff=T0-6900,box=box,**{'from':'carrier','to':'museum-B'})
  self.go('inspect',sub='condition',eff=T0-6800,box=box,damage=False)
  self.assertEqual(self.go('environment',sub='condition',eff=T0-6700,temp=21,humidity=50)['outcome'],'display')
 def segment(self,start=T0,end=T0+7200):
  self.assertEqual(self.go('display',sub='condition',eff=start-100,start=start,end=end)['outcome'],'display-segment')
 def key(self,gate,domain,vk,vf=0,vt=0):
  return self.go('register_key',subject='gallery-1',gate=gate,key_domain=domain,verify_key=vk,valid_from=vf,valid_to=vt)
 def page(self,pid,gate,dom,seq,rows,vk,bad=False):
  sig='00'*32 if bad else attendance.sign(vk,{'gate':gate,'key_domain':dom,'seq':seq,'rows':rows})
  return self.go('archive_page',subject='gallery-1',page=pid,gate=gate,key_domain=dom,seq=seq,rows=rows,signature=sig)
 def commit(self,pid):return self.go('commit_page',subject='gallery-1',page=pid)
 def standard_gates(self,a=80,b=70):
  """合成两小时窗:A 域 80 票码(另有 1 票码在展出窗外),B 域 70 票码,域内无重复。"""
  self.segment()
  self.key('gate-A','A-'+DAY,KEYA);self.key('gate-B','B-'+DAY,KEYB)
  self.key('custodian','blind-'+DAY,CUST);self.key('gallery-authority','auth-'+DAY,AUTH)
  self.page('pA1','gate-A','A-'+DAY,1,rows_for([f'A-tok-{i:03d}' for i in range(1,a+1)],T0+600),KEYA)
  self.page('pA2','gate-A','A-'+DAY,2,[{'token':'A-tok-late','ts':T0+8000,'dir':'in'},{'token':'A-tok-late','ts':T0+8300,'dir':'out'}],KEYA)
  self.page('pB1','gate-B','B-'+DAY,1,rows_for([f'B-tok-{i:03d}' for i in range(1,b+1)],T0+300),KEYB)
  for p in ('pA1','pA2','pB1'):self.assertEqual(self.commit(p)['outcome'],'committed')
 def certificate(self,inter,pages,day=DAY,key=CUST,dom='blind-'+DAY,eff=T0+9000):
  payload={'subject':'artifact-1','day':day,'a_domain':'A-'+day,'b_domain':'B-'+day,'intersection':inter,'blind_domain':dom,'pages':sorted(pages)}
  return self.go('overlap_certificate',eff=eff,day=day,a_domain='A-'+day,b_domain='B-'+day,intersection=inter,blind_domain=dom,pages=sorted(pages),signature=attendance.sign(key,payload))
 def bucket_doc(self,ins,outs,cap,q,eff=T0+9000,counter='gallery-people',span=900):
  buckets=[{'start':T0+i*span,'end':T0+(i+1)*span,'in':x,'out':y} for i,(x,y) in enumerate(zip(ins,outs))]
  payload={'subject':'artifact-1','day':DAY,'start':T0,'end':T0+len(ins)*span,'buckets':buckets,'capacity':cap,'max_repeat':q,'counter':counter}
  return self.go('bucket_totals',eff=eff,day=DAY,start=T0,end=T0+len(ins)*span,buckets=buckets,capacity=cap,max_repeat=q,counter=counter,signer_domain='auth-'+DAY,signature=attendance.sign(AUTH,payload))
 def proofs(self):
  return self.con.execute('SELECT * FROM attendance_proofs ORDER BY version').fetchall()

 def test_demo_baseline_preserved(self):
  """原 demo 的授权与 display 基线不变。"""
  rows=json.loads((FIXTURES/'demo.json').read_text(encoding='utf-8'))
  results=[cli.run_event(self.con,x['subsystem'],x['event']) for x in rows]
  by={r['event']:r for r in results}
  self.assertEqual(by['l1']['outcome'],'authorized')
  self.assertEqual(by['c2']['outcome'],'display')
  self.assertTrue(cli.audit(self.con)['ok'])

 def test_exact_route_80_70_30_gives_120(self):
  """路线一:托管方盲化交集 30 → 精确 80+70-30=120;窗外票码被剪裁不计。"""
  self.chain();self.standard_gates()
  cert=self.certificate(30,['pA1','pA2','pB1'])
  self.assertEqual(cert['outcome'],'overlap-certified')
  r=self.go('prove',eff=T0+9100,day=DAY,route='exact',certificate=cert['event'])
  self.assertEqual(r['outcome'],'proof-issued')
  row=self.proofs()[0]
  self.assertEqual((row['kind'],row['lower_bound'],row['upper_bound'],row['settlement']),('exact',120,120,120))
  basis=json.loads(row['basis'])
  self.assertEqual(basis['marginals'],{'A-'+DAY:80,'B-'+DAY:70})
  self.assertEqual(basis['intersection'],30)
  self.assertTrue(cli.audit(self.con)['ok'])

 def test_cross_domain_string_compare_blocked(self):
  """错把不同盐视不同人(报 150)必须被拦下:无证书不出精确证明,字符串比较/时间硬连禁止,伪签证书拒绝。"""
  self.chain();self.standard_gates()
  r=self.go('prove',eff=T0+9000,day=DAY,route='exact',method='string-compare')
  self.assertEqual((r['outcome'],r['reason']),('blocked','cross-domain-string-compare-forbidden'))
  r=self.go('prove',eff=T0+9001,day=DAY,route='exact',method='time-proximity')
  self.assertEqual(r['reason'],'time-proximity-linking-forbidden')
  r=self.go('prove',eff=T0+9002,day=DAY,route='exact')
  self.assertEqual(r['reason'],'overlap-unproven')
  forged=self.certificate(0,['pA1','pA2','pB1'],key='forged-key',eff=T0+9003)
  self.assertEqual(forged['reason'],'bad-custodian-signature')
  over=self.certificate(71,['pA1','pA2','pB1'],eff=T0+9004)
  self.assertEqual(over['reason'],'impossible-cardinality')
  self.assertEqual(len(self.proofs()),0)

 def test_interval_route_recomputable_110_130(self):
  """路线二:守恒+容量 50+签名重复上界 20 与边际 80/70 共同约束 → 110–130,结算 110,宽 20。"""
  self.chain();self.standard_gates()
  self.go('counter_reading',subject='gallery-1',eff=T0+3600,counter='gallery-people',raw=45)
  self.go('counter_reading',subject='gallery-1',eff=T0+7200,counter='gallery-people',raw=0)
  r=self.bucket_doc([25,25,20,20,15,10,10,5],[5,10,10,20,20,25,20,20],50,20)
  self.assertEqual(r['outcome'],'bucket-totals-accepted')
  r=self.go('prove',eff=T0+9100,day=DAY,route='interval')
  self.assertEqual(r['outcome'],'proof-issued')
  row=self.proofs()[0]
  self.assertEqual((row['lower_bound'],row['upper_bound'],row['settlement']),(110,130,110))
  basis=json.loads(row['basis'])
  self.assertEqual(basis['overlap'],[20,40])
  self.assertEqual(basis['uncertainty_width'],20)
  self.assertEqual(basis['entries'],130)
  self.assertTrue(cli.audit(self.con)['ok'])

 def test_bucket_document_rejects_inconsistent_evidence(self):
  """不守恒、超容量、计数器不符的分时总量不得入库。"""
  self.chain();self.standard_gates()
  self.go('counter_reading',subject='gallery-1',eff=T0+7200,counter='gallery-people',raw=0)
  r=self.bucket_doc([60,70],[0,130],50,20)  # 峰值 60 超容量
  self.assertEqual(r['reason'],'capacity-exceeded')
  r=self.bucket_doc([25,25],[20,20],50,20)  # 进≠出
  self.assertEqual(r['reason'],'conservation-violation')
  self.go('counter_reading',subject='gallery-1',eff=T0+1800,counter='gallery-people',raw=99)
  r=self.bucket_doc([3,3,4,3],[1,2,3,7],5,2,span=1800)  # 与计数器 99 冲突
  self.assertEqual(r['reason'],'counter-mismatch')
  self.assertEqual(self.con.execute('SELECT COUNT(*) c FROM bucket_documents').fetchone()['c'],0)

 def test_counter_wraparound_preserves_source(self):
  """回卷修正保留原始读数与回卷次数;乱序补录重算结果一致。"""
  self.go('counter_reading',subject='gallery-1',eff=200,counter='odo',raw=3,modulus=100)
  self.go('counter_reading',subject='gallery-1',eff=100,counter='odo',raw=98,modulus=100)
  rows=self.con.execute('SELECT raw,wraps,corrected,modulus FROM counter_readings WHERE counter=? ORDER BY effective_at',('odo',)).fetchall()
  self.assertEqual([(r['raw'],r['wraps'],r['corrected'],r['modulus']) for r in rows],[(98,0,98,100),(3,1,103,100)])
  self.go('counter_reading',subject='gallery-1',eff=150,counter='odo',raw=98,modulus=100)
  rows=self.con.execute('SELECT raw,wraps,corrected FROM counter_readings WHERE counter=? ORDER BY effective_at',('odo',)).fetchall()
  self.assertEqual([(r['raw'],r['wraps'],r['corrected']) for r in rows],[(98,0,98),(98,0,98),(3,1,103)])

 def test_cross_midnight_rotation_keeps_source_intervals(self):
  """跨午夜密钥轮换:两密钥域各自去重、来源区间留档;旧域签发跨期行不可证;未开放日不可计。"""
  M0=calendar.timegm((2026,11,24,22,0,0))
  self.chain();self.segment(M0,M0+14400)
  self.key('gate-A','A-2026-11-24',KEYA,vf=M0-3600,vt=M0+7200)
  self.key('gate-A','A-2026-11-25','gateA-key-2',vf=M0+7200,vt=M0+18000)
  self.key('gate-B','B-2026-11-24',KEYB,vf=M0-3600,vt=M0+7200)
  self.key('gate-B','B-2026-11-25','gateB-key-2',vf=M0+7200,vt=M0+18000)
  self.key('custodian','blind-x',CUST)
  self.page('pA24','gate-A','A-2026-11-24',1,[{'token':'a24','ts':M0+3600,'dir':'in'},{'token':'a24','ts':M0+5400,'dir':'out'}],KEYA)
  self.page('pB24','gate-B','B-2026-11-24',1,[{'token':'b24','ts':M0+4000,'dir':'in'},{'token':'b24','ts':M0+5000,'dir':'out'}],KEYB)
  self.page('pA25','gate-A','A-2026-11-25',1,[{'token':'a25','ts':M0+9000,'dir':'in'},{'token':'a25','ts':M0+10800,'dir':'out'}],'gateA-key-2')
  self.page('pB25','gate-B','B-2026-11-25',1,[{'token':'b25','ts':M0+9500,'dir':'in'},{'token':'b25','ts':M0+11000,'dir':'out'}],'gateB-key-2')
  r=self.page('pBAD','gate-A','A-2026-11-24',2,[{'token':'aX','ts':M0+9000,'dir':'in'}],KEYA)
  self.assertEqual((r['outcome'],r['reason']),('unprovable','outside-key-validity'))
  for p in ('pA24','pB24','pA25','pB25'):self.commit(p)
  sess={(r['key_domain'],r['token']):(r['first_in'],r['last_out'],r['source_page']) for r in self.con.execute('SELECT * FROM attendance_sessions')}
  self.assertEqual(sess[('A-2026-11-24','a24')],(M0+3600,M0+5400,'pA24'))
  self.assertEqual(sess[('A-2026-11-25','a25')],(M0+9000,M0+10800,'pA25'))
  self.assertEqual(len(sess),4)
  c1=self.certificate(0,['pA24','pB24'],day='2026-11-24',dom='blind-x',eff=M0+20000)
  self.assertEqual(c1['outcome'],'overlap-certified')
  r=self.go('prove',eff=M0+20100,day='2026-11-24',route='exact',certificate=c1['event'])
  self.assertEqual(r['outcome'],'proof-issued')
  c2=self.certificate(0,['pA25','pB25'],day='2026-11-25',dom='blind-x',eff=M0+20200)
  r=self.go('prove',eff=M0+20300,day='2026-11-25',route='exact',certificate=c2['event'])
  self.assertEqual(r['outcome'],'proof-issued')
  got={p['day']:(p['lower_bound'],p['upper_bound']) for p in self.proofs()}
  self.assertEqual(got,{'2026-11-24':(2,2),'2026-11-25':(2,2)})
  r=self.go('prove',eff=M0+20400,day='2026-11-26',route='exact',certificate=c2['event'])
  self.assertEqual(r['reason'],'no-display-window')
  self.assertTrue(cli.audit(self.con)['ok'])

 def test_duplicate_pages_not_double_counted(self):
  """重复提交、重复归档、分页重叠均不双计;同号异内容页拒绝。"""
  self.chain();self.segment()
  self.key('gate-A','A-'+DAY,KEYA)
  rows=rows_for(['A-1','A-2'],T0+600)
  self.page('pA1','gate-A','A-'+DAY,1,rows,KEYA)
  self.assertEqual(self.commit('pA1')['outcome'],'committed')
  self.assertEqual(self.commit('pA1')['outcome'],'idempotent')
  r=self.page('pA1','gate-A','A-'+DAY,1,rows,KEYA)
  self.assertEqual(r['outcome'],'idempotent')
  self.page('pA2','gate-A','A-'+DAY,2,rows,KEYA)
  self.assertEqual(self.commit('pA2')['outcome'],'committed')
  self.assertEqual(self.con.execute('SELECT COUNT(*) c FROM attendance_sessions').fetchone()['c'],2)
  r=self.page('pA1','gate-A','A-'+DAY,1,rows_for(['A-9'],T0+600),KEYA)
  self.assertEqual(r['reason'],'page-content-conflict')

 def test_crash_between_archive_and_commit_same_boundary(self):
  """归档与提交之间中断后重跑,去重边界与无中断运行一致。"""
  self.chain();self.segment()
  self.key('gate-A','A-'+DAY,KEYA)
  rows=rows_for(['A-1','A-2','A-3'],T0+600)
  sig=attendance.sign(KEYA,{'gate':'gate-A','key_domain':'A-'+DAY,'seq':1,'rows':rows})
  ev={'id':'arch-1','subject':'gallery-1','action':'archive_page','data':{'page':'pA1','gate':'gate-A','key_domain':'A-'+DAY,'seq':1,'rows':rows,'signature':sig},'effective_at':50,'received_at':50}
  cli.run_event(self.con,'attendance',ev)
  self.con.close()  # 模拟归档后、提交前中断
  self.con=storage.connect(self.db)
  r=cli.run_event(self.con,'attendance',ev)
  self.assertEqual(r['outcome'],'idempotent')
  self.commit('pA1')
  got=sorted(map(tuple,self.con.execute('SELECT gate,key_domain,token,first_in,last_out,source_page FROM attendance_sessions')))
  tmp2=tempfile.TemporaryDirectory();con2=storage.connect(Path(tmp2.name)/'db.sqlite')
  cli.run_event(con2,'attendance',{'id':'key-1','subject':'gallery-1','action':'register_key','data':{'gate':'gate-A','key_domain':'A-'+DAY,'verify_key':KEYA,'valid_from':0,'valid_to':0},'effective_at':1,'received_at':1})
  cli.run_event(con2,'attendance',ev)
  cli.run_event(con2,'attendance',{'id':'commit-1','subject':'gallery-1','action':'commit_page','data':{'page':'pA1'},'effective_at':51,'received_at':51})
  want=sorted(map(tuple,con2.execute('SELECT gate,key_domain,token,first_in,last_out,source_page FROM attendance_sessions')))
  self.assertEqual(got,want)
  con2.close();tmp2.cleanup()

 def test_late_rotation_page_revises_without_overwriting(self):
  """后到密钥轮换页缩窄区间:无修订缘由拒绝修订,旧版证明保留可审计。"""
  self.chain();self.standard_gates(a=8,b=7)
  self.go('counter_reading',subject='gallery-1',eff=T0+3600,counter='gallery-people',raw=3)
  self.go('counter_reading',subject='gallery-1',eff=T0+7200,counter='gallery-people',raw=0)
  self.assertEqual(self.bucket_doc([3,3,4,3],[1,2,3,7],5,2,span=1800)['outcome'],'bucket-totals-accepted')
  r=self.go('prove',eff=T0+9100,day=DAY,route='interval')
  self.assertEqual(r['outcome'],'proof-issued')
  self.key('gate-A','A-'+DAY+'-r2','gateA-key-2')
  self.page('pA-r2','gate-A','A-'+DAY+'-r2',1,rows_for([f'A2-tok-{i}' for i in range(12)],T0+900),'gateA-key-2')
  self.assertEqual(self.commit('pA-r2')['outcome'],'committed')
  r=self.go('prove',eff=T0+9200,day=DAY,route='interval')
  self.assertEqual(r['reason'],'revision-reason-required')
  r=self.go('prove',eff=T0+9300,day=DAY,route='interval',revision_reason='late-key-rotation-page-committed')
  self.assertEqual(r['outcome'],'proof-revised')
  proofs=self.proofs()
  self.assertEqual(len(proofs),2)
  self.assertEqual((proofs[0]['lower_bound'],proofs[0]['upper_bound'],proofs[0]['settlement']),(11,13,11))
  self.assertEqual((proofs[1]['lower_bound'],proofs[1]['upper_bound']),(12,13))
  self.assertEqual(proofs[1]['supersedes'],proofs[0]['id'])
  self.assertEqual(proofs[1]['revision_reason'],'late-key-rotation-page-committed')
  self.assertTrue(cli.audit(self.con)['ok'])

 def test_unprovable_page_recorded_but_excluded(self):
  """假签名与未登记密钥域的页留档为不可证,不进入去重边界。"""
  self.chain();self.segment()
  self.key('gate-A','A-'+DAY,KEYA)
  self.page('pA1','gate-A','A-'+DAY,1,rows_for(['A-1'],T0+600),KEYA)
  r=self.page('pX','gate-A','A-'+DAY,2,rows_for(['A-2'],T0+600),KEYA,bad=True)
  self.assertEqual((r['outcome'],r['reason']),('unprovable','bad-signature'))
  r=self.page('pY','gate-C','C-'+DAY,1,rows_for(['C-1'],T0+600),'no-key')
  self.assertEqual((r['outcome'],r['reason']),('unprovable','unregistered-key-domain'))
  self.assertEqual(self.commit('pX')['reason'],'page-unprovable')
  self.commit('pA1')
  self.assertEqual(self.con.execute('SELECT COUNT(*) c FROM attendance_sessions').fetchone()['c'],1)
  states={r['id']:r['state'] for r in self.con.execute('SELECT id,state FROM attendance_pages')}
  self.assertEqual(states,{'pA1':'committed','pX':'unprovable','pY':'unprovable'})

 def test_group_entry_not_merged_by_time(self):
  """家庭团体同时通过:相同时间戳的不同票码不得合并,硬连被禁。"""
  self.chain();self.segment()
  self.key('gate-A','A-'+DAY,KEYA);self.key('gate-B','B-'+DAY,KEYB);self.key('custodian','blind-'+DAY,CUST)
  rows=[{'token':'A-f1','ts':T0+600,'dir':'in'},{'token':'A-f2','ts':T0+600,'dir':'in'},
        {'token':'A-f1','ts':T0+1500,'dir':'out'},{'token':'A-f2','ts':T0+1500,'dir':'out'}]
  self.page('pA1','gate-A','A-'+DAY,1,rows,KEYA)
  self.page('pB1','gate-B','B-'+DAY,1,rows_for(['B-1'],T0+700),KEYB)
  self.commit('pA1');self.commit('pB1')
  cert=self.certificate(0,['pA1','pB1'])
  self.assertEqual(cert['outcome'],'overlap-certified')
  r=self.go('prove',eff=T0+9100,day=DAY,route='exact',certificate=cert['event'])
  self.assertEqual(r['outcome'],'proof-issued')
  self.assertEqual(self.proofs()[0]['settlement'],3)

 def test_single_sided_sessions_preserved(self):
  """正门进侧门出:单边会话保留为开口区间且计入。"""
  self.chain();self.segment()
  self.key('gate-A','A-'+DAY,KEYA);self.key('gate-B','B-'+DAY,KEYB);self.key('custodian','blind-'+DAY,CUST)
  self.page('pA1','gate-A','A-'+DAY,1,[{'token':'A-1','ts':T0+600,'dir':'in'}],KEYA)
  self.page('pB1','gate-B','B-'+DAY,1,[{'token':'B-1','ts':T0+1500,'dir':'out'}],KEYB)
  self.commit('pA1');self.commit('pB1')
  sess=sorted((r['token'],r['first_in'],r['last_out']) for r in self.con.execute('SELECT * FROM attendance_sessions'))
  self.assertEqual(sess,[('A-1',T0+600,-1),('B-1',-1,T0+1500)])
  cert=self.certificate(0,['pA1','pB1'])
  r=self.go('prove',eff=T0+9100,day=DAY,route='exact',certificate=cert['event'])
  self.assertEqual(self.proofs()[0]['settlement'],2)

 def test_gallery_gates_fixture(self):
  """交付 fixture:基线链、不可证页、区间 v1 [11,13] 结算 11、证书交集 3 → 精确 v2=12,旧版保留。"""
  rows=json.loads((FIXTURES/'gallery-gates.json').read_text(encoding='utf-8'))
  results=[cli.run_event(self.con,x['subsystem'],x['event']) for x in rows]
  by={r['event']:r for r in results}
  self.assertEqual(by['g-env']['outcome'],'display')
  self.assertEqual(by['g-bad-page']['outcome'],'unprovable')
  self.assertEqual(by['g-commit-dup']['outcome'],'idempotent')
  self.assertEqual(by['g-prove-interval']['outcome'],'proof-issued')
  self.assertEqual(by['g-prove-exact']['outcome'],'proof-revised')
  proofs=self.proofs()
  self.assertEqual(len(proofs),2)
  self.assertEqual((proofs[0]['kind'],proofs[0]['lower_bound'],proofs[0]['upper_bound'],proofs[0]['settlement']),('interval',11,13,11))
  self.assertEqual((proofs[1]['kind'],proofs[1]['lower_bound'],proofs[1]['upper_bound'],proofs[1]['settlement']),('exact',12,12,12))
  self.assertEqual(proofs[1]['supersedes'],proofs[0]['id'])
  self.assertEqual(proofs[1]['revision_reason'],'custodian-blinded-intersection-received')
  self.assertTrue(cli.audit(self.con)['ok'])

if __name__=='__main__':unittest.main()
