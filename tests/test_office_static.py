"""브라우저 없이 원본 장면 보존과 JavaScript의 조회·패널 갱신을 확인한다."""

import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from office.server import STATIC


HARNESS = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const root=process.argv[1],scene=JSON.parse(fs.readFileSync(root+'/scene.json','utf8'));
const bubbleMode=process.argv[2]==='bubbles';
if(bubbleMode){
	scene.width=1000;scene.height=650;
	const positions={0:[300,300],1:[310,300],2:[700,300],3:[700,450],4:[850,450],5:[850,300],6:[320,300],7:[330,300],8:[5,300],9:[500,60],10:[-100,300],11:[1005,300],12:[500,800],15:[700,200]};
	scene.people.forEach(p=>[p.x,p.y]=positions[p.id]||[-1000,-1000]);
}
const mobileMode=process.argv[2]==='mobile',sessionOK=process.argv[2]!=='no-session';
const labels=new Map(),events=new Map(),windowEvents=new Map(),elements=new Map(),classes=new Set(),openedLinks=[];
let frame,interval,resize,reducedMode=false,viewport={width:1000,height:650,left:0,top:0},apiOK=true,requests=[],actionResponse,actionReject,actionCount=0,actionBody;
const drawCalls=[];
const context2d=id=>{
	let points=[],curves=0;
	return new Proxy({measureText:text=>({width:Array.from(text).length*7}),
		fillText(text,x,y){if(id==='map'){labels.set(text,[x,y]);drawCalls.push({kind:'text',text,x,y,color:this.fillStyle})}},
		beginPath(){points=[];curves=0},moveTo(x,y){points.push([x,y])},lineTo(x,y){points.push([x,y])},
		quadraticCurveTo(...coords){curves++;points.push(coords.slice(0,2),coords.slice(2))},
		stroke(){if(id==='map')drawCalls.push({kind:'bubble',points,curves,color:this.strokeStyle,fill:this.fillStyle,
			left:Math.min(...points.map(p=>p[0])),right:Math.max(...points.map(p=>p[0])),top:Math.min(...points.map(p=>p[1])),bottom:Math.max(...points.map(p=>p[1]))})}},
		{get:(object,key)=>key in object?object[key]:(()=>{}),set:(object,key,value)=>{object[key]=value;return true}});
};
class Element{
	constructor(id='',tagName='DIV'){this.id=id;this.tagName=tagName;this.hidden=id==='side'||id==='nearby';this.open=false;this.disabled=false;this.dataset={};this.style={setProperty(){}};this.innerHTML='';this.textContent='';this.value='';this.scrollTop=0;this.ctx=context2d(id);this.listeners=new Map();this.attributes={};this.children=[];this.isConnected=true}
	getContext(){return this.ctx}getBoundingClientRect(){return viewport}
	setAttribute(name,value){this.attributes[name]=value}append(...children){this.children.push(...children)}setPointerCapture(){}querySelectorAll(){return []}
	replaceChildren(...children){this.children=children;this.textContent=''}
	click(){openedLinks.push({href:this.href,target:this.target,rel:this.rel})}
	addEventListener(name,fn){this.listeners.set(name,fn)}focus(){document.activeElement=this}
	showModal(){this.open=true}close(){this.open=false}
}
const document={getElementById:id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id)},
	createElement:tag=>new Element('',tag.toUpperCase()),querySelectorAll:()=>[],visibilityState:'visible',
	addEventListener:(name,fn)=>events.set(name,fn),dispatchEvent:event=>{events.get(event.type)?.(event);return true},documentElement:new Element(),
	body:{classList:{add:value=>classes.add(value),remove:value=>classes.delete(value),contains:value=>classes.has(value)}}};
