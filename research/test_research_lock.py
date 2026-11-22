import json
import tempfile
import unittest
from pathlib import Path
from research_lock import digest, verify


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, content in {
            "protocol.md":"frozen", "runner.py":"# frozen",
            "pre.json":json.dumps(dict(eligible=True, selected=3))
        }.items():
            (self.root/name).write_text(content)
        self.doc = dict(schema=1, eligible=True, selected=3,
                        windows=[["2024-07-01","2024-10-01"],["2024-11-01","2024-12-01"]],
                        roles=dict(protocol="protocol.md", runner="runner.py", preliminary="pre.json"),
                        artifacts={p.name:digest(p) for p in self.root.iterdir()})
        self.path = self.root/"lock.json"

    def save(self):
        self.path.write_text(json.dumps(self.doc))
        return digest(self.path)

    def test_unchanged(self):
        self.assertEqual(verify(self.path,self.root,self.save())["selected"],3)

    def test_mutation_fails(self):
        anchor=self.save()
        (self.root/"runner.py").write_text("# changed")
        with self.assertRaises(ValueError):verify(self.path,self.root,anchor)

    def test_changed_windows_fail_with_original_anchor(self):
        anchor=self.save()
        self.doc["windows"]=[["2023-01-01","2023-02-01"]]
        self.save()
        with self.assertRaises(ValueError):verify(self.path,self.root,anchor)

    def test_unqualified_or_different_candidate_fails(self):
        for eligible, selected in [(False,3),(True,9)]:
            self.doc.update(eligible=eligible,selected=selected)
            with self.assertRaises(ValueError):verify(self.path,self.root,self.save())

    def test_overlap_and_traversal_fail(self):
        self.doc["windows"]*=2
        with self.assertRaises(ValueError):verify(self.path,self.root,self.save())
        self.doc["windows"]=[["2024-07-01","2024-10-01"]]
        self.doc["artifacts"]["../outside"]="bad"
        with self.assertRaises(ValueError):verify(self.path,self.root,self.save())


if __name__=="__main__":
    unittest.main()
