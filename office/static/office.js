
'use strict';
(async()=>{
const sceneResponse=await fetch('/scene.json',{cache:'no-store'});
if(!sceneResponse.ok)throw new Error('장면 조회 실패');
const sceneData=await sceneResponse.json();
const D=sceneData,$=id=>document.getElementById(id),P=D.palette,W=D.width||1600,H=D.height||1000,world=$('world'),map=$('map'),ctx=map.getContext('2d'),art=createPixelArt(D),mobile=matchMedia('(max-width:900px)'),reduced=matchMedia('(prefers-reduced-motion:reduce)'),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
for(const k of ['ink','paper','accent','muted','line'])document.documentElement.style.setProperty('--'+k,P[k]);document.documentElement.style.setProperty('--ground',P.grass);document.documentElement.style.setProperty('--soft',D.ground==='night'?'#263b3c':'#edf1e6');document.documentElement.style.setProperty('--on-accent',D.ground==='night'?'#243c37':'#fff');$('brand-name').textContent=D.name;$('brand-caption').textContent='역할 '+D.people.length+'개 · 로컬 상태 · 10초마다 갱신';
const player={id:'me',name:'나',x:D.spawn.x,y:D.spawn.y,shirt:'#70b957',hair:'#202b27',appearance:{hairstyle:'long',outfit:'jacket',accessory:'glasses',headwear:'cap',skin:'#efc8ac',trousers:'#89bc65',shoes:'#385c38'},walking:false,face:'down',path:[]},actors=D.people.map((p,i)=>({...p,id:i,status:'확인 불가',engine:'',updated:null,walking:false,face:'down'})),camera={x:0,y:0},keys=new Set();let S=1,cw=1,ch=1,fitMode=innerWidth>600,last=0,selected=null,hovered=null,near=null,toastTimer,follow=false,previousFocus=null;
const propSize={desk:[64,38],table:[80,48],sofa:[76,38],bookshelf:[64,38],counter:[96,44],tree:[64,80],bamboo:[40,96],pine:[56,90],plant:[28,38],rug:[80,50],flower:[24,24],server:[30,48],lamp:[18,46],board:[50,44],meetingboard:[144,80],chair:[24,30],bench:[64,28],coffee:[44,48],campfire:[56,48],crate:[32,30],umbrella:[80,72],fountain:[64,60],bike:[50,30]};
const contains=(b,x,y,pad=0)=>x>b.x-pad&&x<b.x+b.w+pad&&y>b.y-pad&&y<b.y+b.h+pad;
const blocks=[];D.rooms.forEach(r=>{if(r.style==='open')return;blocks.push({x:r.x,y:r.y,w:r.w,h:22},{x:r.x,y:r.y,w:8,h:r.h},{x:r.x+r.w-8,y:r.y,w:8,h:r.h});if(r.style!=='tent')blocks.push({x:r.x,y:r.y+r.h-10,w:(r.w-64)/2,h:10},{x:r.x+(r.w+64)/2,y:r.y+r.h-10,w:(r.w-64)/2,h:10})});
D.props.forEach(p=>{let[w,h]=propSize[p.type]||[0,0];w=p.w||w;h=p.h||h;if(['rug','flower'].includes(p.type))return;if(['tree','pine','bamboo','umbrella'].includes(p.type))blocks.push({x:p.x+w*.4,y:p.y+h*.72,w:w*.22,h:h*.22});else if(p.type==='lamp')blocks.push({x:p.x+6,y:p.y+h-10,w:8,h:10});else blocks.push({x:p.x+3,y:p.y+Math.min(15,h/3),w:w-6,h:h-Math.min(15,h/3)})});
function walkable(x,y){if(x<12||y<18||x>W-12||y>H-12)return false;if(blocks.some(b=>contains(b,x,y,3)))return false;if(D.water.some(b=>contains(b,x,y))&&!D.paths.some(b=>contains(b,x,y))&&!D.rooms.some(b=>contains(b,x,y)))return false;return true}
const tickHooks=new Set(),step=16,cols=Math.ceil(W/step),rows=Math.ceil(H/step);function nearestCell(x,y){const cx=Math.round((x-8)/step),cy=Math.round((y-8)/step);let best=-1,dist=Infinity;for(let j=-6;j<=6;j++)for(let i=-6;i<=6;i++){const xx=cx+i,yy=cy+j;if(xx<0||yy<0||xx>=cols||yy>=rows)continue;const px=xx*16+8,py=yy*16+8,d=Math.hypot(px-x,py-y);if(d<dist&&walkable(px,py)){dist=d;best=yy*cols+xx}}return best}
let reachable=null;function reachableCells(){if(reachable)return reachable;reachable=new Uint8Array(cols*rows);const s=nearestCell(D.spawn.x,D.spawn.y);if(s<0)return reachable;const q=[s];reachable[s]=1;for(let i=0;i<q.length;i++){const n=q[i],xx=n%cols,yy=Math.floor(n/cols);for(const [dx,dy]of[[1,0],[-1,0],[0,1],[0,-1]]){const nx=xx+dx,ny=yy+dy,next=ny*cols+nx;if(nx<0||ny<0||nx>=cols||ny>=rows||reachable[next]||!walkable(nx*16+8,ny*16+8))continue;reachable[next]=1;q.push(next)}}return reachable}
function nearestReachable(x,y){const r=reachableCells(),cx=Math.round((x-8)/step),cy=Math.round((y-8)/step);let best=-1,dist=Infinity;for(let j=-8;j<=8;j++)for(let i=-8;i<=8;i++){const xx=cx+i,yy=cy+j;if(xx<0||yy<0||xx>=cols||yy>=rows)continue;const c=yy*cols+xx;if(!r[c])continue;const d=Math.hypot(xx*16+8-x,yy*16+8-y);if(d<dist){dist=d;best=c}}return best}
function routeFrom(x0,y0,x,y){const start=nearestReachable(x0,y0),end=nearestReachable(x,y);if(start<0||end<0)return[];const seen=new Int32Array(cols*rows).fill(-1),q=[start];seen[start]=start;for(let i=0;i<q.length;i++){const n=q[i];if(n===end)break;const xx=n%cols,yy=Math.floor(n/cols);for(const [dx,dy]of[[1,0],[-1,0],[0,1],[0,-1]]){const nx=xx+dx,ny=yy+dy,next=ny*cols+nx;if(nx<0||ny<0||nx>=cols||ny>=rows||seen[next]!==-1||!walkable(nx*16+8,ny*16+8))continue;seen[next]=n;q.push(next)}}if(seen[end]===-1)return[];const path=[];for(let n=end;n!==start;n=seen[n])path.push({x:n%cols*16+8,y:Math.floor(n/cols)*16+8});return path.reverse()}
function routeTo(x,y){return routeFrom(player.x,player.y,x,y)}
if(!walkable(player.x,player.y)){const n=nearestCell(player.x,player.y);if(n>=0){player.x=n%cols*16+8;player.y=Math.floor(n/cols)*16+8}}
function center(x=player.x,y=player.y){camera.x=x-cw/(2*S);camera.y=y-ch/(2*S);clampCamera()}
function clampCamera(){camera.x=cw/S>=W?(W-cw/S)/2:Math.max(-30,Math.min(W-cw/S+30,camera.x));camera.y=ch/S>=H?(H-ch/S)/2:Math.max(-30,Math.min(H-ch/S+30,camera.y))}
function resize(){world.inert=mobile.matches&&!$('side').hidden;const r=world.getBoundingClientRect();cw=r.width;ch=r.height;const dpr=Math.min(2,devicePixelRatio||1);map.width=Math.round(cw*dpr);map.height=Math.round(ch*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);ctx.imageSmoothingEnabled=false;if(fitMode){S=Math.min(cw/W,ch/H);center(W/2,H/2)}else if(follow)center();else clampCamera();$('zoom-value').textContent=Math.round(S*100)+'%';draw(performance.now())}
function zoomAt(value,x=cw/2,y=ch/2){const wx=camera.x+x/S,wy=camera.y+y/S;S=Math.max(.24,Math.min(2.8,value));fitMode=false;follow=false;camera.x=wx-x/S;camera.y=wy-y/S;clampCamera();$('zoom-value').textContent=Math.round(S*100)+'%'}
D.rooms.forEach(r=>{const b=document.createElement('button');b.className='room-label';b.innerHTML='<span>'+esc(r.name)+'</span>';b.setAttribute('aria-label',r.name+' 부서 보기');b.dataset.room=r.id;$('labels').append(b);r.button=b});
const boards=D.props.filter(p=>p.type==='meetingboard');boards.forEach(p=>{const b=document.createElement('button');b.className='board-control';b.dataset.meeting='all';b.setAttribute('aria-label','중앙 보드: 대화와 회의 요약 보기');b.innerHTML='<span>대화·회의 요약</span>';$('labels').append(b);p.button=b});
function labelAt(e,x,y){const px=(x-camera.x)*S,py=(y-camera.y)*S;e.style.left=px+'px';e.style.top=py+'px';const hide=S<.43||px<20||px>cw-20||py<20||py>ch-25;e.hidden=hide;e.tabIndex=hide?-1:0}
function drawTask(a,x,y,bubbles,color){
	if(!['작업 중','오류'].includes(a.status)||!a.task)return;
	const limit=hovered===a||selected===a?40:14,chars=Array.from(a.task);
	const text=S<.55?'…':chars.length>limit?chars.slice(0,limit-1).join('')+'…':a.task;
	const w=Math.max(22,ctx.measureText(text).width+16),h=S<.55?18:24,tail=5,left=x-w/2,right=x+w/2;
	let top=y-20-h-tail;
	const overlaps=()=>bubbles.some(b=>left<b.right&&right>b.left&&top<b.bottom&&top+h+tail>b.top);
	for(let level=0;level<2&&overlaps();level++)top-=h+tail+4;
	if(left<0||right>cw||top<0||top+h+tail>ch||overlaps())return;
	bubbles.push({left,right,top,bottom:top+h+tail});
	ctx.fillStyle=P.paper;ctx.strokeStyle=color;ctx.lineWidth=1;
	ctx.beginPath();ctx.moveTo(left+5,top);ctx.lineTo(right-5,top);ctx.quadraticCurveTo(right,top,right,top+5);
	ctx.lineTo(right,top+h-5);ctx.quadraticCurveTo(right,top+h,right-5,top+h);
	ctx.lineTo(x+4,top+h);ctx.lineTo(x,top+h+tail);ctx.lineTo(x-4,top+h);
	ctx.lineTo(left+5,top+h);ctx.quadraticCurveTo(left,top+h,left,top+h-5);
	ctx.lineTo(left,top+5);ctx.quadraticCurveTo(left,top,left+5,top);ctx.closePath();ctx.fill();ctx.stroke();
	ctx.fillStyle=S<.55?P.muted:P.ink;ctx.fillText(text,x,top+h/2+4);
}
function draw(t){
	ctx.fillStyle=P.grass;ctx.fillRect(0,0,cw,ch);ctx.save();ctx.scale(S,S);ctx.translate(-camera.x,-camera.y);ctx.drawImage(art.background,0,0);
	const sceneTime=reduced.matches?0:t;
	[...actors,player].sort((a,b)=>a.y-b.y).forEach(a=>art.drawActor(ctx,a,sceneTime));
	if(player.path.length){const d=player.path[player.path.length-1];ctx.strokeStyle=P.accent;ctx.lineWidth=2;ctx.strokeRect(d.x-5,d.y-4,10,7)}
	ctx.restore();D.rooms.forEach(r=>labelAt(r.button,r.labelX??r.x+r.w/2,r.labelY??r.y+r.h-26));
	boards.forEach(p=>{labelAt(p.button,p.x+72,p.y+40);p.button.style.width=Math.max(90,144*S)+'px';p.button.style.height=Math.max(56,80*S+20)+'px';p.button.hidden=(p.x+144<camera.x||p.x>camera.x+cw/S||p.y+100<camera.y||p.y>camera.y+ch/S)});
	const bubbles=[];
	[player,...actors].forEach(a=>{
		const x=(a.x-camera.x)*S,y=(a.y-camera.y)*S-54*S;if(x<0||x>cw||y<0||y>ch)return;
		ctx.font='600 11px "Apple SD Gothic Neo",sans-serif';
		const text=a.name,w=Math.max(26,ctx.measureText(text).width+14);
		ctx.fillStyle=a.id==='me'?P.accent:P.paper;ctx.fillRect(x-w/2,y-15,w,17);
		ctx.fillStyle=a.id==='me'?(D.ground==='night'?'#243c37':'#fff'):P.ink;ctx.textAlign='center';ctx.fillText(text,x,y-3);
		if(a.id!=='me'){
			const color=a.status==='오류'?'#dd6d76':a.status==='작업 중'?'#5d9973':a.status==='쉬는 중'?'#a07bc2':'#8f9fb1';
			ctx.fillStyle=color;ctx.fillRect(x-w/2-4,y-15,4,17);drawTask(a,x,y,bubbles,color);
		}
	});
	D.props.filter(p=>p.spare===true).forEach(p=>{const x=(p.x+(p.w||64)/2-camera.x)*S,y=(p.y+(p.h||0)/2-camera.y)*S-10;ctx.font='600 10px sans-serif';ctx.textAlign='center';ctx.fillStyle=P.paper;ctx.fillRect(x-24,y-12,48,16);ctx.fillStyle=P.muted;ctx.fillText('여유 자리',x,y)});
	const r=D.rooms.find(r=>contains(r,player.x,player.y));$('location').textContent=(r?.name||'공용 산책로')+' · '+(mobile.matches?'바닥을 눌러 이동':'클릭 / 방향키로 이동');
}
function tick(t){const dt=Math.min(.04,(t-last)/1000||0);last=t;tickHooks.forEach(fn=>fn(t,dt));player.walking=false;if(!(mobile.matches&&document.body.classList.contains('panel-open'))){const dx=(keys.has('arrowright')||keys.has('d')?1:0)-(keys.has('arrowleft')||keys.has('a')?1:0),dy=(keys.has('arrowdown')||keys.has('s')?1:0)-(keys.has('arrowup')||keys.has('w')?1:0);if(dx||dy){player.path=[];const norm=Math.hypot(dx,dy),mx=dx/norm*105*dt,my=dy/norm*105*dt;if(walkable(player.x+mx,player.y))player.x+=mx;if(walkable(player.x,player.y+my))player.y+=my;player.face=Math.abs(dx)>Math.abs(dy)?dx<0?'left':'right':dy<0?'up':'down';player.walking=true;follow=!fitMode}else if(player.path.length){const p=player.path[0],vx=p.x-player.x,vy=p.y-player.y,d=Math.hypot(vx,vy),amount=105*dt;if(d<=amount){player.x=p.x;player.y=p.y;player.path.shift()}else{player.x+=vx/d*amount;player.y+=vy/d*amount}player.face=Math.abs(vx)>Math.abs(vy)?vx<0?'left':'right':vy<0?'up':'down';player.walking=true}if(follow)center();}near=actors.filter(a=>Math.hypot(a.x-player.x,a.y-player.y)<64).sort((a,b)=>Math.hypot(a.x-player.x,a.y-player.y)-Math.hypot(b.x-player.x,b.y-player.y))[0]||null;$('nearby').hidden=!near;if(near)$('nearby').textContent=near.name+' 살펴보기';if(document.visibilityState!=='hidden')draw(t);requestAnimationFrame(tick)}
let press=null,moved=false,pinchDistance=0;const pointers=new Map();map.addEventListener('pointerdown',e=>{map.focus({preventScroll:true});map.setPointerCapture(e.pointerId);pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});if(pointers.size===1){press={x:e.clientX,y:e.clientY,lastX:e.clientX,lastY:e.clientY};moved=false}else{moved=true;const a=[...pointers.values()];pinchDistance=Math.hypot(a[0].x-a[1].x,a[0].y-a[1].y)}});
map.addEventListener('pointermove',e=>{const b=map.getBoundingClientRect();if(pointers.has(e.pointerId)){pointers.set(e.pointerId,{x:e.clientX,y:e.clientY});if(pointers.size===2){const a=[...pointers.values()],d=Math.hypot(a[0].x-a[1].x,a[0].y-a[1].y);if(pinchDistance)zoomAt(S*d/pinchDistance,(a[0].x+a[1].x)/2-b.left,(a[0].y+a[1].y)/2-b.top);pinchDistance=d;return}if(press){if(Math.hypot(e.clientX-press.x,e.clientY-press.y)>6)moved=true;if(moved){camera.x-=(e.clientX-press.lastX)/S;camera.y-=(e.clientY-press.lastY)/S;fitMode=false;follow=false;clampCamera()}press.lastX=e.clientX;press.lastY=e.clientY}}else{const x=camera.x+(e.clientX-b.left)/S,y=camera.y+(e.clientY-b.top)/S;hovered=actors.find(a=>Math.abs(a.x-x)<18&&y>a.y-54&&y<a.y+7)||null;map.style.cursor=hovered?'pointer':'crosshair'}});
map.addEventListener('pointerup',e=>{pointers.delete(e.pointerId);if(!moved&&press){const b=map.getBoundingClientRect(),x=camera.x+(e.clientX-b.left)/S,y=camera.y+(e.clientY-b.top)/S;const a=actors.find(a=>Math.abs(a.x-x)<20&&y>a.y-54&&y<a.y+9);const board=boards.find(p=>contains({...p,w:144,h:80},x,y));if(board)showMeeting();else if(D.rooms.some(r=>r.id==='meeting'&&contains(r,x,y)))document.dispatchEvent(new CustomEvent('office-meeting-room',{detail:{x:e.clientX,y:e.clientY}}));else if(a)showPerson(a.id);else{player.path=routeTo(x,y);if(!player.path.length&&Math.hypot(x-player.x,y-player.y)>26)toast('빈 바닥이나 연결된 길을 눌러 주세요.')}}press=null;if(!pointers.size)pinchDistance=0});map.addEventListener('pointercancel',()=>{pointers.clear();press=null;pinchDistance=0});map.addEventListener('pointerleave',()=>hovered=null);map.addEventListener('wheel',e=>{if(!e.ctrlKey&&!e.metaKey)return;e.preventDefault();const b=map.getBoundingClientRect();zoomAt(S*Math.exp(-Math.max(-100,Math.min(100,e.deltaY))*.004),e.clientX-b.left,e.clientY-b.top)},{passive:false});
window.addEventListener('keydown',e=>{if($('action-dialog').open||$('dev-dialog').open||$('blog-dialog').open||['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName))return;const k=e.key.toLowerCase();if(e.key==='Escape'){closePanel();return}if(['arrowup','arrowdown','arrowleft','arrowright','w','a','s','d'].includes(k)&&!(mobile.matches&&document.body.classList.contains('panel-open'))){keys.add(k);e.preventDefault()}if(e.code==='Space'&&near&&e.target===map){e.preventDefault();showPerson(near.id)}});window.addEventListener('keyup',e=>keys.delete(e.key.toLowerCase()));window.addEventListener('blur',()=>keys.clear());
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,4000)}
let currentPanel=()=>overview(),refreshing=false,state=null;
let officeToken=null,applying=false,chosenAction=null,actionFocus=null;
let devBusy=false,devDraft=null,devFocus=null,devOrigin=null;
const filterHelp={all:'아래는 모두 사용자의 승인이나 답을 기다리는 일입니다.',c4:'지난 AI 대화를 정리한 노트를 볼트에 써도 되는지 승인합니다.',apply:'AI가 만들고 검토까지 마친 코드 변경을 저장소에 반영해도 되는지 승인합니다.',r1:'볼트 노트의 머리말(종류·공개 범위·날짜) 정리를 폴더 단위로 승인합니다.',draft:'기획 AI가 쓴 개발 지시 초안을 보고 실행할지 확인합니다.',decision:'계획 문서에서 사용자의 선택을 기다리는 결정입니다.',question:'회의 AI가 정보가 모자라 묻는 질문입니다. 텔레그램에서도 답할 수 있습니다.'},actionFor={c4:'c4_apply',apply:'run_apply',r1:'r1_apply',draft:'dev_run'},devRoles={plan:'기획',backend:'백엔드',frontend:'프론트',design:'디자인'};
function panel(title,html,view='status'){selected=null;previousFocus=$('side').hidden?document.activeElement:previousFocus;$('panel-title').textContent=title;$('panel-body').innerHTML=html;$('side').hidden=false;document.body.classList.add('panel-open');if(!refreshing)$('panel-body').scrollTop=0;document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.view===view)));if(!refreshing)$('live').textContent=title+' 패널을 열었습니다';world.inert=mobile.matches;if(mobile.matches&&!refreshing)$('close-panel').focus()}
function closePanel(){selected=null;world.inert=false;$('side').hidden=true;document.body.classList.remove('panel-open');document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.view==='office')));previousFocus?.focus({preventScroll:true});keys.clear()}

