import unittest,tempfile
from pathlib import Path
from exhibitflow import cli,storage
print('IMPORTED_MODULE',cli.__file__)
class BusinessPaths(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.con=storage.connect(Path(self.tmp.name)/'db.sqlite');self.n=0
 def tearDown(self):self.con.close();self.tmp.cleanup()
 def go(self,sub,action,subject='art-a',**data):
  self.n+=1;return cli.run_event(self.con,sub,{'id':f'e{self.n}','subject':subject,'action':action,'data':data,'effective_at':self.n,'received_at':self.n})
 def loan(self):return self.go('loan','authorize',start=1,end=30)
 def box(self):self.loan();return self.go('transit','seal',seal='S1',items=1)
 def test_authority_and_conflict(self):
  self.assertEqual(self.loan()['outcome'],'authorized');self.assertEqual(self.loan()['outcome'],'blocked')
 def test_invalid_period(self):self.assertEqual(self.go('loan','authorize',start=9,end=2)['outcome'],'blocked')
 def test_seal_and_handoff(self):
  box=self.box();self.assertEqual(box['outcome'],'sealed');self.assertEqual(self.go('transit','handoff',box=box['event'],**{'from':'A','to':'B'})['outcome'],'in-transit')
 def test_missing_seal_and_custody_gap(self):
  self.loan();self.assertEqual(self.go('transit','seal',seal='',items=1)['outcome'],'blocked');box=self.go('transit','seal',seal='S',items=1);self.assertEqual(self.go('transit','handoff',box=box['event'],**{'from':'A','to':'A'})['outcome'],'blocked')
 def test_revoke_intercepts_transit(self):
  box=self.box();self.go('loan','revoke');self.assertEqual(storage.latest(self.con,'allocations',box['event'])['state'],'intercept');self.assertEqual(self.go('condition','inspect',box=box['event'],damage=False)['outcome'],'hold')
 def test_break_damage_changes_liability(self):
  box=self.box();self.go('transit','break',box=box['event']);self.assertEqual(self.go('condition','inspect',box=box['event'],damage=True)['outcome'],'hold');self.assertEqual(self.go('loan','liability')['outcome'],'carrier-review')
 def test_climate_and_return(self):
  box=self.box();self.go('condition','inspect',box=box['event'],damage=False);self.assertEqual(self.go('condition','environment',temp=21,humidity=50)['outcome'],'display');self.assertEqual(self.go('condition','environment',temp=30,humidity=50)['outcome'],'hold');self.assertEqual(self.go('condition','return')['outcome'],'returned')
 def test_event_idempotency(self):
  e={'id':'fixed','subject':'a','action':'authorize','data':{'start':1,'end':2},'effective_at':1};self.assertEqual(cli.run_event(self.con,'loan',e)['outcome'],'authorized');self.assertEqual(cli.run_event(self.con,'loan',e)['outcome'],'idempotent');e['data']['end']=4;self.assertRaises(ValueError,cli.run_event,self.con,'loan',e)
 def test_baseline_score_and_liability(self):
  box=self.box();self.go('condition','baseline',condition_score=90);self.assertEqual(self.go('condition','inspect',box=box['event'],condition_score=70)['outcome'],'hold');self.assertEqual(self.go('loan','liability')['outcome'],'lender-review')
 def test_climate_pause_contract_and_return_route(self):
  box=self.box();self.go('condition','inspect',box=box['event'],damage=False);self.go('condition','environment',temp=30,humidity=50);self.assertEqual(storage.latest(self.con,'subjects','art-a')['state'],'paused');self.assertEqual(self.go('transit','return_plan',route='r1')['outcome'],'urgent-return');self.assertEqual(self.go('loan','extend',end=50)['outcome'],'extended');self.assertEqual(self.go('transit','return_plan',route='r2')['outcome'],'scheduled-return')
 def test_nonoverlap_period_and_handoff_chain(self):
  self.loan();self.assertEqual(self.go('loan','authorize',start=40,end=50)['outcome'],'authorized');box=self.go('transit','seal',seal='S',items=1);self.go('transit','handoff',box=box['event'],**{'from':'museum','to':'carrier'});self.assertEqual(self.go('transit','handoff',box=box['event'],**{'from':'museum','to':'receiver'})['outcome'],'blocked')
 def test_reseal_requires_approval_and_insurance_window(self):
  box=self.box();self.go('transit','break',box=box['event']);self.assertEqual(self.go('transit','reseal',box=box['event'],new_seal='S2')['outcome'],'blocked');self.go('loan','approve_reseal',box=box['event']);self.assertEqual(self.go('transit','reseal',box=box['event'],new_seal='S2')['outcome'],'resealed');self.go('loan','insurance',start=1,end=100);self.go('condition','inspect',box=box['event'],damage=True);self.assertEqual(self.go('loan','liability')['outcome'],'insured-carrier-review')

class EvidenceAudit(unittest.TestCase):
 def test_raw_replay_detects_mutated_decision(self):
  with tempfile.TemporaryDirectory() as tmp:
   con=storage.connect(Path(tmp)/'audit.db')
   event={'id':'audit-1','subject':'audit-subject','action':'unknown','effective_at':1,'received_at':1}
   cli.run_event(con,list(cli.HANDLERS)[0],event)
   self.assertTrue(cli.audit(con)['ok'])
   con.execute("UPDATE decisions SET outcome='tampered' WHERE event_id='audit-1'");con.commit()
   self.assertFalse(cli.audit(con)['ok'])
   con.close()
