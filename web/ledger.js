'use strict';
const $=id=>document.getElementById(id);
let state=null, selected=null, busy=false, archivePreview=null, pollTimer=null, movePreview=null, harnessPreview=null;

const labels={name:'プロジェクト名',purpose:'目的・利用者',category:'分類（ソース／配備コピー／資料など）',status:'管理状況（未評価／運用中／保留／完了など）',owner:'管理担当',source_path:'元ソースのパス（配備コピーの場合・未確認なら空欄）',build_method:'ビルド・起動方法',verification_method:'検証方法・証拠の保存場所',completion_criteria:'完了と判断する条件',next_action:'次に行うこと・未確認事項',notes:'制約・注意点・関連情報',harness_proposal:'ハーネス化提案（下書き）'};
const limits={name:160,purpose:1500,category:80,status:80,owner:120,source_path:1000,build_method:2000,verification_method:2000,completion_criteria:2000,next_action:2000,notes:3000,harness_proposal:8000};
const managementGroups=[
 ['basic','基本情報',['name','purpose','category','status','owner']],
 ['source','ソースと起動方法',['source_path','build_method']],
 ['verification','検証と完了条件',['verification_method','completion_criteria']],
 ['next','次の作業と注意事項',['next_action','notes']],
 ['harness','ハーネス化提案',['harness_proposal']]
];
function defaultManagementValues(p){return {name:p.path.split(/[\\/]/).filter(Boolean).at(-1)||'',purpose:'未確認',category:p.observation.suggested_category||'',status:'未評価',owner:'未確認',source_path:'',build_method:'未確認',verification_method:'未確認',completion_criteria:'未確認',next_action:'目的・検証方法・完了条件を確認し、ハーネス化の範囲を決める。',notes:'',harness_proposal:''};}
function renderManagementFields(p){
 const defaults=defaultManagementValues(p);$('fields').replaceChildren();
 for(const[id,title,keys]of managementGroups){
  const section=node('details',undefined,'detail-section management-group');section.id='management-'+id;section.setAttribute('role','tabpanel');section.setAttribute('aria-labelledby','ledger-tab-'+(id==='basic'?'basic':id==='harness'?'harness':'execution'));section.dataset.inputKind='manual';
  const summary=node('summary',title+' ');summary.append(node('span','○ 入力なし','input-indicator'));section.append(summary);
  for(const key of keys){
   let generate=null;
   if(key==='harness_proposal'){
    section.append(node('p','「提案を生成」は台帳情報だけから下書きを作ります。外部モデルへの送信や作業許可の変更はありません。プロジェクト内へファイルを作る場合は、下の読み取り専用調査で案を確認し、明示操作してください。','harness-proposal-help'));
    generate=node('button',p.harness_proposal?'提案を再生成':'提案を生成');generate.type='button';generate.className='harness-proposal-generate';
    section.append(generate);
   }
   const label=node('label',labels[key]),input=node(['name','category','status','owner','source_path'].includes(key)?'input':'textarea');input.name=key;input.value=p[key]||'';input.dataset.defaultValue=defaults[key];input.maxLength=limits[key];
   if(key==='harness_proposal')input.rows=18;
   label.append(input);section.append(label);
   if(key==='harness_proposal'){
    const feedback=node('p','','harness-proposal-feedback');feedback.setAttribute('role','status');feedback.setAttribute('aria-live','polite');section.append(feedback);
   generate.addEventListener('click',()=>{
     if(input.value.trim()&&input.value!==String(p.harness_proposal||'')){feedback.textContent='未保存の提案があります。先に台帳へ保存するか、内容をコピーしてから入力欄を空にしてください。';feedback.className='harness-proposal-feedback error';return;}
     input.value=buildHarnessProposal(p,Object.fromEntries(new FormData($('edit-form'))));
     feedback.textContent='台帳情報から提案を生成しました。未確認の項目を補い、内容を確認してから保存してください。';feedback.className='harness-proposal-feedback';
    updateSectionIndicators();
   });
    const surveyButton=node('button','読み取り専用調査・ファイル案を作成');surveyButton.type='button';surveyButton.className='harness-proposal-generate';
    section.append(surveyButton);
    const workspace=node('div',undefined,'harness-workspace');section.append(workspace);
    const fillButton=node('button','既存情報からハーネスを補完');fillButton.type='button';section.append(fillButton);
    const fillWorkspace=node('div');section.append(fillWorkspace);
    fillButton.addEventListener('click',async()=>{
     fillButton.disabled=true;fillWorkspace.replaceChildren(node('p','既存資料を読み取り専用で確認しています…'));
     try{
      const preview=await api('/api/harness/fill-preview',{id:p.id});fillWorkspace.replaceChildren(node('p',preview.note));
      fillWorkspace.append(node('p',`補完候補 ${preview.files.length}件・根拠 ${preview.facts.length}件・読取省略 ${preview.skipped}件${preview.capped?'（調査上限あり）':''}`));
      for(const item of preview.files){const detail=node('details');detail.append(node('summary',item.path),node('h4','変更前'),node('pre',item.before),node('h4','補完後'),node('pre',item.after));fillWorkspace.append(detail);}
      if(!preview.files.length)return;
      const label=node('label'),confirm=node('input');confirm.type='checkbox';label.append(confirm,document.createTextNode(' 差分を確認し、表示されたハーネス文書の更新を承認します。'));fillWorkspace.append(label);
      const apply=node('button','補完を適用（変更前をバックアップ）');apply.type='button';apply.disabled=true;confirm.addEventListener('change',()=>{apply.disabled=!confirm.checked;});
      const message=node('p');message.setAttribute('role','status');fillWorkspace.append(apply,message);
      apply.addEventListener('click',async()=>{apply.disabled=true;message.textContent='適用中…';try{const result=await api('/api/harness/fill-apply',{token:preview.token,confirmed:true,confirmed_project:preview.path});message.textContent=result.message+' バックアップ: '+result.backup;confirm.disabled=true;}catch(err){message.textContent=err.message;apply.disabled=!confirm.checked;}});
     }catch(err){fillWorkspace.replaceChildren(node('p',err.message,'error'));}finally{fillButton.disabled=false;}
    });
    surveyButton.addEventListener('click',async()=>{
     const fields=Object.fromEntries(new FormData($('edit-form')));
     const changed=Object.keys(labels).some(name=>String(fields[name]??'')!==String(p[name]??''));
     const feedback=section.querySelector('.harness-proposal-feedback');
     if(changed){feedback.textContent='台帳項目に未保存の変更があります。先に「台帳に保存」してから調査してください。';feedback.className='harness-proposal-feedback error';return;}
     surveyButton.disabled=true;feedback.textContent='ファイル名・許可されたメタデータだけを読み取り専用で確認しています。コードは実行しません。';feedback.className='harness-proposal-feedback';workspace.replaceChildren();
     try{harnessPreview=await api('/api/harness/preview',{id:p.id});renderHarnessPreview(workspace,harnessPreview);feedback.textContent='調査と新規ファイル案を作成しました。各案と作成先を確認してください。';}
     catch(err){feedback.textContent=err.message;feedback.className='harness-proposal-feedback error';}
     finally{surveyButton.disabled=false;}
    });
   }
  }
  $('fields').append(section);
 }
}
function renderHarnessPreview(container,preview){
 container.replaceChildren();
 const survey=preview.survey,summary=node('div',undefined,'harness-survey');
 summary.append(node('h4','読み取り専用調査の結果'));
 for(const warning of survey.instruction_health?.warnings||[])summary.append(node('p','指示ファイルの警告：'+warning.message,'archive-warning'));
 summary.append(node('p',`対象: ${preview.project.path}`,'path'));
 summary.append(node('p',`検出言語: ${survey.languages.join('、')||'未確認'} ／ 確認ファイル数: ${survey.file_count_scanned}${survey.capped?'（上限で打ち切り）':''}`));
 summary.append(node('p',`マニフェスト: ${survey.manifest_names.join('、')||'未確認'} ／ 依存名: ${survey.dependency_names.join('、')||'未確認'}`));
 summary.append(node('p',`スクリプト名（実行未確認）: ${survey.script_names.join('、')||'未確認'} ／ テストの手掛かり: ${survey.test_evidence.join('、')||'未確認'}`));
 summary.append(node('p',`秘密情報らしい名前のファイル: ${survey.secret_candidate_count}件（名前・内容は表示せず読み取り対象外） ／ 危険操作に関わるパス名の手掛かり: ${survey.risk_path_signal_count}件`));
 summary.append(node('p',`ディレクトリ（深さ4まで）: ${survey.directories.join('、')||'未確認'} ／ 読取エラー: ${survey.read_errors}件`));
 summary.append(node('p','README・マニフェスト・限定したエントリーポイントから根拠付きの補完候補を作成します。Git remote・秘密情報候補は対象外です。資料の記載と実証済みの動作は区別し、未知の項目は UNKNOWN のまま残します。','harness-note'));
 container.append(summary);
 const structure=node('details',undefined,'harness-structure');structure.open=true;structure.append(node('summary','既存ファイルを整理してフォルダ構成を変換'));
 structure.append(node('p','ルート直下の一般資料・テスト・スクリプトは名前から整理候補を提示します。コードの配置先は推測しません。候補を確認・編集するか、移動元と移動先を入力してください。フォルダそのもの、Git管理情報、秘密情報候補は移動対象外です。','harness-note'));
 const candidates=node('details',undefined,'harness-candidates');candidates.append(node('summary',`移動元に指定できるファイル一覧（最大500件）${survey.relocation_candidates_capped?'・一部省略':''}`));
 const candidateList=node('ul');for(const rel of survey.relocation_candidates)candidateList.append(node('li',rel,'path'));if(!survey.relocation_candidates.length)candidateList.append(node('li','移動候補がありません。'));candidates.append(candidateList);structure.append(candidates);
 const dirLabel=node('label','追加するフォルダ（相対パスを1行に1つ）'),dirInput=node('textarea');dirInput.rows=3;dirInput.placeholder='例: src\ntests\ndocs';dirLabel.append(dirInput);structure.append(dirLabel);
 const moveLabel=node('label','ファイル移動案（移動元 => 移動先を1行に1つ）'),moveInput=node('textarea');moveInput.rows=5;moveInput.placeholder='例: app.py => src/app.py\nREADME-old.md => docs/README-old.md';moveLabel.append(moveInput);structure.append(moveLabel);
 const suggestedMoves=survey.suggested_moves||[];moveInput.value=suggestedMoves.map(x=>`${x.source} => ${x.target}`).join('\n');
 const planButton=node('button',suggestedMoves.length?'候補を確認・構成案を検証':'フォルダ構成案を確認');planButton.type='button';structure.append(planButton);
 const planView=node('div',undefined,'harness-plan-view');planView.setAttribute('aria-live','polite');structure.append(planView);container.append(structure);
 let structurePlan={moves:[],directories:[],validated:suggestedMoves.length===0};
 if(suggestedMoves.length)planView.append(node('p',`名前から推定した整理候補 ${suggestedMoves.length}件です。まだ実行対象ではありません。「候補を確認・構成案を検証」を押して対象とGit影響を確認してください。`,'harness-note'));
 const apply=node('button','確認したコンバートを実行');apply.type='button';
 const status=node('p','','harness-proposal-feedback');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
 const canApply=()=>preview.conflicts.length===0&&structurePlan.validated&&(preview.create_count>0||structurePlan.moves.length>0||structurePlan.directories.length>0);
 apply.disabled=!canApply();
 const invalidate=()=>{structurePlan.validated=false;apply.disabled=true;planView.replaceChildren(node('p','入力が変更されました。「フォルダ構成案を確認」を押して再確認してください。','harness-note'));};
 dirInput.addEventListener('input',invalidate);moveInput.addEventListener('input',invalidate);
 planButton.addEventListener('click',async()=>{
  const moves=[];for(const line of moveInput.value.split(/\r?\n/)){if(!line.trim())continue;const parts=line.split('=>');if(parts.length!==2){status.textContent=`形式を確認してください: ${line}`;status.className='harness-proposal-feedback error';return;}moves.push({source:parts[0].trim(),target:parts[1].trim()});}
  const directories=dirInput.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);planButton.disabled=true;status.textContent='移動元・移動先の存在と衝突を確認しています…';status.className='harness-proposal-feedback';planView.replaceChildren();
  try{const plan=await api('/api/harness/structure-preview',{token:preview.token,moves,directories});structurePlan={moves:plan.moves,directories:plan.directories.map(x=>x.path),validated:true};planView.append(node('h4',`追加フォルダ ${plan.directories.filter(x=>x.state==='create').length}件 ／ ファイル移動 ${plan.moves.length}件`));for(const dir of plan.directories)planView.append(node('p',`フォルダ: ${dir.path} — ${dir.state==='create'?'新規作成':'既存'}`,'path'));for(const move of plan.moves)planView.append(node('p',`${move.source} → ${move.target}`,'path'));if(!plan.moves.length&&!plan.directories.some(x=>x.state==='create'))planView.append(node('p','既存ファイルの移動・追加フォルダはありません。'));if(plan.moves.length||plan.directories.some(x=>x.state==='create')){const git=survey.git||{};planView.append(node('p',`Git影響: ${git.repository||git.parent_repository?'Git管理フォルダ内が変更されます。remoteや履歴は変更しません。未追跡・rename等の差分は移動後にgit statusで確認してください。':'調査時点でGit管理は検出されませんでした。Git状態は必要に応じて人が確認してください。'}`,'harness-git-impact'));planView.append(node('p','移動でimport・リンク・起動・ビルド参照が壊れる場合があります。参照先の修正はこの操作では行わないため、移動先と参照元を確認してください。','harness-git-impact'));}status.textContent=plan.message;apply.disabled=!canApply();}
  catch(err){structurePlan={moves:[],directories:[],validated:false};status.textContent=err.message;status.className='harness-proposal-feedback error';apply.disabled=true;}
  finally{planButton.disabled=false;}
 });
 container.append(node('h4',`作成予定: ${preview.create_count}件 ／ 既存のためスキップ: ${preview.existing_count}件 ／ 衝突: ${preview.conflicts.length}件`));
 const rows=node('div',undefined,'harness-file-list');
 for(const file of preview.files){
  const block=node('details',undefined,'harness-file');const status=file.state==='create'?'新規作成予定':file.state==='existing'?'既存のためスキップ（中身は読み込んでいません）':'衝突・作成不可';
  const heading=node('summary',`${file.path} — ${status}`);block.append(heading);
  if(file.state==='create'){
   const label=node('label','生成案（確認・編集できます）'),area=node('textarea');area.value=file.content;area.rows=12;area.dataset.harnessPath=file.path;area.dataset.defaultValue=file.content;label.append(area);block.append(label);
  } else block.append(node('p',file.state==='existing'?'このファイルには触れません。必要なら別途内容を確認してください。':'このパスには作成しません。既存の構造を確認して個別対応が必要です。'));
  rows.append(block);
 }
 container.append(rows);
 const message=preview.project.id?'作成予定のハーネス資料に加え、下で確認したフォルダ作成・ファイル移動を実行します。':'新規プロジェクトとして読み取りました。コンバート後、別操作で「台帳に登録」してください。';
 const note=node('p',message+' 既存ファイルは上書きせず、削除・コマンド実行・外部送信もしません。確認案は30分で失効します。','harness-note');container.append(note);
 const confirmLabel=node('label',`確認しました。${preview.project.path} にハーネス案 ${preview.create_count}件と、上記で確認したフォルダ構成の変更を適用します。`,'check'),check=node('input');check.type='checkbox';confirmLabel.prepend(check);container.append(confirmLabel,apply,status);
 apply.addEventListener('click',async()=>{
  if(!check.checked){status.textContent='作成対象・移動対象・保存先を確認し、チェックを入れてください。';status.className='harness-proposal-feedback error';return;}
  const files=[...container.querySelectorAll('textarea[data-harness-path]')].map(area=>({path:area.dataset.harnessPath,content:area.value}));
  apply.disabled=true;status.textContent='対象フォルダの変化を再確認してから作成します…';status.className='harness-proposal-feedback';
  try{const result=await api('/api/harness/apply',{token:preview.token,confirmed:true,confirmed_project:preview.project.path,files,moves:structurePlan.moves,directories:structurePlan.directories});harnessPreview=null;container.replaceChildren(node('p',result.message,'harness-proposal-feedback'));for(const path of result.created)container.append(node('p',path,'path'));for(const item of result.moved)container.append(node('p',`${item.from} → ${item.to}`,'path'));for(const path of result.created_directories)container.append(node('p','フォルダ作成: '+path,'path'));container.append(node('p',preview.project.id?'次回の変更前に、最新状態を読み取り専用調査してください。':'新規プロジェクトの台帳情報を登録し、目的・利用者・検証方法などの UNKNOWN を更新してください。','harness-note'));}
  catch(err){status.textContent=err.message;status.className='harness-proposal-feedback error';apply.disabled=!canApply();}
 });
}
function buildHarnessProposal(p,fields){
 const o=p.observation||{},g=o.git||{},technology=o.technology?.join('・')||'未確認';
 const value=(key)=>{const v=String(fields[key]??p[key]??'').trim();return v&&v!=='未確認'?'確認済み: '+v:'要確認';};
 const docs=o.documents?.length?o.documents.map(path=>'- '+path).join('\n'):'- 台帳の自動調査では既定の資料を検出できていません';
 const unknown=[];
 for(const [label,key] of [['目的・利用者','purpose'],['ビルド・起動方法','build_method'],['検証方法','verification_method'],['完了条件','completion_criteria']])if(value(key)==='要確認')unknown.push(label);
 if(!g.repository&&!g.parent_repository)unknown.push('Gitの正本・ブランチ運用');
 unknown.push('許可する作業範囲・変更禁止領域','実行環境と依存関係','承認が必要な操作と停止条件');
 const lines=[
  `# ${String(fields.name||p.name||'プロジェクト')} のハーネス化提案`,
  '',
  '> 台帳情報から作った下書きです。記載のない仕様やコマンドを推測していません。未確認項目を補ってから採用してください。',
  '',
  '## 1. 目的と対象',
  `- 目的・利用者: ${value('purpose')}`,
  `- 分類・状態: ${String(fields.category||p.category||'未分類')} / ${String(fields.status||p.status||'未評価')}`,
  `- 対象フォルダ: ${p.path}`,
  `- 技術構成: ${technology}`,
  `- Git: ${g.repository?'このフォルダをGit管理':g.parent_repository?`親Git ${g.parent_repository}`:'台帳調査時に未検出'}${g.branch?' / branch '+g.branch:''}${g.head?' / HEAD '+g.head:''}`,
  '',
  '## 2. ハーネスの構成案',
  '- 依頼受付: 目的、対象範囲、変更禁止事項、完了条件を作業開始前に整理する。',
  '- 計画・調査: 正本資料、既存コード、作業状態を確認し、変更案と未確認点を提示する。',
  '- 実装: 承認された範囲だけを変更し、既存データ・未コミット作業・隣接機能を保護する。',
  '- 検証・レビュー: 指定された検証を実行し、結果・未実施項目・残るリスクを分けて記録する。',
  '- 引き継ぎ: 変更ファイル、実行結果、次の作業、復旧方法を台帳またはプロジェクトの正本へ記録する。',
  '',
  '## 3. 起動と検証',
  `- 起動・ビルド手順: ${value('build_method')}`,
  `- 検証方法・証拠: ${value('verification_method')}`,
  `- 完了条件: ${value('completion_criteria')}`,
  '',
  '## 4. 安全境界',
  `- AI作業許可: ${p.worker_allowed?'既存の許可設定を個別に確認':'台帳登録のみ。作業許可なし'}`,
  '- 公開、push、削除、移動、端末操作など影響の大きい操作は、事前に対象と影響を提示して承認を得る。',
  '- 認証情報や個人情報を作業指示、ログ、成果物に含めない。Computer Useは采来 — サイクル —の基本方針に従う。',
  `- 台帳の注意事項: ${value('notes')}`,
  '',
  '## 5. 正本資料・次の作業',
  docs,
  `- 次の作業: ${value('next_action')}`,
  '',
  '## 6. 運用開始前に埋める項目',
  ...[...new Set(unknown)].map(item=>'- '+item),
  '- 上記を確認後、プロジェクト固有のハーネスファイル・自動処理・許可設定を別作業として設計する。'
 ];
 return lines.join('\n').slice(0,limits.harness_proposal);
}
function updateSectionIndicators(){
 for(const section of $('editor').querySelectorAll('details[data-input-kind="manual"]')){
  const inputs=[...section.querySelectorAll('input,textarea,select')];
  const count=inputs.filter(input=>{const value=input.value.trim();return value!==''&&value!==(input.dataset.defaultValue||'').trim();}).length;
  const indicator=section.querySelector(':scope > summary .input-indicator');
  indicator.textContent=count?'● 入力あり（'+count+'）':'○ 入力なし';indicator.classList.toggle('has-input',count>0);
  indicator.title='空欄・デフォルト値は入力なし。未保存の入力も含みます。';
 }
}
function normalizedPath(path){return String(path||'').replaceAll('\\','/').replace(/\/+$/,'').toLowerCase();}
function projectRelations(p){
 const key=normalizedPath(p.path),others=state.projects.filter(other=>other.id!==p.id);
 const ancestors=others.filter(other=>key.startsWith(normalizedPath(other.path)+'/')).sort((a,b)=>b.path.length-a.path.length);
 const parent=ancestors[0]||null,gitPath=p.observation.git.parent_repository||'';
 const gitParent=others.find(other=>normalizedPath(other.path)===normalizedPath(gitPath))||null;
 const children=others.filter(other=>normalizedPath(other.path).startsWith(key+'/')&&!others.some(middle=>middle.id!==other.id&&normalizedPath(middle.path).startsWith(key+'/')&&normalizedPath(other.path).startsWith(normalizedPath(middle.path)+'/')));
 return {parent,gitPath,gitParent,children};
}
function navigateProject(p){
 const fields=new FormData($('edit-form'));
 if(Object.keys(labels).some(key=>String(fields.get(key)||'')!==String(selected[key]||''))||$('github-repositories').value.trim()){
  $('save-message').textContent='入力中の内容があります。管理項目は保存し、GitHub公開先の入力は控えてから、別のプロジェクトを開いてください。';
  $('save-message').scrollIntoView({block:'nearest'});return;
 }
 openProjectDetails(p);$('editor').scrollTop=0;
}
function renderRelations(p){
 const {parent,gitPath,gitParent,children}=projectRelations(p),box=$('project-relations');box.replaceChildren();
 box.append(node('p','台帳ノードID：'+p.id,'path'));
 function relation(label,project,path){
  const row=node('div',undefined,'relation-row');row.append(node('strong',label));
  if(project){row.append(node('p',project.name+' ／ '+project.status),node('p',project.path,'path'));const button=node('button',project.name+' の詳細を開く');button.type='button';button.onclick=()=>navigateProject(project);row.append(button);}
  else row.append(node('p',path?path+'（台帳未登録。「パスを指定して登録」で登録できます）':'該当する台帳ノードはありません。','path'));
  box.append(row);
 }
 relation('親プロジェクト（最も近い台帳の上位フォルダ）',parent);
 if(gitPath){relation('所属する親Gitプロジェクト',gitParent,gitPath);box.append(node('p','このフォルダ単独の削除・アーカイブは対象外です。親Gitプロジェクト全体を扱う場合は、上記のパスと削除範囲を確認してください。','archive-warning'));}
 else box.append(node('p','親Gitプロジェクト：調査時点では検出されていません。'));
 box.append(node('h4','子プロジェクト（台帳上の直下）'));
 if(!children.length)box.append(node('p','台帳に登録された子プロジェクトはありません。'));
 for(const child of children)relation('子プロジェクト',child);
}
function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
async function api(path,body){const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Agent-Team-UI':'1'},body:JSON.stringify(body)});let data;try{data=await response.json();}catch{throw Error('PC側に接続できません。再読み込みしてください。');}if(!response.ok)throw Error(data.error||'処理に失敗しました。');return data;}
function message(text,error=false){$('message').textContent=text;$('message').className=error?'error':'';}
async function load(){state=await api('/api/ledger');const old=$('category').value;const categories=[...new Set(state.projects.map(p=>p.category))].sort();$('category').replaceChildren(node('option','すべて'));$('category').firstChild.value='';for(const c of categories){const option=node('option',c);option.value=c;$('category').append(option);}$('category').value=categories.includes(old)?old:'';const s=state.last_scan;$('scan-info').textContent=s?`最終調査：${s.at} ／ 登録候補 ${s.discovered}件 ／ 調査フォルダ ${s.visited_directories}件`:'まだ調査していません。';$('scope').textContent=s?`${s.root}：${s.scope_note} 除外：${s.excluded.join('、')}`:'C:\\Projects 以下のみ調査します。';$('scan-errors').textContent=s?.errors.length?`未確認：${s.errors.join(' ／ ')}`:'前回調査で取得エラーはありません。';render();refreshArchive();refreshMoves();schedulePoll();}
function isRetiredProject(p){
 return /(?:アーカイブ|削除)済(?:み)?|^(?:アーカイブ|削除|archived|deleted)$/i.test(String(p.status||'').trim());
}
function instructionPresence(p){return p.instruction_presence||(instructionReports.get(p.id)||p.observation.instruction_health)?.presence||{agents:null,claude:null,status:'unknown',message:'指示ファイルの有無は未確認です。'};}
function matchesInstructions(p,filter){
 const presence=instructionPresence(p);
 if(filter==='missing')return presence.agents===false||presence.claude===false;
 if(filter==='agents_missing')return presence.agents===false;
 if(filter==='claude_missing')return presence.claude===false;
 return !filter||presence.status===filter;
}
function render(){
 const q=$('search').value.toLowerCase(),category=$('category').value,showRetired=$('show-retired').checked,instructionFilter=$('instruction-filter').value;
 const hidden=showRetired?0:state.projects.filter(isRetiredProject).length;
 const items=state.projects.filter(p=>(showRetired||!isRetiredProject(p))&&(!category||p.category===category)&&matchesInstructions(p,instructionFilter)&&JSON.stringify([p.name,p.path,p.purpose,p.observation.technology]).toLowerCase().includes(q));
 $('count').textContent=`${items.length}件表示 ／ 台帳 ${state.projects.length}件（資料・候補を含む）${hidden?` ／ アーカイブ・削除 ${hidden}件は非表示`:''}`;
 $('list').replaceChildren();

 for(const p of items){
  const card=node('article',undefined,'project'),button=node('button',p.name);button.type='button';button.dataset.projectId=p.id;
  if(selected?.id===p.id&&!$('editor').hidden){card.classList.add('selected');button.setAttribute('aria-current','true');}
  card.append(button,node('p',p.path,'path'));
  const badges=node('div');
  for(const b of [p.category,p.status,p.worker_allowed?'AI作業対象に登録済み':'AI作業の許可なし'])badges.append(node('span',b,'badge'));
  card.append(badges,node('p',`目的：${p.purpose}`),node('p',`技術構成：${p.observation.technology.join(' / ')||'未確認'}`));
  const keyFields=['purpose','owner','build_method','verification_method','completion_criteria'],known=keyFields.filter(k=>p[k]&&p[k]!=='未確認').length;
  const fill=node('div',undefined,'fill'),meter=node('span',undefined,'meter'),bar=node('i');bar.style.width=(known/keyFields.length*100)+'%';meter.append(bar);meter.setAttribute('aria-hidden','true');
  fill.append(node('span',`主要な管理項目 ${known}/${keyFields.length} 確認済み`),meter);if(known<keyFields.length)fill.append(node('span',`未確認 ${keyFields.length-known}項目`,'unknown'));card.append(fill);
  if((state.archives||[]).some(r=>r.project_id===p.id&&r.state==='completed'))card.append(node('p','アーカイブ保存済み（詳細に保存先）'));
  const presence=instructionPresence(p);
  if(presence.status!=='both_present')card.append(node('p',(presence.status==='unknown'?'未確認：':'指示ファイル不足：')+presence.message,'archive-warning'));
  const instructionWarnings=((instructionReports.get(p.id)||p.observation.instruction_health)?.warnings||[]).filter(w=>!['missing_instructions','presence_unknown'].includes(w.code));
  if(instructionWarnings.length)card.append(node('p',`指示ファイルの警告 ${instructionWarnings.length}件（調査時点）。詳細を開いて再確認してください。`,'archive-warning'));
  if(!p.observation.exists)card.append(node('p','フォルダが見つかりません（履歴は保持）。','error'));
  $('list').append(card);
 }
 if(!items.length)$('list').append(node('p','該当するプロジェクトはありません。'));
}
function renderMetadata(p){const o=p.observation,g=o.git,dl=node('dl');const info={'確認日時':o.checked_at,'フォルダ':o.exists?'存在確認済み':'見つかりません','README表題':o.readme_title||'未確認','所属Gitリポジトリ':g.parent_repository||'親リポジトリなし','技術構成':o.technology.join(' / ')||'未確認','Git管理':g.repository?'あり':'直下の .git なし','ブランチ':g.branch||'未確認','HEAD':g.head||'未確認','変更件数':g.repository&&g.changes!==null?`追跡対象 ${g.changes} ／ ステージ ${g.staged} ／ 未追跡 ${g.untracked}`:'未確認','Git取得結果':g.error||'上記は調査時点の情報です。','関連資料':o.documents.join('\n')||'既定の資料名は見つかりません。','Computer Use':'基本禁止','AI作業の許可':p.worker_allowed?'既存の作業対象に登録済み':'台帳登録のみ。作業の許可なし。'};for(const[k,v]of Object.entries(info))dl.append(node('dt',k),node('dd',v));$('metadata').replaceChildren(dl);}
// 2026-10-03 UI改善：詳細はダイアログではなく、一覧の右側の常設パネルに表示する。
function showEditor(){const ed=$('editor');ed.hidden=false;$('editor-placeholder').hidden=true;markSelected();if(matchMedia('(max-width:980px)').matches)ed.scrollIntoView({behavior:'smooth',block:'start'});else ed.scrollTop=0;}
function hideEditor(){$('editor').hidden=true;$('editor-placeholder').hidden=false;markSelected();}
function markSelected(){for(const card of document.querySelectorAll('#list .project')){const b=card.querySelector('button[data-project-id]'),on=!$('editor').hidden&&b?.dataset.projectId===selected?.id;card.classList.toggle('selected',on);if(b){if(on)b.setAttribute('aria-current','true');else b.removeAttribute('aria-current');}}}
let instructionRequest=0;const instructionReports=new Map();
function showInstructionHealth(report){
 const box=$('instruction-health');box.replaceChildren();
 const heading=node('strong','指示ファイルの有無・容量確認');box.append(heading);
 if(report.presence)box.append(node('p',report.presence.message,report.presence.status==='both_present'?'':'archive-warning'));
 box.append(node('p','判定はプロジェクト直下の指示ファイルです。親・共通指示の継承、代替名、AIの実際の読み込みは別途確認してください。'));
 const format=b=>`${(b/1024).toFixed(1)} KiB（${b.toLocaleString()} bytes）`;
 box.append(node('p',`Codex上限候補：${format(report.codex_limit_bytes)} ／ 階層合計の最大候補：${format(report.codex_candidate_max_bytes||0)}。${report.limit_basis}`));
 for(const w of (report.warnings||[]).filter(w=>!['missing_instructions','presence_unknown'].includes(w.code)))box.append(node('p','警告：'+w.message,'archive-warning'));
 if(!(report.warnings||[]).length)box.append(node('p','確認した範囲にサイズ警告はありません。実際に全指示が読まれたことは未確認です。'));
 box.append(node('p',report.hint));
 const details=node('details');details.append(node('summary','ファイル別の容量・行数と確認範囲'));
 for(const f of report.files||[])details.append(node('p',`${f.path}：${format(f.bytes)} ／ ${f.lines===null?'行数未確認':f.lines+'行'}`,'path'));
 details.append(node('p',`Claude Codeは200行未満を推奨。200行は読み込み打ち切りではありません。CLAUDE.mdは4 MiB超で読み込み対象外（現在の公式仕様）。確認：${report.checked_at}。${report.scope}。${report.note||''}`));box.append(details);
 const retry=node('button','容量を再確認');retry.type='button';retry.onclick=()=>refreshInstructionHealth(selected);box.append(retry);
}
async function refreshInstructionHealth(p){
 if(!p)return;const request=++instructionRequest;
 $('instruction-health').replaceChildren(node('p','指示ファイルの容量を読み取り専用で確認しています…'));
 try{const report=await api('/api/instruction-health?project='+encodeURIComponent(p.path));if(request!==instructionRequest||selected?.id!==p.id)return;instructionReports.set(p.id,report);if(report.presence)p.instruction_presence=report.presence;showInstructionHealth(report);render();}
 catch(err){if(request!==instructionRequest)return;const box=$('instruction-health');box.replaceChildren(node('p','指示ファイルの容量は未確認です。'+err.message,'archive-warning'));const retry=node('button','再確認');retry.type='button';retry.onclick=()=>refreshInstructionHealth(p);box.append(retry);}
}
function openProjectDetails(p){selected=p;harnessPreview=null;$('editor-title').textContent=p.name;$('path').textContent=p.path;$('save-message').textContent='';renderMetadata(p);renderManagementFields(p);$('archive-feedback').textContent='';$('archive-feedback').className='';$('github-repositories').value='';archivePreview=null;renderRelations(p);$('move-destination').value='';$('move-feedback').textContent='';movePreview=null;for(const section of $('editor').querySelectorAll('details.detail-section'))section.open=false;refreshArchive();updateSectionIndicators();selectLedgerTab('basic');showEditor();refreshInstructionHealth(p);}
$('edit-form').addEventListener('input',updateSectionIndicators);
$('edit-form').addEventListener('change',updateSectionIndicators);
$('edit-form').addEventListener('submit',async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;try{const fields=Object.fromEntries(new FormData(e.target));await api('/api/ledger/update',{id:selected.id,fields,expected_updated_at:selected.manual_updated_at});await load();const fresh=state.projects.find(p=>p.id===selected.id);if(fresh)openProjectDetails(fresh);message('台帳を保存しました。変更前のDBと編集履歴も保持しています。');$('save-message').textContent='保存しました。';}catch(err){$('save-message').textContent=err.message;selectLedgerTab('basic');$('management-section').open=true;}finally{button.disabled=false;}});
$('show-retired').checked=false;$('close').onclick=hideEditor;$('search').oninput=render;$('category').onchange=render;$('instruction-filter').onchange=render;$('show-retired').onchange=render;
$('scan').onclick=async()=>{if(busy)return;busy=true;$('scan').disabled=true;message('C:\\Projects を調査しています。プロジェクトのコードは実行しません。');try{const r=await api('/api/ledger/scan',{});await load();message(`再調査して登録しました。候補 ${r.discovered}件、取得エラー ${r.errors.length}件。`);}catch(e){message(e.message,true);}finally{busy=false;$('scan').disabled=false;}};
$('backup').onclick=async()=>{try{const r=await api('/api/ledger/backup',{});message(`バックアップ：data/${r.backup}`);}catch(e){message(e.message,true);}};
$('export').onclick=async()=>{try{const data=await api('/api/ledger/export'),url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=node('a');a.href=url;a.download='project-ledger.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message('現在の台帳をJSONで書き出しました。');}catch(e){message(e.message,true);}};
$('add').onclick=()=>{$('add-message').textContent='';$('add-dialog').showModal();};$('add-close').onclick=()=>$('add-dialog').close();
$('add-harness-preview').onclick=async()=>{const form=$('add-form'),path=String(new FormData(form).get('path')||'').trim(),workspace=$('new-harness-workspace'),button=$('add-harness-preview');$('add-message').textContent='';if(!form.elements.path.checkValidity()){form.elements.path.reportValidity();return;}button.disabled=true;workspace.replaceChildren(node('p','フォルダを読み取り専用で調査しています。コードは実行しません。','harness-note'));try{harnessPreview=await api('/api/harness/preview',{path});renderHarnessPreview(workspace,harnessPreview);}catch(err){workspace.replaceChildren();$('add-message').textContent=err.message;}finally{button.disabled=false;}};
$('add-form').elements.path.addEventListener('input',()=>{if(!harnessPreview)return;harnessPreview=null;$('new-harness-workspace').replaceChildren(node('p','対象パスが変更されました。新しいパスで読み取り専用調査をやり直してください。','harness-note'));});
$('add-form').onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;try{await api('/api/ledger/add',{path:new FormData(e.target).get('path')});await load();$('add-dialog').close();message('台帳に登録しました。AIの作業許可は変更していません。');}catch(err){$('add-message').textContent=err.message;}finally{button.disabled=false;}};
const archiveLive=new Set(['preparing','github_retiring','compressing','verifying','verified','deleting']);
const archiveLabels={preparing:'準備中',github_retiring:'GitHubを非公開化・接続解除中',compressing:'圧縮中',verifying:'内容を照合中',verified:'ZIP照合済み・削除準備中',deleting:'元フォルダを削除中',completed:'アーカイブ完了',failed:'中止・元フォルダを確認',needs_recovery:'復旧が必要',interrupted:'サービス停止で中断'};
function sizeText(bytes){if(bytes>=1024**3)return (bytes/1024**3).toFixed(2)+' GB';if(bytes>=1024**2)return (bytes/1024**2).toFixed(1)+' MB';return bytes.toLocaleString()+' bytes';}
function refreshArchive(){if(!selected)return;refreshMoves();const records=(state.archives||[]).filter(r=>r.project_id===selected.id);$('archive-status').replaceChildren();for(const r of records){const block=node('div',undefined,'archive-record');block.append(node('p',r.state==='completed'&&r.operation==='delete'?'削除完了':archiveLabels[r.state]||r.state,archiveLive.has(r.state)?'archive-progress':'archive-result'),node('p',r.message));if(archiveLive.has(r.state))block.append(node('p',`${r.completed_files||0} / ${r.files} ファイルを圧縮済み。画面を閉じても処理は続きます。`));block.append(node('p',`保存先：${r.archive}`,'path'));for(const g of r.github_changes||[])block.append(node('p',`GitHub ${g.repository}：${g.private_confirmed?'非公開を確認済み':'変更・確認の途中'}`));if(r.removed_remotes?.length)block.append(node('p','解除したGit remote：'+r.removed_remotes.join('、')));if(r.zip_sha256)block.append(node('p',`ZIP SHA-256：${r.zip_sha256}`,'path'));if(r.state==='failed'&&r.partial_archive&&!r.zip_sha256)block.append(node('p',`未完成の圧縮ファイル（復元用ではありません）：${r.partial_archive}`,'path'));if(r.manifest)block.append(node('p',`照合記録：${r.manifest}`,'path'));if(['needs_recovery','interrupted'].includes(r.state))block.append(node('p',`元の場所：${r.source}\n退避先：${r.remaining_folder||r.quarantine}\nZIPの検証済みSHA-256が記録されている場合は復元に使えます。フォルダを重ねて上書きせず、復元先を別にして内容を確認してください。`,'archive-warning'));if(r.state==='completed')block.append(node('p','復元時はZIPを展開し、元のパスへ戻してから再調査してください。AI作業の再開前に内容・必要なアクセス権・起動方法を確認してください。'));block.append(node('p',`更新：${r.updated_at}`));$('archive-status').append(block);}const p=state.projects.find(p=>p.id===selected.id);const disabled=records.some(r=>archiveLive.has(r.state))||(state.moves||[]).some(r=>moveLive.has(r.state))||!p?.observation.exists;$('archive-preview').disabled=disabled;$('delete-preview').disabled=disabled;}
function schedulePoll(){clearTimeout(pollTimer);if((state.archives||[]).some(r=>archiveLive.has(r.state))||(state.moves||[]).some(r=>moveLive.has(r.state)))pollTimer=setTimeout(()=>load().catch(e=>{message(e.message,true);pollTimer=setTimeout(()=>load().catch(e=>message(e.message,true)),5000);}),2000);}
async function prepareRemoval(operation){
 const project=selected;const feedback=$('archive-feedback');feedback.className='';feedback.textContent='対象ファイルとGitHub公開先を確認しています…';$('archive-preview').disabled=true;$('delete-preview').disabled=true;
 try{
  const manual=$('github-repositories').value.split(/[\n,]+/).map(s=>s.trim()).filter(Boolean);
  const request=await api(`/api/ledger/${operation}/preview`,{id:project.id,github_repositories:manual});
  let result;
  for(let attempt=0;attempt<900;attempt++){
   result=await api('/api/ledger/preview-status?id='+encodeURIComponent(request.request_id));
   if(result.status!=='working')break;
   await new Promise(resolve=>setTimeout(resolve,1000));
   if(selected?.id!==project.id||$('editor').hidden)return;
  }
  if(result.status==='failed')throw Error(result.error);
  if(result.status!=='ready')throw Error('対象確認に時間がかかっています。対象のアプリを停止してから、確認をやり直してください。');
  if(selected?.id!==project.id||$('editor').hidden)return;
  archivePreview=result.preview;
  $('archive-confirm-title').textContent=operation==='delete'?'プロジェクトを削除（復旧用ZIPを保存）':'アーカイブして元フォルダを削除';
  $('archive-form').querySelector('button[type="submit"]').textContent=operation==='delete'?'非公開化・接続解除して削除':'非公開化・接続解除してアーカイブ';
  $('archive-plan').replaceChildren();
  for(const[k,v]of Object.entries({'対象プロジェクト':project.name,'削除する元フォルダ':archivePreview.source,'復旧用ZIP保存先':archivePreview.archive,'ファイル数':archivePreview.files.toLocaleString(),'追加ストリーム数':archivePreview.stream_count.toLocaleString(),'元データ容量':sizeText(archivePreview.bytes),'空き容量':sizeText(archivePreview.free_bytes)}))$('archive-plan').append(node('p',`${k}：${v}`,'path'));
  const github=archivePreview.github_plan;
  $('archive-plan').append(node('h3','GitHubの変更対象'));
  if(!github.repositories.length)$('archive-plan').append(node('p','GitHub公開先は検出されませんでした。過去に公開したリポジトリがある場合は戻って入力してください。'));
  for(const repo of github.repositories){const line=node('p');const link=node('a','https://github.com/'+repo.repository);link.href='https://github.com/'+repo.repository;link.target='_blank';link.rel='noopener';line.append(link,node('span',repo.private?' ／ 非公開を確認':' ／ 非公開に変更'));$('archive-plan').append(line);}
  $('archive-plan').append(node('p','解除するGit remote：'+([...new Set(github.refs.map(r=>r.remote))].join('、')||'対象なし')));
  $('archive-form').reset();$('archive-message').textContent='確認は15分間有効です。途中で失敗した場合、ローカルの削除を止めます。';feedback.textContent='対象確認が完了しました。確認画面を表示しています。';$('archive-dialog').showModal();
 }catch(e){feedback.textContent=e.message;feedback.className='error';$('archive-section').open=true;const relations=projectRelations(project);if(relations.gitPath){feedback.append(node('p','所属する親Gitプロジェクト：'+relations.gitPath,'path'));if(relations.gitParent){const button=node('button','親Gitプロジェクトの詳細を開く');button.type='button';button.onclick=()=>navigateProject(relations.gitParent);feedback.append(button);}else feedback.append(node('p','この親は台帳未登録です。「パスを指定して登録」で上記のパスを登録してください。'));}}finally{refreshArchive();}
}
$('archive-preview').onclick=()=>prepareRemoval('archive');
$('delete-preview').onclick=()=>prepareRemoval('delete');
$('archive-close').onclick=()=>$('archive-dialog').close();
$('archive-form').onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;try{const fields=new FormData(e.target);await api(`/api/ledger/${archivePreview.operation}/start`,{token:archivePreview.token,confirmed_path:archivePreview.source,confirmed_delete:fields.has('confirmed_delete'),closed_apps:fields.has('closed_apps'),github_reviewed:fields.has('github_reviewed')});$('archive-dialog').close();message('処理を開始しました。詳細画面に進捗と結果を表示します。');await load();}catch(err){$('archive-message').textContent=err.message;}finally{button.disabled=false;}};
const moveLive=new Set(['preparing','moving']);
function refreshMoves(){
 if(!selected)return;
 const records=(state.moves||[]).filter(r=>r.project_id===selected.id||r.affected.some(p=>p.id===selected.id)||r.references.some(p=>p.id===selected.id));
 const box=$('move-status');box.replaceChildren();
 for(const r of records){const section=node('div',undefined,'archive-record');section.append(node('strong',r.state==='completed'?'移動完了':r.state==='needs_recovery'?'移動の復旧確認が必要':r.state==='failed'?'移動中止':'移動処理中'),node('p',r.message),node('p',`移動元：${r.source}\n移動先：${r.destination}`,'path'),node('p',`更新：${r.updated_at}`));if(r.backup)section.append(node('p','台帳バックアップ：data/'+r.backup,'path'));box.append(section);}
 const p=state.projects.find(p=>p.id===selected.id);
 if(p&&(p.path!==selected.path||records.some(r=>r.state==='completed')&&p.manual_updated_at!==selected.manual_updated_at)){
  selected=p;$('path').textContent=p.path;renderRelations(p);renderMetadata(p);
  for(const key of Object.keys(labels)){const input=$('fields').querySelector(`[name="${key}"]`);if(input)input.value=p[key]||'';}
 }
 const moving=records.some(r=>moveLive.has(r.state));
 for(const input of $('fields').querySelectorAll('input,textarea'))input.disabled=moving;
 $('edit-form').querySelector('button[type="submit"]').disabled=moving;
 updateSectionIndicators();
 $('move-preview').disabled=!p?.observation.exists||(state.moves||[]).some(r=>moveLive.has(r.state))||(state.archives||[]).some(r=>archiveLive.has(r.state));
}
$('move-preview').onclick=async()=>{
 const project=selected,feedback=$('move-feedback');feedback.className='';feedback.textContent='移動先とGit・台帳への影響を確認しています…';$('move-preview').disabled=true;
 try{
  if(Object.keys(labels).some(key=>String(new FormData($('edit-form')).get(key)||'')!==String(project[key]||'')))throw Error('管理項目に未保存の入力があります。台帳に保存してから移動してください。');
  const request=await api('/api/ledger/move/preview',{id:project.id,destination:$('move-destination').value.trim()});let result;
  for(let attempt=0;attempt<900;attempt++){result=await api('/api/ledger/move/preview-status?id='+encodeURIComponent(request.request_id));if(result.status!=='working')break;await new Promise(resolve=>setTimeout(resolve,1000));if(selected?.id!==project.id||$('editor').hidden)return;}
  if(result.status==='failed')throw Error(result.error);
  if(result.status!=='ready')throw Error('対象確認が終わっていません。時間を置いてもう一度確認してください。');
  if(selected?.id!==project.id||$('editor').hidden)return;
  movePreview=result.preview;const box=$('move-plan');box.replaceChildren();
  for(const[k,v]of Object.entries({'移動元':movePreview.source,'移動先':movePreview.destination,'所属Git':movePreview.git_root||'検出なし','移動先の所属Git':movePreview.destination_git_root||'検出なし','Git追跡ファイル':movePreview.tracked_files+'件','移動範囲のGit差分':movePreview.git_changes+'件','台帳で移るノード':movePreview.affected.length+'件','更新する元ソース参照':movePreview.references.length+'件'}))box.append(node('p',k+'：'+v,'path'));
  for(const warning of movePreview.warnings)box.append(node('p',warning,'archive-warning'));
  const details=node('details'),summary=node('summary','移動する台帳ノードと参照先を確認');details.append(summary);
  for(const p of movePreview.affected)details.append(node('p',p.name+'\n'+p.source+' → '+p.destination,'path'));
  for(const p of movePreview.references)details.append(node('p','元ソース参照を更新：'+p.name));box.append(details);
  $('move-form').reset();$('move-message').textContent='確認は15分間有効です。Gitや台帳が変わった場合は実行直前に停止します。';feedback.textContent='対象確認が完了しました。確認画面でチェックを入れ、「この場所へ移動を実行」を押してください。';$('move-dialog').showModal();
 }catch(e){feedback.textContent=e.message;feedback.className='error';$('move-section').open=true;}finally{refreshMoves();}
};
$('move-close').onclick=()=>$('move-dialog').close();
$('move-form').onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;try{const fields=new FormData(e.target);await api('/api/ledger/move/start',{token:movePreview.token,confirmed_source:movePreview.source,confirmed_destination:movePreview.destination,closed_apps:fields.has('closed_apps'),confirmed_move:fields.has('confirmed_move'),git_reviewed:fields.has('git_reviewed')});$('move-dialog').close();$('move-feedback').textContent='移動を開始しました。進捗は詳細画面に表示します。';await load();}catch(err){$('move-message').textContent=err.message;}finally{button.disabled=false;}};
load().catch(e=>message(e.message,true));

