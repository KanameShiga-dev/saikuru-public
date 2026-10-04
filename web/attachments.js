'use strict';
// Shared reference draft UI. Documents are never embedded/executed by the browser.
window.ImageAttachments = class {
  constructor(host) {
    this.files=[];this.uploaded=new Map();this.locked=false;this.version=0;
    this.extensions=new Set('.png,.jpg,.jpeg,.webp,.pdf,.docx,.xlsx,.pptx,.txt,.md,.markdown,.log,.csv,.json,.yaml,.yml,.py,.js,.ts,.tsx,.jsx,.cs,.java,.c,.cpp,.h,.hpp,.css,.html,.htm,.sql,.xml,.toml,.ini,.cfg,.conf,.sh,.ps1,.bat,.cmd,.rb,.go,.rs,.vue,.svelte'.split(','));
    this.input=document.createElement('input');this.input.type='file';this.input.multiple=true;this.input.accept=[...this.extensions].join(',');
    this.input.id=host.id+'-files';
    const label=document.createElement('label');label.htmlFor=this.input.id;label.textContent='参考ファイルを添付（画像・PDF・Office・テキスト・コード）';
    const hint=document.createElement('p');hint.className='hint';hint.textContent='4ファイルまで、各5MB・合計15MB。画像の貼り付けも可能です。送信時に全文・画像OCRをローカル検査し、疑わしい指示や検査できない内容は拒否します。マクロ・実行ファイル・暗号化・ZIPは不可。検査後の画像／抽出文字をAIへ送り、原本は実行しません。文書は抽出文字として渡すため、図表・レイアウトは再現しません。PDFは20ページまで。検査には時間がかかる場合があります。検査通過は完全な安全保証ではなく、承認条件は従来どおりです。';
    this.list=document.createElement('div');this.list.className='attachment-gallery';
    this.status=document.createElement('p');this.status.setAttribute('role','status');this.status.setAttribute('aria-live','polite');
    host.append(label,this.input,hint,this.list,this.status);
    this.input.onchange=()=>{this.add([...this.input.files]);this.input.value='';};
    const form=host.closest('form');form.addEventListener('paste',e=>{
      const files=[...(e.clipboardData?.files||[])];if(files.length&&!this.locked){e.preventDefault();this.add(files);}
    });
  }
  add(files) {
    if(this.locked)return;
    if(files.some(f=>!this.extensions.has('.'+f.name.split('.').at(-1).toLowerCase())||f.size>5*1024*1024)||this.files.length+files.length>4||[...this.files.map(x=>x.file),...files].reduce((n,f)=>n+f.size,0)>15*1024*1024){this.status.textContent='許可された形式を4ファイルまで、各5MB・合計15MB以内で選んでください。';return;}
    for(const file of files){const ext=file.name.split('.').at(-1).toLowerCase();this.files.push({file,url:['png','jpg','jpeg','webp'].includes(ext)?URL.createObjectURL(file):null});}
    this.status.textContent='添付はまだ検査・送信されていません。内容を確認して依頼・相談を送信してください。';this.render();
  }
  render() {
    this.list.replaceChildren();
    for(const entry of this.files){
      const box=document.createElement('div'),img=document.createElement('img'),name=document.createElement('p'),remove=document.createElement('button');
      if(entry.url){img.src=entry.url;img.alt=entry.file.name;box.append(img);}name.textContent=entry.file.name;remove.type='button';remove.textContent='取り消す';remove.disabled=this.locked;remove.setAttribute('aria-label',entry.file.name+'の添付を取り消す');
      remove.onclick=()=>{if(entry.url)URL.revokeObjectURL(entry.url);this.files=this.files.filter(x=>x!==entry);this.uploaded.delete(entry);this.render();};box.append(name,remove);this.list.append(box);
    }
    this.input.disabled=this.locked;
  }
  clear() {this.version++;for(const e of this.files)if(e.url)URL.revokeObjectURL(e.url);this.files=[];this.uploaded.clear();this.status.textContent='';this.render();}
  async upload(api) {
    if(this.locked)throw new Error('添付を検査・送信中です。');
    this.locked=true;this.render();const version=this.version;
    try{
      const ids=[];
      for(const entry of this.files){
        this.status.textContent=`添付をローカル検査しています（${ids.length+1}/${this.files.length}）… 疑わしい指示・検査不能は拒否します。`;
        let item=this.uploaded.get(entry);
        if(!item){
          const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(new Error('添付を読み込めません。'));reader.readAsDataURL(entry.file);});
          item=await api('/api/attachments',{name:entry.file.name,data});
          if(version!==this.version)throw new Error('相談が切り替わったため送信を中止しました。');
          this.uploaded.set(entry,item);
        }
        ids.push(item.id);
      }
      this.status.textContent=ids.length?'添付を検査・保存しました。依頼・相談を送信しています…':'';return ids;
    }catch(e){this.status.textContent=e.message+' 添付は残っています。';throw e;}
    finally{this.locked=false;this.render();}
  }
  static gallery(items) {
    const gallery=document.createElement('div');gallery.className='attachment-gallery';
    for(const value of items){const item=typeof value==='string'?{id:value,name:'添付ファイル'}:value;
      const link=document.createElement('a'),img=document.createElement('img'),caption=document.createElement('p');
      link.href='/api/attachments/image?id='+encodeURIComponent(item.id);link.target='_blank';link.rel='noopener';caption.textContent=item.name+'（検査済み内容を開く）';link.append(caption);gallery.append(link);
      const show=meta=>{caption.textContent=meta.name+'（検査済み内容を開く）';if(meta.kind==='image'){img.src=link.href;img.alt=meta.name;img.loading='lazy';link.prepend(img);}};
      if(item.kind)show(item);else fetch('/api/attachments/meta?id='+encodeURIComponent(item.id),{credentials:'same-origin',cache:'no-store'}).then(async response=>{const result=await response.json();if(!response.ok)throw new Error(result.error||'添付を再検査できません。');show(result);}).catch(error=>{caption.textContent=error.message;});
    }return gallery;
  }
};
