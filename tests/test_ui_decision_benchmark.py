import tempfile
import unittest
from pathlib import Path
from ui_decision_benchmark import decide, ModelContext
from ui_decision_report import usage

class UiDecisionBoundaryTests(unittest.TestCase):
    def test_empty_choices_rejected(self):
        with self.assertRaises(ValueError): decide(Path('.'),'ollama',{'choices':{}})
    def test_unbounded_choices_rejected(self):
        with self.assertRaises(ValueError): decide(Path('.'),'ollama',{'choices':{str(n):'action' for n in range(13)}})
    def test_large_state_rejected(self):
        with self.assertRaises(ValueError): decide(Path('.'),'ollama',{'choices':{'STOP':'stop'},'visible':'x'*13000})
    def test_unknown_arm_rejected(self):
        with self.assertRaises(ValueError): decide(Path('.'),'other',{'choices':{'STOP':'stop'}})
    def test_model_tool_request_denied(self):
        with tempfile.TemporaryDirectory() as root:
            ctx=ModelContext(Path(root))
            self.assertFalse(ctx.approve({'operation':'write'})['allow'])
            self.assertEqual(ctx.denied_requests,1)
    def test_usage_unknown_is_not_zero(self):
        self.assertEqual(usage([{'usage':{}}],'input'),'UNKNOWN')
    def test_native_usage_alias(self):
        self.assertEqual(usage([{'usage':{'inputTokens':9}}],'input'),9)

if __name__=='__main__': unittest.main()