$('list').addEventListener('click',e=>{const button=e.target.closest('button[data-project-id]');if(!button)return;try{openProjectDetails(state.projects.find(p=>p.id===button.dataset.projectId));}catch(err){const error=node('p','詳細画面を開けません：'+err.message,'error');button.parentElement.append(error);message(error.textContent,true);}});

// Keep every field mounted: switching tabs preserves unsaved drafts.
function selectLedgerTab(tab){
 const groups={basic:['basic'],execution:['source','verification','next'],harness:['harness'],observations:[],management:[]};
 for(const b of document.querySelectorAll('[data-ledger-tab]')){const on=b.dataset.ledgerTab===tab;b.setAttribute('aria-selected',String(on));b.tabIndex=on?0:-1;b.setAttribute('aria-controls',b.dataset.ledgerTab==='observations'?'metadata-section relationship-section':b.dataset.ledgerTab==='management'?'ledger-danger':groups[b.dataset.ledgerTab].map(id=>'management-'+id).join(' '));}
 for(const [id] of managementGroups){const pane=$('management-'+id);if(pane){pane.hidden=!groups[tab].includes(id);if(!pane.hidden)pane.open=true;}}
 $('management-section').hidden=!groups[tab].length;$('management-section').open=true;
 $('metadata-section').hidden=$('relationship-section').hidden=tab!=='observations';
 $('ledger-danger').hidden=tab!=='management';
}
for(const b of document.querySelectorAll('[data-ledger-tab]')){
 b.onclick=()=>selectLedgerTab(b.dataset.ledgerTab);
 b.onkeydown=e=>{const all=[...document.querySelectorAll('[data-ledger-tab]')],i=all.indexOf(b);let next;
 if(e.key==='ArrowRight')next=all[(i+1)%all.length];else if(e.key==='ArrowLeft')next=all[(i+all.length-1)%all.length];else if(e.key==='Home')next=all[0];else if(e.key==='End')next=all.at(-1);
 if(next){e.preventDefault();selectLedgerTab(next.dataset.ledgerTab);next.focus();}};
}
