'use strict';
// 共通ヘッダーの「その他」メニュー（2026-10-03 UI改善）。処理はメニュー内の各ボタンが持つ既存のIDに任せる。
(()=>{
  for(const menu of document.querySelectorAll('.app-menu')){
    const toggle=menu.querySelector('.app-menu-button'),list=menu.querySelector('.app-menu-list');
    if(!toggle||!list)continue;
    const close=(focus)=>{list.hidden=true;toggle.setAttribute('aria-expanded','false');if(focus)toggle.focus();};
    toggle.addEventListener('click',()=>{const open=list.hidden;list.hidden=!open;toggle.setAttribute('aria-expanded',String(open));if(open)list.querySelector('button:not([disabled])')?.focus();});
    list.addEventListener('click',e=>{if(e.target.closest('button'))close(false);});
    document.addEventListener('click',e=>{if(!menu.contains(e.target))close(false);});
    document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!list.hidden)close(true);});
  }
})();