let state={updated:'2026-10-24T12:00:00+09:00',blog_dashboard:'켜짐',dev_targets:[{id:'ai-hub',label:'ai-hub'},{id:'blog-x',label:'blog-x'},{id:'xyz',label:'xyz'}],dev_plans:{implementer:'p01',reviewer:'p02'},people:scene.people.map(p=>({...p,status:'작업 중',engine:'Codex',task:'합성 작업',updated:null,project:p.department==='company'?'p01':'',duration:60})),
	approvals:{c4:{status:'정상',items:[{id:'c4-'+'a'.repeat(16),prepared:3,held:1,reply:'C4 합성 반영해'}]},
		runs:{status:'정상',items:[{id:'2026-10-24-001',status:'승인 필요'}]},r1:{status:'정상',items:[{id:'AI & Prompts',pending:2}]},
		drafts:{status:'정상',items:[]},decisions:{status:'정상',items:[{id:'d01',title:'합성 결정'}]}},
	usage:{c4:{status:'정상',subscription:3,local:5},limit:{status:'정상',daily_limit_calls:20},plans:{status:'정상',items:[]},notebooklm:{count:null}},
	schedule:{status:'정상',items:[{title:'합성 후속 일정',source:'개인',due:'2026-10-26'}]},
	activity:[],board:{status:'정상',items:[{kind:'chat',title:'결정',text:'합성 결정',at:'2026-10-24T12:00:00+09:00'}],progress:{items:[]}},engines:[]};
let draftView={id:'d-20261024-120000-abcdef01',target:'blog-x',role:'frontend',goal:'합성 <script>목표</script>',done:['완료 조건을 검사한다'],owned_paths:['office/*.py'],forbidden:['합성 금지 사항'],budget:{calls:4,minutes:30,fixes:1},plan:'p01',created:state.updated};
const pendingDraft=value=>{state.approvals.drafts.items=[{id:value.id,target:value.target,role:value.role,goal:value.goal,done:value.done.length,owned_paths:value.owned_paths,created:value.created}]};
const sandbox={document,Event,CustomEvent:class extends Event{constructor(name,options){super(name,options);this.detail=options?.detail}},window:{addEventListener:(name,fn)=>windowEvents.set(name,fn)},matchMedia:query=>({get matches(){return query.includes('reduce')?reducedMode:mobileMode&&query.includes('900px')}}),innerWidth:mobileMode?390:1200,devicePixelRatio:1,
	performance:{now:()=>100},requestAnimationFrame:fn=>{frame=fn},ResizeObserver:class{constructor(fn){resize=fn}observe(){}},AbortController,
	console,setTimeout:()=>1,clearTimeout(){},setInterval:(fn,ms)=>{interval=fn;assert.equal(ms,10000)},
	fetch:async(url,options)=>{
		requests.push(url);
		if(url==='/api/session')return {ok:sessionOK,json:async()=>({token:'xyz-session'})};
		if(url.startsWith('/api/draft/'))return {ok:true,json:async()=>({ok:true,draft:draftView})};
		if(url==='/api/action'){
			actionCount++;actionBody=JSON.parse(options.body);
			assert.equal(options.method,'POST');assert.equal(options.headers['Content-Type'],'application/json');
			assert.equal(options.headers['X-Office-Token'],'xyz-session');
			return new Promise((resolve,reject)=>{actionResponse=resolve;actionReject=reject});
		}
		return {ok:url==='/scene.json'||apiOK,json:async()=>url==='/scene.json'?scene:state};
	}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(root+'/pixel-art.js','utf8'),sandbox);