const items=group=>Array.isArray(group?.items)?group.items:[];
const empty=message=>'<p class="muted">'+esc(message)+'</p>';
const note=group=>group?.status==='확인 불가'?empty('일부 자료를 확인할 수 없습니다.'):'';
const when=value=>value?new Date(value).toLocaleString('ko-KR',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'시각 미확인';
const stat=(label,value)=>'<div class="stat"><span>'+esc(label)+'</span><b>'+esc(value??'확인 불가')+'</b></div>';
function approvals(){
	return [['c4','대화 기록 정리',state?.approvals?.c4],['apply','AI 작업 결과',state?.approvals?.runs],['r1','볼트 노트 정리',state?.approvals?.r1],['draft','개발 지시 확인',state?.approvals?.drafts],['decision','결정할 일',state?.approvals?.decisions]]
		.flatMap(([kind,label,group])=>items(group).map(row=>({...row,kind,label,title:row.title||row.goal||[row.project,row.id].filter(Boolean).join(' · ')}))).concat(window.officeMeeting?.approvals()||[]);
}
const activityHTML=()=>state?'<ol class="timeline">'+(state.activity||[]).map(row=>'<li>'+esc(row.text)+'<time>'+esc(when(row.at))+'</time></li>').join('')+'</ol>':empty('활동 자료 조회 중');
function meter(label,value,max){
	if(!(max>0))return '';
	const current=Math.max(0,Math.min(value,max)),percent=current/max*100;
	return '<div class="meter" role="progressbar" aria-label="'+esc(label)+'" aria-valuenow="'+current+'" aria-valuemin="0" aria-valuemax="'+max+'"><i style="width:'+percent+'%"></i></div>';
}
function usageHTML(){
	const usage=state?.usage;
	if(!usage)return empty('사용량을 확인할 수 없습니다.');
	const c4=usage.c4,limit=usage.limit,plans=usage.plans;
	return '<h3>오늘 사용량</h3>'+stat('기록 정리 구독 AI 호출',c4?.status==='정상'?c4.subscription+'회':'확인 불가')
		+(c4?.status==='정상'&&limit?.status==='정상'?meter('기록 정리 구독 AI 호출',c4.subscription,limit.daily_limit_calls):'')
		+stat('기록 정리 하루 구독 한도',limit?.status==='정상'?limit.daily_limit_calls+'회':'확인 불가')
		+stat('기록 정리 로컬 AI 호출',c4?.status==='정상'?c4.local+'회':'확인 불가')
		+stat('매일 기록 호출',plans?.status==='정상'?items(plans).reduce((sum,row)=>sum+row.count,0)+'회':'확인 불가')
		+stat('NotebookLM',usage.notebooklm?.count==null?'미측정':usage.notebooklm.count+'회')+note(c4)+note(plans);
}
function approvalButtons(){
	return '<div class="approval-grid">'+[['c4','대화 기록 정리','c4'],['apply','AI 작업 결과','runs'],['r1','볼트 노트 정리','r1'],['draft','개발 지시 확인','drafts'],['decision','결정할 일','decisions']].map(([kind,label,key])=>'<button data-filter="'+kind+'"><b>'+(!state||state.approvals?.[key]?.status==='확인 불가'?'?':approvals().filter(a=>a.kind===kind).length)+'</b>'+label+'</button>').join('')+(window.officeMeeting?.summary()||'')+'</div>';
}
function scheduleHTML(list){
	let undated=false;
	if(!state)return empty('일정 자료를 확인할 수 없습니다.');
	return list.map(row=>{
		const heading=!row.due&&!undated?'<h3 class="section">언젠가</h3>':'';
		if(!row.due)undated=true;
		return heading+'<div class="schedule"><time title="'+esc(row.at||row.due||'기한 없음')+'">'+esc(row.at?new Date(row.at).toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit'}):row.due?row.due.slice(5).replace('-','/'):'미정')+'</time><div>'+esc(row.title)+'<small>'+esc(row.source)+(row.due?' · '+esc(row.due):'')+'</small></div></div>';
	}).join('')||empty('등록된 일정이 없습니다.');
}
function overview(){
	currentPanel=overview;
	panel('오늘의 오피스','<span class="state">'+actors.length+'개 역할 · '+actors.filter(a=>a.status==='작업 중').length+'개 작업 중</span>'
		+stat('자리',D.props.filter(p=>p.seat||p.spare).length+'개')
		+'<button class="outline-button" data-meeting="all">중앙 보드 · 대화와 회의 보기</button><button class="outline-button" data-directory="true">부서와 직원 찾아보기</button><button class="outline-button" data-characters="true">캐릭터 23명 한눈에 보기</button>'
		+'<div class="section"><h3>확인이 필요한 일</h3>'+approvalButtons()+'</div><div class="section">'+usageHTML()+'</div>'
		+'<div class="section"><h3>다음 일정</h3>'+scheduleHTML(items(state?.schedule).slice(0,3))+'</div><div class="section"><h3>방금 일어난 일</h3>'+activityHTML()+'</div>','office');
}
function showApprovals(filter='all'){
	currentPanel=()=>showApprovals(filter);
	const list=approvals().filter(row=>filter==='all'||row.kind===filter);
	panel('확인이 필요한 일','<p class="intro">'+(state?approvals().length+'건':'확인 불가')+' · '+(applying?'반영 중':'반영 전에 변경 내용을 확인합니다.')+'</p><div class="subtabs">'+[['all','전체'],['c4','기록 정리'],['apply','작업 결과'],['r1','노트 정리'],['draft','개발 지시'],['decision','결정'],['question','AI 질문']].map(([id,label])=>'<button data-filter="'+id+'" aria-pressed="'+(id===filter)+'">'+label+'</button>').join('')+'</div>'
		+'<p class="intro">'+esc(filterHelp[filter]||'')+'</p>'+(!officeToken?empty('반영 세션을 읽지 못했습니다. 화면을 다시 열어 주세요.'):'' )
		+(list.map(row=>'<div class="row"><div>'+esc(row.title)+'<small>'+esc(row.label)+(row.kind==='c4'?' · 볼트에 쓸 노트 '+esc(row.prepared)+'건 · 보류 '+esc(row.held)+'건':row.kind==='r1'?' · 노트 '+esc(row.pending)+'개':row.kind==='draft'?' · '+esc(devRoles[row.role])+' · 대상 저장소 '+esc(row.target)+' · 완료 조건 '+esc(row.done)+'개 · '+esc(when(row.created)):'')+'</small>'+(row.reply?'<code>'+esc(row.reply)+'</code>':'')+(row.kind==='draft'?'<code>'+esc(row.owned_paths.join(', '))+'</code><small>'+esc(devPlanText())+'</small>':'')+'</div>'+(row.kind==='question'?window.officeMeeting.button(row):actionFor[row.kind]?'<button class="apply-button" '+(row.kind==='draft'?'data-draft="'+esc(row.id):'data-action="'+row.kind)+'" data-id="'+esc(row.id)+'"'+(applying||devBusy||!officeToken?' disabled':'')+'>'+ (applying?'반영 중':row.kind==='draft'?'초안 보기':'반영')+'</button>':'<span class="state">대기</span>')+'</div>').join('')||empty(state?'대기 항목이 없습니다.':'할 일 자료를 확인할 수 없습니다.'))
		+Object.values(state?.approvals||{}).map(note).join(''),'approvals');
}
function openAction(kind,id,button){
	if(applying||devBusy||!officeToken||$('action-dialog').open)return;
	const row=approvals().find(item=>item.kind===kind&&item.id===id);
	if(!row||!actionFor[kind])return;
	if(kind==='draft'&&devDraft?.id!==id)return;
	chosenAction={action:actionFor[kind],id};actionFocus=button;keys.clear();
	$('action-title').textContent=row.label+' 반영 확인';
	const description=kind==='c4'?'지난 AI 대화를 정리한 노트 '+row.prepared+'건을 볼트에 씁니다. 보류 '+row.held+'건은 남습니다.\n기록: 볼트 노트·일지와 원장.\n되돌리기: 볼트 커밋이 생긴 뒤 해당 커밋을 되돌리고 원장 상태를 확인하세요.':kind==='r1'?'폴더의 노트 '+row.pending+'개에 머리말(종류·공개 범위·날짜)과 태그 정리를 반영합니다.\n기록: 로컬 볼트 커밋 한 개와 사무실 활동 기록.\n되돌리기: 해당 볼트 커밋을 되돌리세요.':'AI가 만들고 검토까지 마친 변경을 저장소에 한 커밋으로 반영합니다.\n기록: 로컬 squash 커밋 한 개와 볼트 일지.\n되돌리기: 해당 저장소 커밋을 되돌리세요.';
	$('action-description').textContent=id+'\n'+description;
	if(kind==='draft'){
		$('action-title').textContent='개발 지시 실행 확인';
		$('action-description').textContent=devDraft.goal+'\n대상 저장소: '+devDraft.target+'\n'+devPlanText()+'\n역할: '+devRoles[devDraft.role]+'\n범위: '+devDraft.owned_paths.join(', ')+'\n예산: '+budgetText(devDraft.budget)+'\n기획 AI: '+devDraft.plan+'\n'+devDraft.target+' 저장소에서 구현·검토를 백그라운드로 실행합니다. 반영은 따로 승인합니다.';
	}
	$('action-status').textContent='';$('action-result').hidden=true;$('action-details').open=false;
	$('action-confirm').hidden=false;$('action-confirm').disabled=false;$('action-confirm').textContent='반영';
	if(kind==='draft')$('action-confirm').textContent='실행';
	$('action-cancel').disabled=false;$('action-cancel').textContent='취소';
	$('action-dialog').showModal();$('action-cancel').focus();
}
function closeAction(){
	if(applying)return;
	$('action-dialog').close();chosenAction=null;
	if(actionFocus?.isConnected)actionFocus.focus({preventScroll:true});else $('close-panel').focus();
}
async function submitAction(){
	if(applying||!chosenAction||!officeToken)return;
	const running=chosenAction.action==='dev_run';
	applying=true;$('action-confirm').disabled=true;$('action-cancel').disabled=true;
	$('action-confirm').textContent=running?'실행 시작 중':'반영 중';$('action-status').textContent=(running?'실행 시작 중':'반영 중')+' · 결과를 기다려 주세요.';refreshPanel();
	try{
		const response=await fetch('/api/action',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-Office-Token':officeToken},body:JSON.stringify(chosenAction)});
		const data=await response.json();
		$('action-status').textContent=data.message||'반영 결과를 확인할 수 없습니다.';
		$('action-result').hidden=false;$('action-output').textContent=data.output||'출력이 없습니다.';
		$('action-restart').hidden=!data.restart_needed;
		$('action-restart').textContent=data.restart_needed?'사무실 재시작 필요: 터미널에서 launchctl kickstart -k gui/$(id -u)/com.$(id -un).ai-hub-office':'';
	}catch{
		$('action-status').textContent='반영 결과를 받지 못했습니다. 활동 기록과 상태를 확인한 뒤 다시 시도하세요.';
	}finally{
		applying=false;chosenAction=null;$('action-confirm').hidden=true;$('action-cancel').disabled=false;$('action-cancel').textContent='닫기';
		await loadState();$('action-cancel').focus();
	}
}
$('action-confirm').onclick=submitAction;$('action-cancel').onclick=closeAction;
$('action-dialog').addEventListener('cancel',e=>{e.preventDefault();closeAction()});
const budgetText=budget=>budget.calls+'회 호출 · '+budget.minutes+'분 · 보완 '+budget.fixes+'회';
const devTargets=()=>state?.dev_targets||[{id:'ai-hub',label:'ai-hub'}];
const devPlanText=()=>'구현 요금제: '+(state?.dev_plans?.implementer||'확인 불가')+' · 검토 요금제: '+(state?.dev_plans?.reviewer||'확인 불가');
function devControls(){
	for(const id of ['dev-role','dev-target','dev-text','dev-write','dev-close','dev-redraft','dev-discard','dev-run'])$(id).disabled=devBusy||applying||!officeToken;
	for(const id of ['dev-role','dev-target'])$(id).disabled=devBusy||applying||!officeToken||!!devDraft;
	$('dev-warning').textContent='회사 내용·비밀값은 적지 마세요. 기획 AI가 '+$('dev-target').value+' 저장소의 요청서 초안을 씁니다.';
	const length=Array.from($('dev-text').value||'').length;
	$('dev-remaining').textContent=(2000-length)+'자 남음';
	$('dev-write').disabled=devBusy||applying||!officeToken||length<1||length>2000||!$('dev-text').value.trim();
}
function renderDraft(value){
	devDraft=value;$('dev-editor').hidden=true;$('dev-card').hidden=false;
	$('dev-target').value=value.target;
	const list=values=>'<ul>'+values.map(item=>'<li>'+esc(item)+'</li>').join('')+'</ul>';
	$('dev-card').innerHTML='<span class="state">'+esc(devRoles[value.role])+' · '+esc(when(value.created))+'</span>'+stat('대상 저장소',value.target)+'<p class="dev-plans">'+esc(devPlanText())+'</p><h3>목표</h3><p>'+esc(value.goal)+'</p><h3>완료 조건</h3>'+list(value.done)+'<h3>고칠 파일 범위</h3>'+list(value.owned_paths)+'<h3>금지 사항</h3>'+list(value.forbidden)+'<h3>예산</h3><p>'+esc(budgetText(value.budget))+'</p>';
	for(const id of ['dev-run','dev-redraft','dev-discard'])$(id).hidden=false;
	devControls();
}
function openDev(role,button,blog=false){
	if(devBusy||applying||!officeToken||$('dev-dialog').open||$('action-dialog').open)return;
	devDraft=null;devFocus=button;devOrigin=Number(button?.dataset.origin);keys.clear();$('dev-title').textContent='개발 지시';$('dev-role').value=role;
	$('dev-target').innerHTML=devTargets().map(item=>'<option value="'+esc(item.id)+'">'+esc(item.label)+'</option>').join('');
	$('dev-target').value=(blog?devTargets().find(item=>item.id!=='ai-hub')?.id:null)||'ai-hub';
	$('dev-text').value='';$('dev-text-label').textContent='할 일';$('dev-status').textContent='';$('dev-write').textContent='초안 쓰기';
	$('dev-editor').hidden=false;$('dev-card').hidden=true;
	for(const id of ['dev-run','dev-redraft','dev-discard'])$(id).hidden=true;
	devControls();$('dev-dialog').showModal();$('dev-text').focus();
}
function closeDev(){
	if(devBusy)return;
	$('dev-dialog').close();if(devFocus?.isConnected)devFocus.focus({preventScroll:true});else $('close-panel').focus();
}
async function showDraft(id,button){
	if(devBusy||applying||!officeToken||$('dev-dialog').open||$('action-dialog').open)return;
	openDev('backend',button);if(!$('dev-dialog').open)return;
	devBusy=true;devControls();$('dev-status').textContent='초안 조회 중';
	const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),5000);
	try{
		const response=await fetch('/api/draft/'+encodeURIComponent(id),{cache:'no-store',signal:controller.signal}),data=await response.json();
		if(!response.ok||!data.ok)throw new Error();
		$('dev-role').value=data.draft.role;renderDraft(data.draft);$('dev-status').textContent='내용을 확인한 뒤 실행해 주세요.';
	}catch{$('dev-editor').hidden=true;$('dev-status').textContent='대기 중인 초안을 읽지 못했습니다. 상태를 다시 확인해 주세요.'}
	finally{clearTimeout(timeout);devBusy=false;devControls();$('dev-close').focus()}
}
async function submitDev(action){
	if(devBusy||applying||!officeToken)return;
	const payload={action,id:action==='dev_draft'?$('dev-role').value:devDraft?.id};
	if(action!=='dev_discard')payload.text=$('dev-text').value;
	if(action==='dev_draft'){payload.target=$('dev-target').value;payload.origin=devOrigin}
	devBusy=true;devControls();refreshPanel();$('dev-status').textContent=action==='dev_discard'?'버림으로 표시 중':'초안 쓰는 중(1~3분)';
	try{
		const response=await fetch('/api/action',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-Office-Token':officeToken},body:JSON.stringify(payload)}),data=await response.json();
		$('dev-status').textContent=data.message||'초안 결과를 확인할 수 없습니다.';
		if(response.ok&&data.ok){if(data.draft)renderDraft(data.draft);else{devBusy=false;closeDev();toast(data.message)}}
	}catch{$('dev-status').textContent='결과를 받지 못했습니다. 결재함을 다시 읽고 초안이 있는지 확인하세요.'}
	finally{devBusy=false;devControls();await loadState();if($('dev-dialog').open)$('dev-close').focus()}
}
$('dev-text').oninput=devControls;$('dev-target').onchange=devControls;$('dev-close').onclick=closeDev;
$('dev-dialog').addEventListener('cancel',e=>{e.preventDefault();closeDev()});
$('dev-write').onclick=()=>submitDev(devDraft?'dev_redraft':'dev_draft');
$('dev-discard').onclick=()=>devDraft&&submitDev('dev_discard');
$('dev-redraft').onclick=()=>{if(devBusy||!devDraft)return;$('dev-editor').hidden=false;$('dev-text').value='';$('dev-text-label').textContent='추가 지시';$('dev-write').textContent='초안 다시 쓰기';devControls();$('dev-text').focus()};
$('dev-run').onclick=()=>{if(devBusy||!devDraft)return;closeDev();openAction('draft',devDraft.id,$('close-panel'))};
async function loadSession(){
	const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),5000);
	try{
		const response=await fetch('/api/session',{cache:'no-store',signal:controller.signal});
		if(!response.ok)throw new Error('세션 조회 실패');
		const data=await response.json();officeToken=typeof data.token==='string'&&data.token?data.token:null;
	}catch{officeToken=null}finally{clearTimeout(timeout)}
}
function showSchedule(){
	currentPanel=showSchedule;
	panel('앞으로의 일정','<p class="intro">예약 · 요금제 일정 · 대화에서 남긴 후속 일</p>'+scheduleHTML(items(state?.schedule))+note(state?.schedule),'schedule');
}
function showStatus(kind='usage'){
	currentPanel=()=>showStatus(kind);
	const tabs='<div class="subtabs">'+[['usage','사용량'],['materials','자료 채우기'],['activity','활동']].map(([id,label])=>'<button data-status="'+id+'" aria-pressed="'+(kind===id)+'">'+label+'</button>').join('')+'</div>';
	const progress=state?.board?.progress;
	const rows=items(progress),applied=rows.reduce((sum,row)=>sum+row.applied,0),total=rows.reduce((sum,row)=>sum+row.total,0);
	const body=kind==='usage'?usageHTML():kind==='activity'?activityHTML():'<h3>지난 기록 채우기</h3>'+(progress?.status==='정상'?stat('전체 진행',applied+' / '+total+'건')+meter('자료 채우기 진행',applied,total):'')+(items(progress).map(row=>'<div class="row"><div>'+esc(row.project)+'<small>'+esc(row.kind==='sessions'?'대화':'문서·커밋')+'</small></div><span>'+esc(row.applied)+' / '+esc(row.total)+'건</span></div>').join('')||empty('아직 진행 기록이 없습니다.'))+note(progress);
	panel('오피스 현황',tabs+body);
}
function personRows(list){return list.map(a=>'<button class="row person-row" data-person="'+a.id+'"><span class="avatar" style="background:'+esc(a.shirt)+'">'+esc(a.name.slice(0,1))+'</span><span>'+esc(a.name)+'<small>'+esc(a.engine||'담당 없음')+'</small></span><span class="row-state">'+esc(a.status)+'</span></button>').join('')}
const validBlogPath=path=>typeof path==='string'&&path.startsWith('/')&&!path.startsWith('//')&&!/[\s"'<>\\]/.test(path);
const blogButton=(path,name,label='블로그 화면 열기')=>validBlogPath(path)?'<button class="outline-button" data-blog-path="'+esc(path)+'" data-blog-name="'+esc(name)+'">'+esc(label)+'</button>':'';
let blogFocus=null;
function openBlog(path,name,button){
	if(!validBlogPath(path)||$('blog-dialog').open||$('action-dialog').open||$('dev-dialog').open)return;
	const url='http://127.0.0.1:8765'+path,available=state?.blog_dashboard==='켜짐';
	$('blog-link').href=url;$('blog-title').textContent=name+' 블로그 화면';
	if(mobile.matches){if(available)$('blog-link').click();else toast('블로그 대시보드가 꺼져 있습니다');return}
	blogFocus=button;keys.clear();$('blog-body').replaceChildren();
	if(available){
		const frame=document.createElement('iframe');
		frame.setAttribute('sandbox','allow-scripts allow-forms allow-same-origin allow-popups allow-modals');
		frame.setAttribute('referrerpolicy','no-referrer');frame.title=name+' 블로그 화면';frame.src=url;
		$('blog-body').append(frame);
	}else $('blog-body').textContent='블로그 대시보드가 꺼져 있습니다';
	$('blog-dialog').showModal();$('blog-close').focus();
}
function closeBlog(){
	$('blog-body').replaceChildren();$('blog-dialog').close();
	if(blogFocus?.isConnected)blogFocus.focus({preventScroll:true});else $('close-panel').focus();
}
$('blog-close').onclick=closeBlog;$('blog-dialog').addEventListener('cancel',e=>{e.preventDefault();closeBlog()});
function showDepartment(id,focus=true){
	const r=D.rooms.find(r=>r.id===id),people=actors.filter(a=>a.department===id);
	if(!r)return;
	if(id==='meeting'){showMeeting();return}
	currentPanel=()=>showDepartment(id,false);
	panel(r.name,'<p class="intro">'+(id==='company'?'프로젝트 ID · 상태 · 시간':people.length?people.length+'개 역할':'공용 공간')+'</p>'+personRows(people)+(id==='blog'?blogButton('/',r.name,'블로그 대시보드 열기'):'')
		+(id==='engines'?(state?.engines||[]).map(engine=>'<div class="row"><span>'+esc(engine.name)+'</span><span class="state">'+esc(engine.status)+'</span></div>').join(''):''));
	if(focus&&!fitMode){center(r.x+r.w/2,r.y+r.h/2);follow=false}
}
function showPerson(id,focus=true){
	const a=actors[Number(id)];if(!a)return;
	currentPanel=()=>showPerson(id,false);
	const r=D.rooms.find(r=>r.id===a.department);
	const info=a.department==='company'?'<div class="row"><span>프로젝트</span><span>'+esc(a.project||'미지정')+'</span></div><div class="row"><span>작업 시간</span><span>'+esc(a.duration==null?'미측정':Math.floor(a.duration/60)+'분 '+a.duration%60+'초')+'</span></div>':'<div class="section"><h3>지금 하고 있는 일</h3><p class="intro">'+esc(a.task||'다음 작업 대기')+'</p></div>';
	panel(a.name,'<div class="detail-portrait"><canvas class="portrait" id="portrait" width="90" height="100"></canvas><div><h3>'+esc(a.name)+'</h3><p>'+esc(r?.name||'공용 공간')+'</p><span class="state">'+esc(a.status)+'</span></div></div><div class="row"><span>내 책상</span><span>'+a.seat+'번 자리</span></div>'+info+'<div class="row"><span>담당 AI</span><span>'+esc(a.engine||'담당 없음')+'</span></div><div class="row"><span>신호 시각</span><span>'+esc(when(a.updated))+'</span></div><button class="outline-button" data-room="'+a.department+'">부서 전체 보기</button>');
	selected=a;
	const role=Object.keys(devRoles).find(key=>devRoles[key]===a.name);
	if(a.department==='dev'&&role)$('panel-body').innerHTML+='<button class="outline-button" data-dev="'+role+'" data-origin="'+a.id+'"'+(devBusy||applying||!officeToken?' disabled':'')+'>개발 지시</button>';
	if(a.department==='blog')$('panel-body').innerHTML+='<button class="outline-button" data-dev="backend" data-team="blog" data-origin="'+a.id+'"'+(devBusy||applying||!officeToken?' disabled':'')+'>개발 지시</button>';
	$('panel-body').innerHTML+=blogButton(a.blog_path,a.name);
	const c=$('portrait').getContext('2d');c.imageSmoothingEnabled=false;c.scale(1.6,1.6);art.drawActor(c,{...a,x:28,y:56,walking:false},0);
	if(focus&&!fitMode){center(a.x,a.y);follow=false}
}
function showMeeting(filter='all'){
	currentPanel=()=>showMeeting(filter);
	const list=items(state?.board).filter(row=>filter==='all'||row.kind===filter);
	panel('회의실','<p class="intro">중앙 보드 · 최근 실행과 작업 기록의 결정</p><div class="subtabs">'+[['all','전체'],['meeting','회의'],['chat','대화']].map(([id,label])=>'<button data-meeting="'+id+'" aria-pressed="'+(id===filter)+'">'+label+'</button>').join('')+'</div>'
		+(list.map(row=>'<article class="record"><small><span class="tag">'+(row.kind==='meeting'?'실행':'대화')+'</span>'+esc(when(row.at))+'</small><h3>'+esc(row.title)+'</h3><p>'+esc(row.text)+'</p></article>').join('')||empty(state?'표시할 최근 기록이 없습니다.':'최근 기록을 확인할 수 없습니다.'))+note(state?.board));
}
function showCharacters(){currentPanel=showCharacters;const list=[{...player,identity:'검은 머리 · 초록 캡 · 안경'},...actors];panel('캐릭터 23명','<p class="intro">역할마다 고정된 헤어·의상·소품을 사용해요.</p><div class="character-grid">'+list.map((a,i)=>'<div class="character-card"><canvas width="100" height="108" data-character="'+i+'" aria-label="'+esc(a.name)+' 캐릭터"></canvas><b>'+esc(a.name)+'</b><small>'+esc(a.identity||'')+'</small></div>').join('')+'</div>');document.querySelectorAll('[data-character]').forEach(c=>{const a=list[Number(c.dataset.character)],cx=c.getContext('2d');cx.imageSmoothingEnabled=false;cx.scale(1.7,1.7);art.drawActor(cx,{...a,x:29,y:59,walking:false,face:'down'},0)})}
function showDirectory(){currentPanel=showDirectory;panel('부서 찾기','<p class="intro">부서나 직원을 선택해 살펴보세요.</p><button class="outline-button" data-characters="true" style="margin:0 0 16px">캐릭터 23명 한눈에 보기</button>'+D.rooms.map(r=>'<button class="row person-row" data-room="'+r.id+'"><span>'+esc(r.name)+'</span><span class="row-state">'+actors.filter(a=>a.department===r.id).length+'명</span></button>').join(''))}
function view(v){if(v==='office'){if(mobile.matches&&!$('side').hidden)closePanel();else overview()}else if(v==='approvals')showApprovals();else if(v==='schedule')showSchedule();else showStatus()}
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b||b.disabled)return;if(b.dataset.blogPath!==undefined)openBlog(b.dataset.blogPath,b.dataset.blogName,b);else if(b.dataset.dev)openDev(b.dataset.dev,b,b.dataset.team==='blog');else if(b.dataset.draft)showDraft(b.dataset.draft,b);else if(b.dataset.action)openAction(b.dataset.action,b.dataset.id,b);else if(b.dataset.characters)showCharacters();else if(b.dataset.meeting)showMeeting(b.dataset.meeting);else if(b.dataset.directory)showDirectory();else if(b.dataset.view)view(b.dataset.view);else if(b.dataset.filter)showApprovals(b.dataset.filter);else if(b.dataset.status)showStatus(b.dataset.status);else if(b.dataset.person!==undefined)showPerson(b.dataset.person);else if(b.dataset.room)showDepartment(b.dataset.room)});$('directory').onclick=showDirectory;$('close-panel').onclick=closePanel;$('nearby').onclick=()=>near&&showPerson(near.id);$('zoom-in').onclick=()=>zoomAt(S*1.25);$('zoom-out').onclick=()=>zoomAt(S/1.25);$('fit').onclick=()=>{fitMode=true;follow=false;resize()};$('me').onclick=()=>{fitMode=false;follow=true;S=mobile.matches?1.15:Math.max(1,S);resize()};

