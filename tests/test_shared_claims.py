import unittest,tempfile,json
from pathlib import Path
from exhibitflow import cli,storage,claims

class SharedCapClaims(unittest.TestCase):
 """两件借展品同一次护运事故(INC-8)共享 1000 分车次上限的分配与对账。"""
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.con=storage.connect(Path(self.tmp.name)/'db.sqlite');self.n=0;self.ev={}
 def tearDown(self):self.con.close();self.tmp.cleanup()
 def go(self,sub,action,subject,**data):
  self.n+=1
  return cli.run_event(self.con,sub,{'id':f'e{self.n}','subject':subject,'action':action,'data':data,'effective_at':self.n,'received_at':self.n})
 def damaged(self,art,quote):
  """一件文物:借展授权、投保、装箱、途中破封(INC-8)、到馆、损伤查验、定性审查。"""
  self.go('loan','authorize',art,start=1,end=100)
  self.go('loan','insurance',art,start=1,end=100,policy='POL-8',tier='trip')
  box=self.go('transit','seal',art,seal=f'S-{art}',items=1)
  self.go('transit','handoff',art,box=box['event'],**{'from':'museum-A','to':'carrier'})
  brk=self.go('transit','break',art,box=box['event'],incident='INC-8')
  self.go('transit','handoff',art,box=box['event'],**{'from':'carrier','to':'museum-B'})
  insp=self.go('condition','inspect',art,box=box['event'],damage=True,repair_quote=quote)
  liab=self.go('loan','liability',art)
  self.ev[art]={'break':brk['event'],'inspect':insp['event']}
  return liab
 def endorsed(self):
  return self.go('claims','endorsement','INC-8',cap=1000,policy='POL-8',page=1,start=1,end=100,clause='priority')
 def assess(self,art,amount,inv,**kw):
  d=dict(incident='INC-8',amount=amount,deductible=100,invoice=inv,assessor='adjuster-A',damage_event=self.ev[art]['inspect'],break_event=self.ev[art]['break'])
  d.update(kw);return self.go('claims','assess',art,**d)
 def both_assessed(self):
  self.damaged('artifact-1',800);self.damaged('artifact-2',700);self.endorsed()
  self.assess('artifact-1',800,'INV-1');self.assess('artifact-2',700,'INV-2')
 def item(self,rep,art):return [i for i in rep['items'] if i['artifact']==art][0]

 def test_shared_cap_priority_allocation(self):
  liab1=self.damaged('artifact-1',800);liab2=self.damaged('artifact-2',700)
  # 旧定性路径保留为来源:两件都是 insured-carrier-review,但这不是钱
  self.assertEqual(liab1['outcome'],'insured-carrier-review')
  self.assertEqual(liab2['outcome'],'insured-carrier-review')
  self.assertEqual(self.con.execute("SELECT COUNT(*) n FROM receipts WHERE kind='payment'").fetchone()['n'],0)
  self.endorsed()
  # 仍待核的费用项:两条维修报价尚未核损
  rep=claims.report(self.con,'INC-8')
  self.assertEqual(sorted((x['artifact'],x['quote']) for x in rep['pending_expenses']),[('artifact-1',800.0),('artifact-2',700.0)])
  # 逐件核损 800/700,各免赔 100 → 待保 700/600
  self.assertEqual(self.assess('artifact-1',800,'INV-1')['outcome'],'assessed')
  self.assertEqual(self.assess('artifact-2',700,'INV-2')['outcome'],'assessed')
  # 超额反例:各自取 min(待保,1000) 会报 700+600=1300,虚增 300
  self.assertEqual(min(700,1000)+min(600,1000),1300)
  self.assertEqual(sum(claims.allocate({'artifact-1':70000,'artifact-2':60000},100000,'priority',['artifact-1','artifact-2']).values()),100000)
  rep=claims.report(self.con,'INC-8')
  self.assertEqual(rep['total_payable'],1000.0)
  self.assertEqual(rep['cap_remaining'],0.0)
  # 先确认先占用:artifact-1 得 700,artifact-2 只得剩余 300;不是机械各 500
  self.assertEqual({i['artifact']:i['share'] for i in rep['items']},{'artifact-1':700.0,'artifact-2':300.0})
  # 承运剩余义务:300 净损差额 + 两件免赔 200
  self.assertEqual({i['artifact']:i['carrier'] for i in rep['items']},{'artifact-1':100.0,'artifact-2':400.0})
  self.assertEqual(rep['carrier_total'],500.0)
  self.assertEqual(rep['pending_expenses'],[])
  self.assertEqual({d['artifact']:d['amount'] for d in rep['dunning'] if d['state']=='open'},{'artifact-1':700.0,'artifact-2':300.0})

 def test_pro_rata_agreement_reallocates(self):
  self.both_assessed()
  bad=self.go('claims','agreement','INC-8',mode='pro-rata',signed_by=['museum-A'])
  self.assertEqual(bad['reason'],'agreement-not-countersigned')
  ok=self.go('claims','agreement','INC-8',mode='pro-rata',signed_by=['museum-A','museum-B'])
  self.assertEqual(ok['outcome'],'agreed')
  rep=claims.report(self.con,'INC-8')
  self.assertEqual(rep['mode'],'pro-rata')
  # 1000 按净损 700:600 拆分,仍不超赔
  self.assertEqual({i['artifact']:i['share'] for i in rep['items']},{'artifact-1':538.46,'artifact-2':461.54})
  self.assertEqual(rep['total_payable'],1000.0)
  self.assertEqual({i['artifact']:i['carrier'] for i in rep['items']},{'artifact-1':261.54,'artifact-2':238.46})
  self.assertEqual(rep['carrier_total'],500.0)

 def test_payments_salvage_and_dunning_history(self):
  self.both_assessed()
  p1=self.go('claims','payment','artifact-1',incident='INC-8',amount=700,ref='PAY-1')
  self.assertEqual(p1['outcome'],'paid')
  # 中断重试:同一事件重放不双计
  retry=cli.run_event(self.con,'claims',{'id':f'e{self.n}','subject':'artifact-1','action':'payment','data':{'incident':'INC-8','amount':700,'ref':'PAY-1'},'effective_at':self.n,'received_at':self.n})
  self.assertEqual(retry['outcome'],'idempotent')
  self.assertEqual(self.item(claims.report(self.con,'INC-8'),'artifact-1')['paid'],700.0)
  # 同一银行回单换个事件 ID 也不许双计
  dup=self.go('claims','payment','artifact-1',incident='INC-8',amount=700,ref='PAY-1')
  self.assertEqual((dup['outcome'],dup['reason']),('blocked','duplicate-remittance'))
  # 份额 300 不能付 400
  over=self.go('claims','payment','artifact-2',incident='INC-8',amount=400,ref='PAY-X')
  self.assertEqual(over['reason'],'exceeds-entitlement')
  p2=self.go('claims','payment','artifact-2',incident='INC-8',amount=300,ref='PAY-2')
  self.assertEqual(p2['outcome'],'paid')
  rep=claims.report(self.con,'INC-8')
  self.assertEqual({i['artifact']:i['outstanding'] for i in rep['items']},{'artifact-1':0.0,'artifact-2':0.0})
  self.assertEqual([d for d in rep['dunning'] if d['state']=='open'],[])
  # artifact-1 追回 100 残值:释放共享限额重算,旧回单留史
  sv=self.go('claims','salvage','artifact-1',incident='INC-8',amount=100,ref='SALV-1')
  self.assertEqual(sv['outcome'],'salvage-credited')
  rep=claims.report(self.con,'INC-8')
  self.assertEqual({i['artifact']:i['share'] for i in rep['items']},{'artifact-1':600.0,'artifact-2':400.0})
  self.assertEqual({i['artifact']:i['paid'] for i in rep['items']},{'artifact-1':700.0,'artifact-2':300.0})
  self.assertEqual({i['artifact']:i['outstanding'] for i in rep['items']},{'artifact-1':-100.0,'artifact-2':100.0})
  # 银行实付对账:700/300 回单仍在;残值单列,不是另一笔保险金
  self.assertEqual([r['ref'] for r in rep['reconciliation'] if r['kind']=='payment'],['PAY-1','PAY-2'])
  self.assertEqual([(r['ref'],r['amount']) for r in rep['reconciliation'] if r['kind']=='salvage'],[('SALV-1',100.0)])
  # 催赔通知因果修订:artifact-2 新开口 100,旧通知保留为 settled
  self.assertEqual({d['artifact']:d['amount'] for d in rep['dunning'] if d['state']=='open'},{'artifact-2':100.0})
  self.assertEqual(len([d for d in rep['dunning'] if d['state']=='settled']),2)
  # 撤销付款:原回单保留,负向冲正,催赔再修订
  v=self.go('claims','void_payment','artifact-2',payment=p2['event'],reason='bank-reversal')
  self.assertEqual(v['outcome'],'voided')
  self.assertEqual(self.go('claims','void_payment','artifact-2',payment=p2['event'])['reason'],'already-voided')
  self.assertEqual(self.go('claims','void_payment','artifact-2',payment='nope')['reason'],'no-such-payment')
  rep=claims.report(self.con,'INC-8')
  self.assertEqual({i['artifact']:i['paid'] for i in rep['items']},{'artifact-1':700.0,'artifact-2':0.0})
  self.assertIn('payment-void',[r['kind'] for r in rep['reconciliation']])
  self.assertEqual({d['artifact']:d['amount'] for d in rep['dunning'] if d['state']=='open'},{'artifact-2':400.0})
  self.assertTrue(any(d['state']=='superseded' for d in rep['dunning']))

 def test_late_appraisal_revision_reallocates(self):
  self.both_assessed()
  self.go('claims','payment','artifact-1',incident='INC-8',amount=700,ref='PAY-1')
  # 后到鉴价修订:同物同事故同发票,升版而非重复报销
  rev=self.assess('artifact-1',900,'INV-1')
  self.assertEqual(rev['outcome'],'reassessed')
  rep=claims.report(self.con,'INC-8')
  self.assertEqual({i['artifact']:i['share'] for i in rep['items']},{'artifact-1':800.0,'artifact-2':200.0})
  self.assertEqual(rep['total_payable'],1000.0)
  item1=self.item(rep,'artifact-1')
  self.assertEqual(item1['revision'],2)
  self.assertEqual(item1['outstanding'],100.0)
  self.assertEqual({d['artifact']:d['amount'] for d in rep['dunning'] if d['state']=='open'},{'artifact-1':100.0,'artifact-2':200.0})
  # 旧核损单留痕
  n=self.con.execute("SELECT COUNT(*) n FROM documents WHERE subject='artifact-1' AND kind='assessment'").fetchone()['n']
  self.assertEqual(n,2)

 def test_evidence_binding_and_guards(self):
  self.damaged('artifact-1',800);self.damaged('artifact-2',700)
  # 无批单不能核损
  self.assertEqual(self.assess('artifact-1',800,'INV-1')['reason'],'no-endorsement')
  self.endorsed()
  # 共同保额以事故号识别:INC-9 批单管不到 INC-8 的事故
  self.go('claims','endorsement','INC-9',cap=500,policy='POL-9',page=1,start=1,end=100)
  r=self.go('claims','assess','artifact-1',incident='INC-9',amount=800,deductible=100,invoice='INV-9',assessor='adjuster-A',damage_event=self.ev['artifact-1']['inspect'],break_event=self.ev['artifact-1']['break'])
  self.assertEqual(r['reason'],'incident-mismatch')
  # 核损行必须绑定 condition 损伤证据与 transit 护运事故
  r=self.go('claims','assess','artifact-1',incident='INC-8',amount=800,deductible=100,invoice='INV-1',assessor='adjuster-A',damage_event='nope',break_event=self.ev['artifact-1']['break'])
  self.assertEqual(r['reason'],'no-damage-evidence')
  r=self.go('claims','assess','artifact-1',incident='INC-8',amount=800,deductible=100,invoice='INV-1',assessor='adjuster-A',damage_event=self.ev['artifact-1']['inspect'],break_event='nope')
  self.assertEqual(r['reason'],'no-transit-incident')
  # 验签与金额归一:未签认、免赔超过核损都拒绝
  r=self.go('claims','assess','artifact-1',incident='INC-8',amount=800,deductible=100,invoice='INV-1',damage_event=self.ev['artifact-1']['inspect'],break_event=self.ev['artifact-1']['break'])
  self.assertEqual(r['reason'],'unsigned-assessment')
  self.assertEqual(self.assess('artifact-1',800,'INV-1',deductible=900)['reason'],'invalid-amount')
  self.assertEqual(self.assess('artifact-1',800,'INV-1')['outcome'],'assessed')
  # 同一修复发票不能在两个文物都报销
  self.assertEqual(self.assess('artifact-2',700,'INV-1')['reason'],'invoice-already-claimed')
  self.assertEqual(self.assess('artifact-2',700,'INV-2')['outcome'],'assessed')
  # 重复批单页不双计:同页号重录仍是一个上限
  self.go('claims','endorsement','INC-8',cap=1000,policy='POL-8',page=1,start=1,end=100)
  n=self.con.execute("SELECT COUNT(*) n FROM documents WHERE kind='endorsement' AND subject='INC-8'").fetchone()['n']
  self.assertEqual(n,1)
  rep=claims.report(self.con,'INC-8')
  self.assertEqual(rep['cap'],1000.0)
  self.assertEqual(rep['total_payable'],1000.0)

class SharedPolicyCapFixture(unittest.TestCase):
 def test_fixture_replays_incident_8(self):
  with tempfile.TemporaryDirectory() as tmp:
   con=storage.connect(Path(tmp)/'fx.db')
   rows=json.loads((Path(__file__).resolve().parent.parent/'fixtures'/'shared-policy-cap.json').read_text(encoding='utf-8'))
   results=[cli.run_event(con,x['subsystem'],x['event']) for x in rows]
   self.assertFalse(any(r['outcome']=='blocked' for r in results))
   rep=claims.report(con,'INC-8')
   self.assertEqual(rep['total_payable'],1000.0)
   self.assertEqual({i['artifact']:i['share'] for i in rep['items']},{'artifact-1':600.0,'artifact-2':400.0})
   self.assertEqual({i['artifact']:i['paid'] for i in rep['items']},{'artifact-1':700.0,'artifact-2':300.0})
   self.assertEqual(rep['carrier_total'],400.0)
   self.assertTrue(cli.audit(con)['ok'])
   con.close()

if __name__=='__main__':unittest.main()
