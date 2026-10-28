"""W05.05.UI: focus after a workflow action whose button disappears.

Runs restoreFocus from web/app.js under node with a minimal fictional DOM stub. Skips if node
is unavailable. Browser keyboard checks are recorded in docs/p0/UI_ACCEPTANCE.md 3.13.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

APP_JS = Path(__file__).resolve().parents[3] / 'web' / 'app.js'

STUB = r'''
function node(id,{key=null,visible=true,inside=null,hidden=false,tag='DIV'}={}){
  const n={id,tagName:tag,dataset:key?{focusKey:key}:{},offsetParent:visible?{}:null,hidden,tabIndex:0,children:[],
    focus(){document.activeElement=n;},contains(o){let c=o;while(c){if(c===n)return true;c=c.parent;}return false;},
    querySelector(sel){return (this.children||[]).find(c=>c.matches&&c.matches(sel))||null;}};
  if(inside){n.parent=inside;inside.children.push(n);}
  return n;
}
const body=node('body');
const document={activeElement:body,body,
  querySelectorAll(sel){
    if(sel==='[data-focus-key]')return elements.filter(e=>e.dataset.focusKey);
    if(sel==='dialog[open]')return dialogs.filter(d=>d.open);
    return [];}};
let elements=[],dialogs=[],busyDialog=null;
function $(id){return elements.find(e=>e.id===id)||null;}
let focusPending=false;
'''

CASES = r'''
const results={};
function reset(){elements=[];dialogs=[];document.activeElement=body;nextFocusKey=null;focusPending=false;}
// 1. The pressed button disappeared, the old key is passed by loadState, and the result row exists.
reset(); elements.push(node('row',{key:'wp:1'}));
nextFocusKey='wp:1'; restoreFocus('feature-approve:a1');
results.button_removed=[document.activeElement.id,nextFocusKey];
// 2. A modal dialog is open: the row outside it is inert, so focus goes to the in-dialog notice.
reset(); const dlg=node('detail-dialog',{tag:'DIALOG'}); dlg.open=true; dialogs.push(dlg);
const notice=node('dlg-notice',{inside:dlg}); notice.matches=s=>s===':scope > .dialog-notice:not([hidden])';
elements.push(node('row',{key:'wp:1'}),dlg,notice);
nextFocusKey='wp:1'; restoreFocus('feature-approve:a1');
results.modal=[document.activeElement.id,nextFocusKey];
// 3. The focused control still exists after re-render: it keeps focus, and a stale result key is discarded.
reset(); elements.push(node('btn',{key:'feature-reject:a1'}),node('row',{key:'wp:1'}));
nextFocusKey='wp:1'; restoreFocus('feature-reject:a1');
results.same_control=[document.activeElement.id,nextFocusKey];
// 4. A later re-render without an action does not jump to an old result row.
reset(); elements.push(node('row',{key:'wp:1'}));
restoreFocus(null);
results.no_action=[document.activeElement.id,nextFocusKey];
// 5. Focus already on a control: nothing moves, the result key is still consumed.
reset(); const other=node('other'); elements.push(other,node('row',{key:'wp:1'})); document.activeElement=other;
nextFocusKey='wp:1'; restoreFocus(null);
results.focused_elsewhere=[document.activeElement.id,nextFocusKey];
// 6. The busy indicator (a modal without id) holds focus during the refresh: keep the result key,
//    then apply it when hideBusy calls restoreFocus after closing the indicator.
reset(); elements.push(node('row',{key:'wp:1'})); busyDialog=node('busy',{tag:'DIALOG'}); busyDialog.open=true;
const spinner=node('spinner',{inside:busyDialog}); document.activeElement=spinner;
nextFocusKey='wp:1'; restoreFocus('feature-rework-send:w1');
const during=[document.activeElement.id,nextFocusKey];
busyDialog.open=false; restoreFocus(null);   // just after close the closed indicator still holds focus
results.busy=[...during,document.activeElement.id,nextFocusKey];
process.stdout.write(JSON.stringify(results));
'''


class FocusRestoreTests(unittest.TestCase):
    def test_result_focus_after_disappearing_button_main_and_modal(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is required to evaluate web/app.js')
        source = APP_JS.read_text(encoding='utf-8')
        decl = re.search(r'let nextFocusKey=null;', source)
        func = re.search(r'function restoreFocus\(key\)\{.*?\n\}', source, re.S)
        self.assertTrue(decl and func, 'web/app.js no longer defines restoreFocus/nextFocusKey')
        out = subprocess.run([node, '-e', STUB + decl.group(0) + '\n' + func.group(0) + CASES],
                             capture_output=True, text=True, encoding='utf-8', timeout=30, check=True)
        r = json.loads(out.stdout)
        self.assertEqual(r['button_removed'], ['row', None])
        self.assertEqual(r['modal'], ['dlg-notice', None])
        self.assertEqual(r['same_control'], ['btn', None])
        self.assertEqual(r['no_action'], ['body', None])
        self.assertEqual(r['focused_elsewhere'], ['other', None])
        self.assertEqual(r['busy'], ['spinner', 'wp:1', 'row', None])


if __name__ == '__main__':
    unittest.main()