const element=id=>document.getElementById(id);
const click=data=>{const button=new Element();button.dataset=data;events.get('click')({target:{closest:()=>button}});return button};
const html=fs.readFileSync(root+'/index.html','utf8'),blogLink=html.match(/<a\b[^>]*\bid="blog-link"[^>]*>/);
if(blogLink)for(const name of ['target','rel'])element('blog-link')[name]=blogLink[0].match(new RegExp(name+'="([^"]*)"'))?.[1];
(async()=>{
	const longTask='😀'+'합성진행'.repeat(12),shortTask='오류 합성 작업';
	if(bubbleMode)state.people=state.people.map(p=>({...p,status:p.id===1?'오류':[0,5,8,9,10,11,12,15].includes(p.id)?'작업 중':p.id===3?'쉬는 중':p.id===4?'확인 불가':'대기',
		task:p.id===0?longTask:p.id===1?shortTask:p.id===5?'':'숨길 합성 작업 '+p.id}));
	await vm.runInContext(fs.readFileSync(root+'/office.js','utf8'),sandbox);
	assert.deepEqual(Object.keys(sandbox.window.officeBridge).sort(),['actors','chairs','esc','openDevWith','openPanel','people','reducedMotion','refresh','routeFrom','state','tickHook','toast','token','walkable']);
	assert.equal(sandbox.window.officeBridge.state().updated,state.updated);
	assert.equal(sandbox.window.officeBridge.token(),sessionOK?'xyz-session':null);
	assert.deepEqual(sandbox.window.officeBridge.people(),scene.people);
	assert.deepEqual(requests,['/scene.json','/api/session','/api/state']);
	if(bubbleMode){
		const cut=limit=>Array.from(longTask).slice(0,limit-1).join('')+'…';
		const paint=()=>{drawCalls.length=0;frame(200);return {bubbles:drawCalls.filter(c=>c.kind==='bubble'),texts:drawCalls.filter(c=>c.kind==='text')}};
		let drawing=paint();assert.equal(drawing.bubbles.length,2);
		assert(drawing.texts.some(c=>c.text===cut(14)));assert(drawing.texts.some(c=>c.text===shortTask));
		assert(!drawing.texts.some(c=>c.text.startsWith('숨길 합성 작업')));
		assert.equal(Array.from(cut(14)).length,14);
		assert(drawing.bubbles[1].bottom<drawing.bubbles[0].top);
		for(const [i,bubble] of drawing.bubbles.entries()){
			assert.equal(bubble.color,i===0?'#5d9973':'#dd6d76');assert.equal(bubble.fill,scene.palette.paper);assert.equal(bubble.curves,4);
			assert(bubble.left>=0&&bubble.right<=viewport.width&&bubble.top>=0&&bubble.bottom<=viewport.height);
			assert(bubble.points.some(([x,y])=>x===300+i*10&&y===bubble.bottom));
		}
		for(const c of drawing.texts.filter(c=>[cut(14),shortTask].includes(c.text)))assert.equal(c.color,scene.palette.ink);
		const normal=drawing;reducedMode=true;assert.deepEqual(paint(),normal);reducedMode=false;
		state.people[6].status=state.people[7].status='작업 중';await interval();drawing=paint();
		assert.equal(drawing.bubbles.length,3);assert(!drawing.texts.some(c=>c.text==='숨길 합성 작업 7'));
		assert.equal(drawing.bubbles[0].top-drawing.bubbles[2].top,2*(24+5+4));
		element('map').listeners.get('pointermove')({clientX:300,clientY:270,pointerId:1});
		assert(paint().texts.some(c=>c.text===cut(40)));assert.equal(Array.from(cut(40)).length,40);
		element('map').listeners.get('pointerleave')();assert(paint().texts.some(c=>c.text===cut(14)));
		click({person:'0'});assert(paint().texts.some(c=>c.text===cut(40)));
		await interval();assert(paint().texts.some(c=>c.text===cut(40)));
		click({room:'blog'});assert(paint().texts.some(c=>c.text===cut(14)));
		click({person:'0'});element('close-panel').onclick();assert(paint().texts.some(c=>c.text===cut(14)));
		state.people[6].status=state.people[7].status='대기';await interval();
		viewport={width:549,height:650*.549,left:0,top:0};resize();drawing=paint();
		assert.equal(drawing.bubbles.length,2);assert.equal(drawing.texts.filter(c=>c.text==='…').length,2);
		assert(!drawing.texts.some(c=>[cut(14),shortTask].includes(c.text)));
		for(const c of drawing.texts.filter(c=>c.text==='…'))assert.equal(c.color,scene.palette.muted);
		reducedMode=true;assert.deepEqual(paint(),drawing);reducedMode=false;
		viewport={width:550,height:650*.55,left:0,top:0};resize();assert(paint().texts.some(c=>c.text===cut(14)));
		viewport={width:1000,height:650,left:0,top:0};resize();
		state.people[0].task='가'.repeat(14);await interval();assert(paint().texts.some(c=>c.text==='가'.repeat(14)));
		state.people[0].task='나'.repeat(40);await interval();click({person:'0'});assert(paint().texts.some(c=>c.text==='나'.repeat(40)));
		return;
	}
	for(const person of scene.people){
		click({person:String(person.id)});
		const body=element('panel-body').innerHTML;
		assert.equal(body.includes('블로그 화면 열기'),!!person.blog_path,person.name);
		if(!person.blog_path)continue;
		assert(body.includes('data-blog-path="'+person.blog_path+'"'));
		assert(body.includes('data-blog-name="'+person.name+'"'));
		const button=click({blogPath:person.blog_path,blogName:person.name}),url='http://127.0.0.1:8765'+person.blog_path;
		assert.equal(element('blog-link').href,url);
		if(mobileMode){
			assert(!element('blog-dialog').open);assert.equal(element('blog-body').children.length,0);
			assert.deepEqual(openedLinks.at(-1),{href:url,target:'_blank',rel:'noopener noreferrer'});
		}else{
			assert(element('blog-dialog').open);assert.equal(document.activeElement,element('blog-close'));
			assert.equal(element('blog-title').textContent,person.name+' 블로그 화면');
			const frames=element('blog-body').children;assert.equal(frames.length,1);
			assert.equal(frames[0].tagName,'IFRAME');assert.equal(frames[0].src,url);
			assert.equal(frames[0].title,person.name+' 블로그 화면');
			assert.deepEqual(frames[0].attributes,{sandbox:'allow-scripts allow-forms allow-same-origin allow-popups allow-modals',referrerpolicy:'no-referrer'});
			windowEvents.get('keydown')({key:'Escape',target:element('map')});assert(!element('side').hidden);
			if(person.id===8){element('blog-link').click();assert.deepEqual(openedLinks.at(-1),{href:url,target:'_blank',rel:'noopener noreferrer'})}
			if(person.id===19)element('blog-dialog').listeners.get('cancel')({preventDefault(){}});else element('blog-close').onclick();
			assert(!element('blog-dialog').open);assert.equal(element('blog-body').children.length,0);
			assert.equal(document.activeElement,button);
		}
	}
	for(const path of ['', '//acme.invalid', 'https://acme.invalid', '/ xyz', '/\txyz', '/\n', '/"xyz', "/'xyz", '/<xyz', '/>xyz', '/\\xyz']){
		const count=openedLinks.length;click({blogPath:path,blogName:'합성'});
		assert(!element('blog-dialog').open);assert.equal(openedLinks.length,count);
	}
	click({room:'dev'});assert(!element('panel-body').innerHTML.includes('블로그 대시보드 열기'));
	click({room:'blog'});assert(element('panel-body').innerHTML.includes('블로그 대시보드 열기'));
	assert(element('panel-body').innerHTML.includes('data-blog-path="/"'));
	click({blogPath:'/',blogName:'블로그팀'});
	assert.equal(element('blog-link').href,'http://127.0.0.1:8765/');
	if(!mobileMode)element('blog-close').onclick();
	state.blog_dashboard='꺼짐';await interval();
	const linkCount=openedLinks.length;click({blogPath:'/clip',blogName:'클립'});
	assert.equal(element('blog-body').children.length,0);assert.equal(openedLinks.length,linkCount);
	if(mobileMode){assert(!element('blog-dialog').open);assert.equal(element('toast').textContent,'블로그 대시보드가 꺼져 있습니다')}
	else{assert(element('blog-dialog').open);assert.equal(element('blog-body').textContent,'블로그 대시보드가 꺼져 있습니다');element('blog-close').onclick()}
	assert(requests.every(url=>url.startsWith('/')&&!url.startsWith('//')));assert.equal(actionCount,0);
	state.blog_dashboard='켜짐';await interval();element('close-panel').onclick();if(!mobileMode)click({view:'office'});
	if(!sessionOK){
		click({view:'approvals'});
		assert(element('panel-body').innerHTML.includes('반영 세션을 읽지 못했습니다.'));
		assert.equal((element('panel-body').innerHTML.match(/class="apply-button"[^>]* disabled/g)||[]).length,3);
		click({action:'c4',id:'c4-'+'a'.repeat(16)});
		click({dev:'backend'});
		assert(!element('action-dialog').open);assert(!element('dev-dialog').open);assert.equal(actionCount,0);return;
	}
	if(mobileMode)click({view:'office'});
	assert.equal(element('panel-title').textContent,'오늘의 오피스');
	assert(element('panel-body').innerHTML.includes('3회'));
	assert.equal(element('brand-name').textContent,'루프탑 가든 오피스');
	for(const name of ['pointerdown','pointermove','pointerup','pointercancel','wheel'])assert(element('map').listeners.has(name));
	frame(200);const positions=new Map(labels);
	state.people=state.people.map(p=>({...p,x:0,y:0,shirt:'#000',appearance:{},task:p.department==='company'?'회사 숨김 합성 문장':'다음 합성 작업'}));
	await interval();frame(300);
	for(const p of scene.people)assert.deepEqual(labels.get(p.name),positions.get(p.name));
	click({person:'15'});
	assert(element('panel-body').innerHTML.includes('p01'));
	assert(!element('panel-body').innerHTML.includes('회사 숨김 합성 문장'));
	click({view:'schedule'});
	assert(element('panel-body').innerHTML.includes('합성 후속 일정'));
	state.schedule.items.push({title:'기한 없는 합성 일정',source:'블로그',due:''});
	await interval();
	assert.equal(element('panel-title').textContent,'앞으로의 일정');
	assert(element('panel-body').innerHTML.includes('언젠가'));
	click({characters:'true'});await interval();
	assert.equal(element('panel-title').textContent,'캐릭터 23명');
	click({directory:'true'});await interval();
	assert.equal(element('panel-title').textContent,'부서 찾기');
	click({view:'approvals'});
	assert(element('panel-body').innerHTML.includes('AI &amp; Prompts'));
	assert.equal((element('panel-body').innerHTML.match(/class="apply-button"/g)||[]).length,3);
	click({action:'c4',id:'c4-'+'a'.repeat(16)});
	assert(element('action-dialog').open);assert.equal(document.activeElement,element('action-cancel'));
	assert(element('action-description').textContent.includes('노트 3건'));
	assert(element('action-description').textContent.includes('보류 1건'));
	assert(element('action-description').textContent.includes('되돌리기'));
	element('action-cancel').onclick();assert(!element('action-dialog').open);assert.equal(actionCount,0);
	click({action:'c4',id:'c4-'+'a'.repeat(16)});
	const pending=element('action-confirm').onclick();
	assert.equal(actionCount,1);assert.equal(actionBody.action,'c4_apply');assert.equal(actionBody.id,'c4-'+'a'.repeat(16));
	assert(element('action-confirm').disabled&&element('action-cancel').disabled);
	assert(element('action-status').textContent.includes('반영 중'));
	await interval();
	assert.equal((element('panel-body').innerHTML.match(/class="apply-button"[^>]* disabled/g)||[]).length,3);
	click({action:'r1',id:'AI & Prompts'});assert.equal(actionCount,1);
	element('action-dialog').listeners.get('cancel')({preventDefault(){}});assert(element('action-dialog').open);
	state.approvals.c4.items=[];
	actionResponse({ok:true,json:async()=>({ok:true,message:'합성 반영 완료',output:'[보안값] <script>xyz</script>',restart_needed:true})});
	await pending;
	assert.equal(element('action-status').textContent,'합성 반영 완료');
	assert.equal(element('action-output').textContent,'[보안값] <script>xyz</script>');
	assert(!element('action-restart').hidden);assert(element('action-restart').textContent.includes('launchctl kickstart'));
	assert(!element('action-details').open);assert(element('action-confirm').hidden);assert(!element('action-cancel').disabled);
	assert(!element('panel-body').innerHTML.includes('C4 합성 반영해'));
	element('action-cancel').onclick();
	click({action:'r1',id:'AI & Prompts'});
	assert(element('action-description').textContent.includes('노트 2개'));
	const r1=element('action-confirm').onclick();assert.equal(actionBody.action,'r1_apply');assert.equal(actionBody.id,'AI & Prompts');
	actionResponse({ok:false,json:async()=>({ok:false,message:'합성 충돌',output:'',restart_needed:false})});await r1;
	assert.equal(element('action-status').textContent,'합성 충돌');assert(element('action-restart').hidden);
	element('action-cancel').onclick();
	click({action:'apply',id:'2026-10-24-001'});
	assert(element('action-description').textContent.includes('로컬 squash 커밋'));
	const failed=element('action-confirm').onclick();assert.equal(actionBody.action,'run_apply');
	actionReject(new Error('합성 연결 실패'));await failed;
	assert(element('action-status').textContent.includes('활동 기록과 상태를 확인'));
	assert(!element('action-cancel').disabled);element('action-cancel').onclick();
	for(const [id,role] of [['3','plan'],['4','backend'],['5','frontend'],['6','design']]){
		click({person:id});assert(element('panel-body').innerHTML.includes('data-dev="'+role+'"'));assert(element('panel-body').innerHTML.includes('data-origin="'+id+'"'));
	}
	click({person:'7'});assert(!element('panel-body').innerHTML.includes('data-dev='));
	for(const id of ['8','9','10','11','22']){
		click({person:id});assert(element('panel-body').innerHTML.includes('data-team="blog"'));assert(element('panel-body').innerHTML.includes('data-origin="'+id+'"'));
	}
	click({dev:'backend',team:'blog'});assert.equal(element('dev-target').value,'blog-x');element('dev-close').onclick();
	const savedTargets=state.dev_targets;state.dev_targets=[savedTargets[0]];
	click({dev:'backend',team:'blog'});assert.equal(element('dev-target').value,'ai-hub');element('dev-close').onclick();state.dev_targets=savedTargets;
	click({dev:'frontend',origin:'11'});assert(element('dev-dialog').open);assert.equal(element('dev-role').value,'frontend');
	assert.equal(element('dev-target').value,'ai-hub');assert(element('dev-target').innerHTML.includes('blog-x'));
	element('dev-target').value='blog-x';element('dev-target').onchange();assert(element('dev-warning').textContent.includes('blog-x 저장소'));
	assert.equal(document.activeElement,element('dev-text'));assert(element('dev-write').disabled);
	element('dev-text').value='합성 지시';element('dev-text').oninput();
	assert(!element('dev-write').disabled);assert(element('dev-remaining').textContent.includes(String(2000-'합성 지시'.length)));
	await interval();assert.equal(element('dev-text').value,'합성 지시');assert.equal(element('dev-role').value,'frontend');
	element('dev-text').value='a'.repeat(2001);element('dev-text').oninput();assert(element('dev-write').disabled);
	element('dev-text').value='합성 지시';element('dev-text').oninput();
	const beforeDraft=actionCount,drafting=element('dev-write').onclick();
	assert.equal(actionBody.action,'dev_draft');assert.equal(actionBody.id,'frontend');assert.equal(actionBody.text,'합성 지시');
	assert.equal(actionBody.target,'blog-x');assert.equal(actionBody.origin,11);assert.deepEqual(Object.keys(actionBody).sort(),['action','id','origin','target','text']);
	assert.equal(element('dev-status').textContent,'초안 쓰는 중(1~3분)');
	assert(element('dev-write').disabled&&element('dev-text').disabled&&element('dev-close').disabled);
	assert(element('dev-target').disabled);
	element('dev-write').onclick();assert.equal(actionCount,beforeDraft+1);
	element('dev-dialog').listeners.get('cancel')({preventDefault(){}});assert(element('dev-dialog').open);
	await interval();assert.equal(element('dev-text').value,'합성 지시');
	pendingDraft(draftView);actionResponse({ok:true,json:async()=>({ok:true,message:'합성 초안 완료',draft:draftView})});await drafting;
	assert(!element('dev-card').hidden);assert(element('dev-editor').hidden);
	assert(element('dev-card').innerHTML.includes('&lt;script&gt;'));assert(!element('dev-card').innerHTML.includes('<script>'));
	for(const text of ['완료 조건','고칠 파일 범위','금지 사항','예산','4회 호출'])assert(element('dev-card').innerHTML.includes(text));
	for(const text of ['대상 저장소','blog-x','구현 요금제: p01','검토 요금제: p02'])assert(element('dev-card').innerHTML.includes(text));
	assert(!element('dev-run').hidden&&!element('dev-run').disabled);
	element('dev-redraft').onclick();assert(!element('dev-editor').hidden);assert.equal(element('dev-text-label').textContent,'추가 지시');
	assert(element('dev-target').disabled);assert.equal(element('dev-target').value,'blog-x');
	element('dev-text').value='추가 합성 지시';element('dev-text').oninput();
	const redrafting=element('dev-write').onclick();assert.equal(actionBody.action,'dev_redraft');assert.equal(actionBody.id,draftView.id);
	assert.equal(actionBody.text,'추가 합성 지시');
	assert.deepEqual(Object.keys(actionBody).sort(),['action','id','text']);
	draftView={...draftView,id:'d-20261024-120100-abcdef02'};pendingDraft(draftView);
	actionResponse({ok:true,json:async()=>({ok:true,message:'합성 다시 쓰기 완료',draft:draftView})});await redrafting;
	const beforeRun=actionCount;element('dev-run').onclick();assert(!element('dev-dialog').open);assert(element('action-dialog').open);
	assert.equal(actionCount,beforeRun);assert.equal(element('action-confirm').textContent,'실행');
	assert(element('action-description').textContent.includes('4회 호출 · 30분 · 보완 1회'));
	assert(element('action-description').textContent.includes('반영은 따로 승인'));
	for(const text of ['대상 저장소: blog-x','구현 요금제: p01','검토 요금제: p02'])assert(element('action-description').textContent.includes(text));
	element('action-cancel').onclick();assert.equal(actionCount,beforeRun);
	click({filter:'draft'});assert(element('panel-body').innerHTML.includes('개발 지시 확인'));
	assert(element('panel-body').innerHTML.includes('완료 조건 1개'));
	assert(element('panel-body').innerHTML.includes('대상 저장소 blog-x'));
	click({draft:draftView.id});await new Promise(resolve=>setImmediate(resolve));assert(element('dev-dialog').open);
	assert(!element('dev-card').hidden);assert.equal(element('dev-role').value,'frontend');
	element('dev-run').onclick();const running=element('action-confirm').onclick();
	assert.equal(actionBody.action,'dev_run');assert.equal(actionBody.id,draftView.id);assert.deepEqual(Object.keys(actionBody).sort(),['action','id']);
	state.approvals.drafts.items=[];actionResponse({ok:true,json:async()=>({ok:true,message:'실행 시작',output:'',restart_needed:false})});await running;
	element('action-cancel').onclick();assert(!element('panel-body').innerHTML.includes('완료 조건 1개'));
	draftView={...draftView,id:'d-20261024-120200-abcdef03'};pendingDraft(draftView);await interval();
	click({draft:draftView.id});await new Promise(resolve=>setImmediate(resolve));
	const discarding=element('dev-discard').onclick();assert.equal(actionBody.action,'dev_discard');assert.deepEqual(Object.keys(actionBody).sort(),['action','id']);
	state.approvals.drafts.items=[];actionResponse({ok:true,json:async()=>({ok:true,message:'버림'})});await discarding;
	assert(!element('dev-dialog').open);
	apiOK=false;await interval();click({person:'8'});
	assert(element('panel-body').innerHTML.includes('확인 불가'));
	assert(element('updated').textContent.includes('상태 조회 실패'));
	assert.equal(element('approval-count').textContent,'?');
	click({view:'schedule'});
	assert(element('panel-body').innerHTML.includes('일정 자료를 확인할 수 없습니다.'));
	assert(!element('panel-body').innerHTML.includes('등록된 일정이 없습니다.'));
})().catch(error=>{console.error(error);process.exitCode=1});
"""


class OfficeStaticTest(unittest.TestCase):
	def test_only_six_blog_roles_have_safe_local_paths(self):
		people = json.loads((STATIC / "scene.json").read_text())["people"]
		expected = {8: ("작가", "/?tab=tab3"), 9: ("분석", "/"), 10: ("클립", "/clip"),
			11: ("티스토리", "/?tab=tab3"), 19: ("이웃", "/outreach"), 22: ("네이버", "/?tab=tab3")}
		actual = {p["id"]: (p["name"], p["blog_path"]) for p in people if "blog_path" in p}
		self.assertEqual(actual, expected)
		for _, path in actual.values():
			self.assertIsInstance(path, str)
			self.assertTrue(path.startswith("/") and not path.startswith("//"))
			self.assertNotRegex(path, r"[\s\"'<>\\]")

	def test_original_pixel_art_and_22_seat_names_are_preserved(self):
		art = "\n".join(line.strip() for line in (STATIC / "pixel-art.js").read_text().strip().splitlines())
		self.assertEqual(hashlib.sha256(art.encode()).hexdigest(), "ea3ef8d6eeb4fdaed56b2b48d64436d706bec07f85d9891c6c978c9173486f20")
		scene = json.loads((STATIC / "scene.json").read_text())
		self.assertEqual([p["name"] for p in scene["people"]], ["PM", "기록", "총무", "기획", "백엔드", "프론트", "디자인", "QA", "작가", "분석", "클립", "티스토리", "PD", "대본", "썸네일", "회사 개발", "레거시 분석", "이관 QA", "회사 기록", "이웃", "편집", "음악", "네이버"])
		self.assertEqual([p["seat"] for p in scene["people"]], list(range(1, 24)))
		self.assertEqual(sum("seat" in p for p in scene["props"]), 26)
		self.assertTrue(all(not {"status", "engine", "task", "duration"}.intersection(p) for p in scene["people"]))

	def test_styles_keep_responsive_rules_and_remove_design_selection(self):
		css = (STATIC / "office.css").read_text()
		html = (STATIC / "index.html").read_text()
		self.assertEqual(css.count("{"), css.count("}"))
		self.assertIn("@media(max-width:900px)", css)
		self.assertIn("touch-action:none", css)
		self.assertIn("prefers-reduced-motion:reduce", css)
		self.assertIn('id="approval-count">?</span>', html)
		self.assertIn('<dialog id="action-dialog"', html)
		self.assertIn('aria-describedby="action-description"', html)
		self.assertIn('<details id="action-details">', html)
		self.assertIn('max-height:calc(100dvh - 32px)', css)
		self.assertIn('width:min(480px,calc(100% - 24px))', css)
		self.assertIn('<dialog id="dev-dialog"', html)
		self.assertIn('<dialog id="blog-dialog" class="action-dialog blog-dialog" aria-labelledby="blog-title">', html)
		self.assertIn('<a id="blog-link" class="outline-button" target="_blank" rel="noopener noreferrer">새 탭으로 열기</a>', html)
		self.assertIn('<button id="blog-close" class="outline-button">닫기</button>', html)
		self.assertIn('.blog-dialog{width:90vw;height:80vh;height:80dvh;', css)
		self.assertIn('.blog-dialog[open]{display:flex;flex-direction:column;', css)
		self.assertIn('.blog-head{display:flex;align-items:center;', css)
		self.assertIn('flex:none', css.split('.blog-head{')[1].split('}')[0])
		self.assertIn('.blog-body{flex:1;min-height:0;overflow:auto}', css)
		self.assertIn('maxlength="2000"', html)
		self.assertIn('회사 내용·비밀값은 적지 마세요', html)
		self.assertIn('aria-describedby="dev-warning dev-remaining"', html)
		self.assertIn('<label for="dev-target">대상 저장소</label>', html)
		self.assertIn('<select id="dev-target"', html)
		self.assertIn('overflow-wrap:anywhere', css)
		self.assertIn('.dev-card .stat{flex-wrap:wrap;', css)
		self.assertNotRegex((STATIC / "office.js").read_text(), r"\b(?:alert|confirm|prompt)\s*\(")
		self.assertIn('max-width:100%', css)
		self.assertIn('textarea:focus-visible', css)
		for word in ("이 안 선택", "시안", "예시", 'id="choose"', 'id="copy-dialog"'):
			self.assertNotIn(word, html)

	@unittest.skipUnless(shutil.which("node"), "JavaScript 실행기 없음")
	def test_fetch_refresh_panel_retention_error_and_fixed_actor_positions_without_browser(self):
		for mode in ("desktop", "mobile", "no-session", "bubbles"):
			with self.subTest(mode=mode):
				result = subprocess.run([shutil.which("node"), "-e", HARNESS, str(STATIC), mode], capture_output=True, text=True, timeout=10)
				self.assertEqual(result.returncode, 0, result.stderr)
