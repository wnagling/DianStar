"""The grader must reject false successes without re-running a model."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.evaluate_shell import cases, edge_cases, judge, seed, sequence_errors


class ShellGraderTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)/'project'

    def test_correct_answer_does_not_hide_an_unrequested_input_change(self):
        case=cases()[7]
        seed(self.root,case)
        (self.root/'answer.txt').write_text('4\n',encoding='utf-8')
        (self.root/'a.txt').write_text('wrong',encoding='utf-8')
        self.assertIn('unexpected_input_change:a.txt',judge(self.root,case))

    def test_missing_answer_still_checks_input_preservation(self):
        case=cases()[10]
        seed(self.root,case)
        (self.root/'words.dat').write_text('changed',encoding='utf-8')
        errors=judge(self.root,case)
        self.assertIn('missing_answer_file',errors)
        self.assertIn('unexpected_input_change:words.dat',errors)

    def test_repeated_ranking_accepts_equal_size_ties_but_rejects_wrong_sizes(self):
        case={**cases()[1],'id':'task-02-r1','original_id':'task-02'}
        seed(self.root,case)
        answer=self.root/'answer.txt'
        answer.write_text('pictures/two.jpg\nleft.dat\nright.dat\n',encoding='utf-8')
        self.assertEqual(judge(self.root,case),[])
        answer.write_text('pictures/two.jpg\nleft.dat\napp.log\n',encoding='utf-8')
        self.assertIn('answer_mismatch',judge(self.root,case))

    def test_unsupported_answer_encoding_is_a_result_not_a_grader_crash(self):
        case=cases()[7]
        seed(self.root,case)
        (self.root/'answer.txt').write_bytes('4\n'.encode('utf-16'))
        self.assertIn('answer_encoding_unsupported',judge(self.root,case))

    def test_exit_sequence_requires_real_result_metadata_in_order(self):
        case=next(case for case in edge_cases() if case['id']=='edge-12')
        def result(code): return {'content':json.dumps({'exit_code':code,'content':'7 then 0'})}
        self.assertEqual(sequence_errors(case,[result(7),result(0)]),[])
        for results in ([result(0),result(7)],[result(0)],[]):
            self.assertEqual(sequence_errors(case,results),['required_exit_sequence_missing'])
