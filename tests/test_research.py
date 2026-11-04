import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from analyst.data import make_demo,load_dataset,load_snapshots,digest
from analyst.research import register,run_stage,experiment_configs,replay,daily_bootstrap
from analyst.config import DEFAULT

class ResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name);cls.dataset=cls.root/"data"
        cls.manifest=make_demo(cls.dataset,20)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def test_all_filters_have_an_experiment(self):
        cs=experiment_configs(DEFAULT);self.assertEqual(len(cs),28)
        self.assertTrue(all(v=="off" for v in cs["baseline"]["filters"].values()))
    def test_registered_periods_do_not_overlap(self):
        out=self.root/"protocol1";p=register(self.dataset,DEFAULT,out)
        self.assertEqual(p["periods"]["develop"][1],p["periods"]["select"][0]);self.assertEqual(p["periods"]["select"][1],p["periods"]["holdout"][0])
        with self.assertRaises(FileExistsError):register(self.dataset,DEFAULT,out)
    def test_holdout_remains_unopened_if_no_candidate(self):
        out=self.root/"protocol2";register(self.dataset,DEFAULT,out);(out/"select").mkdir()
        (out/"select"/"report.json").write_text(json.dumps({"summaries":{"baseline":{"synthetic":False,"missing":{},"trades":1,"profit_factor":9,"mean_r":3,"max_drawdown_pct":1}}}))
        with self.assertRaisesRegex(ValueError,"не прошёл"):run_stage(self.dataset,out,"holdout")
        self.assertFalse((out/"holdout.exposed").exists())
    def test_synthetic_never_passes_selection(self):
        out=self.root/"protocol3";register(self.dataset,DEFAULT,out);(out/"select").mkdir()
        (out/"select"/"report.json").write_text(json.dumps({"summaries":{"baseline":{"synthetic":True,"missing":{},"trades":1000,"profit_factor":9,"mean_r":3,"max_drawdown_pct":1}}}))
        with self.assertRaises(ValueError):run_stage(self.dataset,out,"holdout")
    def test_modified_protocol_rejected(self):
        out=self.root/"protocol4";register(self.dataset,DEFAULT,out)
        with (out/"protocol.json").open("a") as f:f.write(" ")
        with self.assertRaisesRegex(ValueError,"изменён"):run_stage(self.dataset,out,"develop")
    def test_exposure_claim_written_even_when_run_fails(self):
        out=self.root/"protocol5";register(self.dataset,DEFAULT,out)
        with patch("analyst.research.replay",side_effect=RuntimeError("Interrupted")):
            with self.assertRaises(RuntimeError):run_stage(self.dataset,out,"develop")
        self.assertTrue((out/"develop.exposed").exists())
        with self.assertRaises(FileExistsError):run_stage(self.dataset,out,"develop")
    def test_bootstrap_insufficient_days(self):
        self.assertIsNone(daily_bootstrap([{"entry_ts":0,"net_r":1},{"entry_ts":0,"net_r":-1}]))
    def test_current_snapshot_not_used_as_history(self):
        m=copy.deepcopy(self.manifest);file="snapshot_test.json"
        p=self.dataset/file;p.write_text(json.dumps([{"ts":m["end_ms"]+1,"spread_bps":1}]))
        m["files"][file]=digest(p);m["snapshot_files"]={"BTCUSDT":file}
        rows=load_snapshots(self.dataset,m);self.assertGreater(rows["BTCUSDT"][0]["ts"],m["end_ms"])
    def test_replay_only_completed_minutes_and_forced_end(self):
        cs=experiment_configs(DEFAULT);c={"baseline":cs["baseline"]};end=self.manifest["end_ms"]
        r=replay(self.dataset,c,end-3600000,end)
        a=r["accounts"]["baseline"]
        self.assertFalse(a.positions);self.assertFalse(a.pending)
        self.assertTrue(all(t["exit_ts"]<=end for t in a.closed))
        self.assertTrue(r["summaries"]["baseline"]["synthetic"])

if __name__=="__main__":unittest.main()