function refreshPanel(){
	if($('side').hidden)return;
	const body=$('panel-body'),scroll=body.scrollTop,buttons=[...body.querySelectorAll('button')],focus=buttons.indexOf(document.activeElement);
	refreshing=true;
	try{currentPanel();body.scrollTop=scroll;if(focus>=0)body.querySelectorAll('button')[focus]?.focus({preventScroll:true})}finally{refreshing=false}
}
async function loadState(){
	const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),5000);
	try{
		const response=await fetch('/api/state',{cache:'no-store',signal:controller.signal});
		if(!response.ok)throw new Error('상태 조회 실패');
		const data=await response.json();
		if(!Array.isArray(data.people)||data.people.length!==actors.length)throw new Error('역할 자료 확인 불가');
		state=data;
		actors.forEach(actor=>{
			const signal=data.people.find(person=>person.id===actor.id);
			if(!signal||!['작업 중','대기','쉬는 중','오류','확인 불가'].includes(signal.status))throw new Error('역할 상태 확인 불가');
			for(const key of ['status','engine','task','updated','project','duration'])if(Object.hasOwn(signal,key))actor[key]=signal[key];
			if(actor.department==='company')delete actor.task;
		});
		$('updated').textContent='갱신 '+when(data.updated);
		$('approval-count').textContent=Object.values(data.approvals||{}).some(group=>group.status==='확인 불가')?'?':approvals().length;
	}catch{
		state=null;
		actors.forEach(actor=>{actor.status='확인 불가';actor.engine='';actor.updated=null;if(actor.department!=='company')actor.task='상태를 읽을 수 없습니다.';else{actor.duration=null;actor.project=''}});
		$('updated').textContent='상태 조회 실패 · 다음 갱신 대기';$('approval-count').textContent='?';
	}finally{clearTimeout(timeout);document.dispatchEvent(new CustomEvent('office-state',{detail:state}));refreshPanel()}
}
window.officeBridge={state:()=>state,token:()=>officeToken,toast,esc,people:()=>D.people,openPanel(title,html){currentPanel=()=>panel(title,html);currentPanel()},refresh:()=>loadState(),
	actors:()=>actors,routeFrom,walkable,chairs:()=>{const room=D.rooms.find(r=>r.id==='meeting');return room?D.props.filter(p=>p.type==='chair'&&contains(room,p.x,p.y)):[]},
	tickHook(fn){tickHooks.add(fn);return()=>tickHooks.delete(fn)},reducedMotion:()=>reduced.matches,
	openDevWith(text,target){
		if(!officeToken||devBusy||applying||['dev-dialog','action-dialog','blog-dialog','meeting-dialog'].some(id=>$(id).open))return false;
		openDev('plan',document.activeElement);if(!$('dev-dialog').open)return false;
		devOrigin=(D.people.find(p=>p.name===devRoles.plan)||D.people[0]).id;
		$('dev-target').value=(devTargets().find(t=>t.id===target)||devTargets().find(t=>t.id==='ai-hub')||devTargets()[0]).id;
		$('dev-text').value=Array.from(text).slice(0,2000).join('');devControls();return true;
	}
};
document.dispatchEvent(new Event('office-ready'));
console.assert(actors.length===D.people.length&&D.props.filter(p=>p.seat||p.spare).length>=D.people.length,'역할과 자리 수 확인');
console.assert(actors.filter(a=>a.department==='company').every(a=>!a.task),'회사 역할의 내용 숨김');
if(!mobile.matches)overview();else{S=1.15;follow=true}
new ResizeObserver(resize).observe(world);resize();requestAnimationFrame(tick);
await loadSession();await loadState();setInterval(loadState,10000);
})().catch(()=>{document.getElementById('live').textContent='장면 자료를 읽을 수 없습니다.';document.getElementById('brand-name').textContent='사무실 조회 실패'});
