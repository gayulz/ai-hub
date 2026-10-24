"""브라우저·소켓 없이 회의 화면의 DOM 동작과 요청 계약을 검사한다."""

import shutil
import subprocess
import unittest
from unittest import mock

from office import server


HARNESS = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),root=process.argv[1];
const scene=JSON.parse(fs.readFileSync(root+'/scene.json','utf8'));
const MID='m-'+'a'.repeat(32),CID='q-'+'b'.repeat(32),plain=v=>JSON.parse(JSON.stringify(v));
const decode=s=>s.replace(/&(?:amp|lt|gt|quot|#39);/g,v=>({'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&#39;':"'"}[v]));
class Event{
	constructor(type,options={}){Object.assign(this,options);this.type=type;this.stopped=false;this.defaultPrevented=false}
	preventDefault(){this.defaultPrevented=true}stopImmediatePropagation(){this.stopped=true}
}
class Events{
	constructor(){this.listeners=new Map()}
	addEventListener(type,fn,options={}){if(!this.listeners.has(type))this.listeners.set(type,[]);this.listeners.get(type).push({fn,capture:options===true||!!options.capture,once:!!options.once})}
	deliver(e,capture){for(const row of [...(this.listeners.get(e.type)||[])]){if(row.capture!==capture)continue;row.fn(e);if(row.once)this.listeners.set(e.type,this.listeners.get(e.type).filter(r=>r!==row));if(e.stopped)break}}
	dispatchEvent(e){this.deliver(e,true);if(!e.stopped)this.deliver(e,false);return !e.defaultPrevented}
}
class Element extends Events{
	constructor(tag='div'){super();this.tagName=tag.toUpperCase();this.children=[];this.parentElement=null;this.attributes={};this.dataset={};this.style={values:{},setProperty(k,v){this.values[k]=v}};this._html='';this._text='';this.hidden=false;this.disabled=false;this.open=false;this.value='';this.scrollTop=0;const names=new Set();this.classList={add:n=>names.add(n),remove:n=>names.delete(n),contains:n=>names.has(n)};this.ctx=new Proxy({canvas:this,measureText:t=>({width:Array.from(t).length*7})},{get:(o,k)=>k in o?o[k]:(()=>{}),set:(o,k,v)=>{o[k]=v;return true}})}
	setAttribute(k,v){this.attributes[k]=String(v);if(k==='id')this.id=v;if(k==='class')this.className=v;if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=String(v);if(k==='hidden'||k==='disabled')this[k]=true;if(k==='value')this.value=v}
	getAttribute(k){return this.attributes[k]??null}
	get isConnected(){return this===document.documentElement||!!this.parentElement?.isConnected}
	get nextElementSibling(){return this.parentElement?.children[this.parentElement.children.indexOf(this)+1]}
	append(...nodes){for(const node of nodes){node.parentElement=this;this.children.push(node)}}
	replaceChildren(...nodes){for(const child of this.children)child.parentElement=null;this.children=[];this._text='';this._html='';this.append(...nodes)}
	set innerHTML(html){this.replaceChildren();this._html=html;const stack=[this];for(const token of html.match(/<[^>]*>|[^<]+/g)||[]){if(token.startsWith('</')){if(stack.length>1)stack.pop()}else if(token.startsWith('<!'))continue;else if(token.startsWith('<')){const tag=token.match(/^<([\w-]+)/)?.[1];if(!tag)continue;const node=new Element(tag);for(const a of token.slice(tag.length+1,-1).matchAll(/([\w-]+)(?:="([^"]*)")?/g))node.setAttribute(a[1],decode(a[2]??''));stack.at(-1).append(node);if(!/\/$/.test(token.slice(0,-1))&&!['input','meta','link','br','hr'].includes(tag))stack.push(node)}else stack.at(-1)._text+=decode(token)}}
	get innerHTML(){return this._html}
	set textContent(text){this.replaceChildren();this._text=String(text)}get textContent(){return this._text+this.children.map(c=>c.textContent).join('')}
	matches(selector){const tag=selector.match(/^[\w-]+/)?.[0];if(tag&&tag.toUpperCase()!==this.tagName)return false;for(const c of selector.matchAll(/\.([\w-]+)/g))if(!(this.className||'').split(' ').includes(c[1]))return false;for(const a of selector.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)){const v=a[1]==='open'?(this.open?'':null):this.getAttribute(a[1])??(a[1].startsWith('data-')?this.dataset[a[1].slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]:null);if(v===null||v===undefined||a[2]!==undefined&&String(v)!==a[2])return false}return true}
	querySelectorAll(selector){return this.children.flatMap(c=>[...(selector.split(',').some(s=>c.matches(s.trim()))?[c]:[]),...c.querySelectorAll(selector)])}
	querySelector(selector){return this.querySelectorAll(selector)[0]||null}
	closest(selector){return this.matches(selector)?this:this.parentElement?.closest(selector)||null}
	contains(node){return node===this||this.children.some(c=>c.contains(node))}
	getContext(){return this.ctx}setPointerCapture(){}
	getBoundingClientRect(){return this.id==='meeting-popover'?{width:Math.min(340,width-24),height:500,left:0,top:0,bottom:500}:{width:1000,height:650,left:20,top:80,bottom:124}}
	focus(){document.activeElement=this}showModal(){assert(!document.querySelector('dialog[open]'));this.open=true}close(){this.open=false}
}
const document=new Events();document.documentElement=new Element('html');document.documentElement.innerHTML=fs.readFileSync(root+'/index.html','utf8');
document.body=document.documentElement.querySelector('body');document.visibilityState='visible';document.activeElement=document.body;
document.getElementById=id=>document.documentElement.querySelectorAll('[id]').find(el=>el.id===id)||null;
document.createElement=tag=>new Element(tag);document.querySelectorAll=s=>document.documentElement.querySelectorAll(s);document.querySelector=s=>document.documentElement.querySelector(s);
const window=new Events(),$=id=>document.getElementById(id),width=390,height=740;
const questions=['scope','tone','format'].map((id,i)=>({id,text:'합성 질문 '+(i+1),options:[{id:'a',label:'합성 A',difference:'합성 차이 A',reply:'합성 회신 A'},{id:'b',label:'합성 B',difference:'합성 차이 B',reply:'합성 회신 B'}],recommended:'a',reason:'합성 추천 이유',allow_delegate:i!==1}));
let state={updated:'2026-10-24T12:00:00+09:00',people:scene.people.map(p=>({id:p.id,status:'대기'})),dev_targets:[{id:'xyz',label:'xyz'},{id:'ai-hub',label:'ai-hub'}],
	approvals:{c4:{status:'정상',items:[]},runs:{status:'정상',items:[]},r1:{status:'정상',items:[]},drafts:{status:'정상',items:[]},decisions:{status:'정상',items:[]},questions:{status:'정상',count:1}},
	meetings:{status:'정상',items:[{id:MID,status:'questions_pending',agenda:'합성 안건',participants:['backend','qa']}]},questions:{status:'정상',items:[{meeting:MID,card:CID,turn:1,questions}]},
	meeting_roles:[{key:'backend',name:'백엔드'},{key:'qa',name:'QA'},{key:'plan',name:'기획'},{key:'design',name:'디자인'},{key:'frontend',name:'프론트'}],activity:[],schedule:{items:[]},engines:[]};
let interval,resize,apiOK=true,token='xyz-'+'session',requests=[],reply=async()=>({status:400,ok:false,json:async()=>({ok:false,message:'합성 거부'})});
const response=(body,status=200)=>({status,ok:status>=200&&status<300,json:async()=>plain(body)});
const sandbox={document,window,Event,CustomEvent:Event,console,AbortController,innerWidth:width,innerHeight:height,devicePixelRatio:1,
	matchMedia:q=>({matches:q.includes('900px')}),performance:{now:()=>0},requestAnimationFrame(){},ResizeObserver:class{constructor(fn){resize=fn}observe(){}},setTimeout:()=>1,clearTimeout(){},setInterval:fn=>{interval=fn},
	getComputedStyle:el=>({getPropertyValue:key=>el.style.values[key]}),
	fetch:async(url,options={})=>{if(url==='/scene.json')return response(scene);if(url==='/api/session')return response({token});if(url==='/api/state')return response(state,apiOK?200:503);assert.equal(url,'/api/action');assert.equal(options.method,'POST');assert.equal(options.headers['Content-Type'],'application/json');assert.equal(options.headers['X-Office-Token'],token);const payload=JSON.parse(options.body);requests.push(payload);return reply(payload)}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(root+'/pixel-art.js','utf8'),sandbox);
const makeArt=sandbox.createPixelArt,avatarDraws=[];sandbox.createPixelArt=(s)=>{const art=makeArt(s),draw=art.drawActor;art.drawActor=(ctx,actor,t)=>{if(ctx.canvas.dataset.roleAvatar)avatarDraws.push(plain(actor));draw(ctx,actor,t)};return art};
function click(target){if(target.disabled)return;const open=document.querySelector('dialog[open]');if(open&&!open.contains(target))return;const e=new Event('click',{target});document.deliver(e,true);if(e.stopped)return;let result;for(let node=target;node;node=node.parentElement){result=node.onclick?.(e)??result;node.deliver(e,false);if(e.stopped)return result}document.deliver(e,false);return result}
const button=(selector)=>{const el=document.querySelector(selector);assert(el,selector);return el};
const input=(el,value)=>{el.value=value;return $('meeting-body').oninput({target:el})};
const openRequest=()=>{click(button('button.room-label[data-room="meeting"]'));assert(!$('meeting-popover').hidden);click($('meeting-request'));assert($('meeting-dialog').open)};
const openQuestions=()=>{click(document.querySelector('[data-view="approvals"]'));click(button('[data-question-card]'));assert.equal($('meeting-title').textContent,'회의 질문')};
const choice=(q,value='a')=>click(button('[data-question="'+q+'"][data-answer-kind="choice"][data-value="'+value+'"]'));
const delegate=q=>click(button('[data-question="'+q+'"][data-answer-kind="delegate"]'));
const refresh=()=>sandbox.window.officeBridge.refresh();
async function boot(order='early'){
	if(order==='early')vm.runInContext(fs.readFileSync(root+'/meeting.js','utf8'),sandbox);
	await vm.runInContext(fs.readFileSync(root+'/office.js','utf8'),sandbox);
	if(order==='late')vm.runInContext(fs.readFileSync(root+'/meeting.js','utf8'),sandbox);
}
(async()=>{
"""


@unittest.skipUnless(shutil.which("node"), "JavaScript 실행기 없음")
class OfficeMeetingUITest(unittest.TestCase):
	def run_js(self, checks):
		result = subprocess.run([shutil.which("node"), "-e", HARNESS + checks + "\n})().catch(e=>{console.error(e);process.exitCode=1});",
			str(server.STATIC)], capture_output=True, text=True, timeout=15)
		self.assertEqual(result.returncode, 0, result.stderr)

	def test_popover_labels_routing_bounds_and_close_without_browser(self):
		self.run_js(r"""
		await boot();const labels=['참석자 확인 대기','질문 답변 대기','답변 확인 대기','진행 중','완료','실패','예산 소진','질문 한도 소진','기록 대기'];
		const codes=['participants_pending','questions_pending','answers_pending','running','completed','failed','budget_exhausted','questions_exhausted','record_pending'];
		for(let i=0;i<codes.length;i++){
			state.meetings.items=[{id:MID,status:codes[i],agenda:'가'.repeat(60),participants:['backend','qa']}];await refresh();
			click(button('button.room-label[data-room="meeting"]'));assert($('meeting-recent').textContent.includes(labels[i]));assert(!$('meeting-recent').textContent.includes('가'.repeat(41)));
			const left=parseFloat($('meeting-popover').style.left),top=parseFloat($('meeting-popover').style.top);assert(left>=12&&left+340<=width-12);assert(top>=12&&top+500<=height-12);
			window.dispatchEvent(new Event('keydown',{key:'Escape',target:$('map')}));assert($('meeting-popover').hidden);
		}
		state.meetings.items=Array.from({length:6},(_,i)=>({id:String(i),status:'completed',agenda:'합성 '+i}));await refresh();
		click(button('button.room-label[data-room="meeting"]'));assert.equal($('meeting-recent').querySelectorAll('button').length,5);
		click(button('[data-meeting-id="0"]'));assert($('meeting-popover').hidden);assert.equal($('panel-body').textContent,'완료');await interval();assert.equal($('panel-body').textContent,'완료');
		document.dispatchEvent(new Event('office-meeting-room',{detail:{x:1000,y:1000}}));assert(!$('meeting-popover').hidden);
		document.dispatchEvent(new Event('pointerdown',{target:$('map')}));assert($('meeting-popover').hidden);
		const map=$('map'),label=button('button.room-label[data-room="meeting"]'),coords={clientX:20+parseFloat(label.style.left),clientY:80+parseFloat(label.style.top)-100*parseFloat($('zoom-value').textContent)/100,pointerId:1};
		map.listeners.get('pointerdown')[0].fn(new Event('pointerdown',coords));map.listeners.get('pointerup')[0].fn(new Event('pointerup',coords));assert(!$('meeting-popover').hidden);
		""")

	def test_no_session_or_unavailable_meetings_disable_actions(self):
		for preparation in ("token=null;", "state.meetings.status='확인 불가';state.meetings.reason='합성 상태 거부';"):
			with self.subTest(preparation=preparation):
				self.run_js(preparation + r"""
				await boot();click(button('button.room-label[data-room="meeting"]'));assert($('meeting-request').disabled);assert($('meeting-unavailable').textContent);click($('meeting-request'));assert(!$('meeting-dialog').open);assert.equal(requests.length,0);
				""")

	def test_request_rejection_exact_body_busy_and_refresh_preserve_input(self):
		self.run_js(r"""
		await boot();openRequest();assert.equal($('meeting-target').value,'ai-hub');assert($('meeting-submit').disabled);
		input($('meeting-text'),'합성 <script>안건</script>');const text=$('meeting-text');assert.equal($('meeting-remaining').textContent,(2000-Array.from(text.value).length)+'자 남음');
		await interval();assert.strictEqual($('meeting-text'),text);assert.equal(text.value,'합성 <script>안건</script>');
		input(text,'가'.repeat(2001));assert($('meeting-submit').disabled);input(text,'합성 안건');$('meeting-target').value='xyz';
		let resolve;reply=()=>new Promise(r=>resolve=r);const pending=click($('meeting-submit'));assert($('meeting-submit').disabled&&$('meeting-close').disabled);assert.equal($('meeting-status').textContent,'분석 중(1~3분)');
		assert.deepEqual(requests.at(-1),{action:'meeting_analyze',id:'xyz',text:'합성 안건',origin:scene.people.find(p=>p.name==='PM').id});
		resolve(response({ok:false,message:'합성 거부 <script>그대로</script>'},400));await pending;assert.equal($('meeting-status').textContent,'합성 거부 <script>그대로</script>');assert.equal($('meeting-status').children.length,0);assert(!text.disabled);
		click($('meeting-close'));assert(!$('meeting-dialog').open);assert.equal(requests.length,1);
		""")

	def test_analyze_routes_to_questions_or_participants_and_reuses_avatars(self):
		for has_questions in (True, False):
			with self.subTest(questions=has_questions):
				self.run_js("const hasQuestions=" + str(has_questions).lower() + ";" + r"""
				await boot();openRequest();input($('meeting-text'),'합성 안건');
				reply=async()=>{state.meetings.items[0].status=hasQuestions?'questions_pending':'participants_pending';if(!hasQuestions)state.questions.items=[];return response({ok:true,draft:{id:MID,status:state.meetings.items[0].status,cards:hasQuestions?[{id:CID}]:[],participants:[{role:'backend',reason:'합성 서버 사유'},{role:'qa',reason:'합성 검사 사유'}]}})};
				await click($('meeting-submit'));assert.equal($('meeting-title').textContent,hasQuestions?'회의 질문':'참석자 확인');
				if(hasQuestions){assert($('meeting-submit').disabled);assert.equal($('meeting-body').querySelectorAll('fieldset').length,3)}
				else{assert(!$('meeting-submit').disabled);assert.equal($('meeting-submit').textContent,'확정하고 회의 시작');assert($('meeting-reasons').textContent.includes('합성 서버 사유'));assert.equal(avatarDraws.length,5);for(const a of avatarDraws)assert.deepEqual(a.appearance,scene.people.find(p=>p.id===a.id).appearance)}
				""")

	def test_answers_are_exclusive_complete_and_confirmed_separately(self):
		self.run_js(r"""
		await boot();openQuestions();assert($('meeting-submit').disabled);assert($('meeting-body').textContent.includes('추천 이유: 합성 추천 이유'));assert(!document.querySelector('[data-question="tone"][data-answer-kind="delegate"]'));
		choice('scope');assert($('meeting-submit').disabled);const chosen=button('[data-question="scope"][data-value="a"]');assert.equal(chosen.getAttribute('aria-pressed'),'true');
		input($('meeting-text-1'),'가');assert($('meeting-submit').disabled);input($('meeting-text-1'),'직접 쓴 합성 답변');delegate('format');assert(!$('meeting-submit').disabled);
		choice('scope','b');assert.equal(chosen.getAttribute('aria-pressed'),'false');input($('meeting-text-0'),'직접 쓴 답변');assert.equal(button('[data-question="scope"][data-value="b"]').getAttribute('aria-pressed'),'false');choice('scope','a');
		const retained=$('meeting-text-1');await interval();assert.strictEqual($('meeting-text-1'),retained);assert.equal(retained.value,'직접 쓴 합성 답변');assert(!$('meeting-submit').disabled);
		reply=async()=>{state.questions.items=[];state.meetings.items[0].status='answers_pending';return response({ok:true,draft:{id:MID,card:CID,summary:'합성 요약 <script>표시</script>'}})};
		await click($('meeting-submit'));assert.deepEqual(requests.at(-1),{action:'meeting_answer',id:MID,card:CID,answers:[{question_id:'scope',kind:'choice',value:'a'},{question_id:'tone',kind:'text',value:'직접 쓴 합성 답변'},{question_id:'format',kind:'delegate',value:'알아서 정해 줘'}]});
		assert.equal($('meeting-submit').textContent,'확인');assert.equal($('meeting-body').textContent,'합성 요약 <script>표시</script>');assert.equal($('meeting-body').querySelectorAll('script').length,0);assert.equal(requests.length,1);
		await interval();assert.equal($('meeting-submit').textContent,'확인');assert.equal(requests.length,1);
		reply=async()=>{state.meetings.items[0].status='participants_pending';return response({ok:true})};await click($('meeting-submit'));assert.deepEqual(requests.at(-1),{action:'meeting_confirm',id:MID,card:CID});assert.equal($('meeting-title').textContent,'참석자 확인');
		""")

	def test_conflict_message_survives_refresh_and_closing_never_consents(self):
		self.run_js(r"""
		await boot();openQuestions();choice('scope');click($('meeting-close'));assert.equal(requests.length,0);assert.equal(state.questions.items.length,1);
		openQuestions();choice('scope');input($('meeting-text-1'),'합성 답변');delegate('format');reply=async()=>{state.questions.items=[];state.approvals.questions.count=0;return response({ok:false,message:'이미 답했습니다(텔레그램에서)'},409)};
		await click($('meeting-submit'));assert.equal($('meeting-status').textContent,'이미 답했습니다(텔레그램에서)');assert($('meeting-submit').disabled);assert.equal(sandbox.window.officeBridge.state().questions.items.length,0);await interval();assert.equal($('meeting-status').textContent,'이미 답했습니다(텔레그램에서)');
		click($('meeting-close'));assert.equal(requests.length,1);assert(!requests.some(r=>r.action==='meeting_confirm'));
		""")

	def test_participant_min_max_start_payload_and_fallback(self):
		self.run_js(r"""
		state.questions.items=[];state.meetings.items[0].status='participants_pending';await boot('late');
		click(button('button.room-label[data-room="meeting"]'));click(button('[data-meeting-id]'));assert.equal($('meeting-title').textContent,'참석자 확인');assert(!$('meeting-submit').disabled);assert($('meeting-reasons').textContent.includes('확인할 수 없습니다'));
		const chip=key=>button('[data-participant="'+key+'"]');click(chip('qa'));assert($('meeting-submit').disabled);assert.equal($('meeting-participant-hint').textContent,'2~4명을 골라 주세요');
		click(chip('qa'));for(const key of ['plan','design'])click(chip(key));assert(!$('meeting-submit').disabled);click(chip('frontend'));assert($('meeting-submit').disabled);click(chip('frontend'));
		const before=chip('qa');await interval();assert.strictEqual(chip('qa'),before);assert.equal(before.getAttribute('aria-pressed'),'true');
		reply=async()=>{state.meetings.items[0].status='running';return response({ok:true})};await click($('meeting-submit'));assert.deepEqual(requests.at(-1),{action:'meeting_start',id:MID,participants:['backend','qa','plan','design']});assert(!$('meeting-dialog').open);assert.equal($('toast').textContent,'회의를 시작했습니다');assert.equal(sandbox.window.officeBridge.state().meetings.items[0].status,'running');
		""")

	def test_question_todos_count_filter_and_existing_dialog_exclusion(self):
		self.run_js(r"""
		state.approvals.questions.count=7;await boot();click(button('[data-view="office"]'));assert($('panel-body').innerHTML.includes('<b>7</b>AI 질문'));click(button('[data-filter="question"]'));assert($('panel-body').textContent.includes('합성 안건 · 질문 3개 · '+MID.slice(0,8)));assert.equal(document.querySelectorAll('[data-question-card]').length,1);
		for(const id of ['dev-dialog','action-dialog','blog-dialog']){$(id).showModal();click(button('[data-question-card]'));document.dispatchEvent(new Event('office-meeting-room',{detail:{x:100,y:100}}));assert(!$('meeting-dialog').open);assert($('meeting-popover').hidden);$(id).close()}
		openQuestions();const text=$('meeting-body').innerHTML;window.dispatchEvent(new Event('keydown',{key:'w',target:$('meeting-submit')}));await interval();assert.equal($('meeting-body').innerHTML,text);assert.equal(document.querySelectorAll('dialog[open]').length,1);
		window.dispatchEvent(new Event('keydown',{key:'Escape',target:$('meeting-close')}));assert(!$('meeting-dialog').open);assert.equal(requests.length,0);
		state.questions.items=[];state.approvals.questions.count=0;await refresh();click(button('[data-filter="question"]'));assert.equal(document.querySelectorAll('[data-question-card]').length,0);
		apiOK=false;await refresh();if(!$('side').hidden)click(button('[data-view="office"]'));click(button('[data-view="office"]'));assert($('panel-body').innerHTML.includes('<b>?</b>AI 질문'));
		""")

	def test_module_loaded_after_state_refreshes_question_badge(self):
		self.run_js(r"""
		await boot('late');await new Promise(resolve=>setImmediate(resolve));assert.equal($('approval-count').textContent,'1');assert.equal(requests.length,0);
		""")

	def test_keyboard_focus_keeps_answers_and_question_scroll_starts_at_top(self):
		self.run_js(r"""
		await boot();openQuestions();assert.equal(document.activeElement,$('meeting-title'));assert.equal($('meeting-dialog').scrollTop,0);
		choice('scope');input($('meeting-text-1'),'합성 답변');delegate('format');assert(!$('meeting-submit').disabled);
		$('meeting-body').dispatchEvent(new Event('focusin',{target:$('meeting-text-0')}));assert(!$('meeting-submit').disabled);assert.equal(button('[data-question="scope"][data-value="a"]').getAttribute('aria-pressed'),'true');
		""")

	def test_missing_question_projection_can_be_reread_without_consent(self):
		self.run_js(r"""
		await boot();openRequest();input($('meeting-text'),'합성 안건');reply=async()=>{apiOK=false;return response({ok:true,draft:{id:MID,status:'questions_pending',cards:[{id:CID}]}})};
		await click($('meeting-submit'));assert.equal($('meeting-title').textContent,'회의 질문');assert.equal($('meeting-submit').textContent,'다시 읽기');assert(!$('meeting-submit').disabled);assert.equal(requests.length,1);
		apiOK=true;await click($('meeting-submit'));assert.equal($('meeting-submit').textContent,'제출');assert($('meeting-submit').disabled);assert.equal(requests.length,1);
		""")

	def test_summary_close_and_network_failure_do_not_retry_mutations(self):
		self.run_js(r"""
		await boot();openQuestions();choice('scope');input($('meeting-text-1'),'합성 답변');delegate('format');reply=async()=>response({ok:true,draft:{id:MID,card:CID,summary:'합성 확인 요약'}});
		await click($('meeting-submit'));click($('meeting-close'));assert.equal(requests.length,1);assert(!requests.some(r=>r.action==='meeting_confirm'));
		openRequest();input($('meeting-text'),'다음 합성 안건');reply=async()=>{throw new Error('합성 연결 거부')};await click($('meeting-submit'));assert($('meeting-status').textContent.includes('결과를 받지 못했습니다'));assert.equal($('meeting-text').value,'다음 합성 안건');assert($('meeting-submit').disabled);assert(!$('meeting-close').disabled);assert.equal(requests.length,2);
		""")

	def test_origin_fallback_and_missing_avatar_use_existing_initials(self):
		self.run_js(r"""
		scene.people=scene.people.map((p,i)=>i===0?{...p,name:'합성 PM'}:p);state.questions.items=[];state.meetings.items[0].status='participants_pending';state.meeting_roles.push({key:'xyz',name:'합성 역할'});await boot();openRequest();input($('meeting-text'),'합성 안건');
		await click($('meeting-submit'));assert.equal(requests.at(-1).origin,scene.people[0].id);click($('meeting-close'));
		click(button('button.room-label[data-room="meeting"]'));click(button('[data-meeting-id]'));const avatar=button('[data-role-avatar="xyz"]');assert(avatar.hidden);assert(!avatar.nextElementSibling.hidden);assert.equal(avatar.nextElementSibling.textContent,'합');
		""")


class OfficeMeetingUIStaticTest(unittest.TestCase):
	def test_static_files_and_existing_server_index_without_socket(self):
		captured = []
		handler = object.__new__(server.Handler)
		handler.target = lambda: "/"
		handler.respond = lambda *args: captured.append(args)
		with mock.patch("socket.create_connection", side_effect=AssertionError("네트워크 금지")):
			handler.do_GET()
		self.assertEqual(captured[0][0], 200)
		html = captured[0][1].decode()
		self.assertIn('<script src="/meeting.js" defer></script>', html)
		self.assertLess(html.index('src="/office.js"'), html.index('src="/meeting.js"'))
		self.assertTrue((server.STATIC / "meeting.js").is_file())
		self.assertIn('id="meeting-dialog" class="action-dialog meeting-dialog"', html)

	def test_limits_tokens_and_no_storage_dialog_calls_or_emoji(self):
		js = (server.STATIC / "meeting.js").read_text()
		css = (server.STATIC / "office.css").read_text().split('.meeting-popover{', 1)[1]
		self.assertLessEqual(len(js.splitlines()), 500)
		self.assertNotRegex(js, r"\b(?:alert|confirm|prompt)\s*\(")
		self.assertNotRegex(js, r"localStorage|sessionStorage|console\.|location\.|https?://")
		self.assertNotRegex(js + css, r"#[0-9a-fA-F]{3,8}\b|(?:rgb|hsl)a?\(")
		self.assertNotRegex(js + css, "[\U0001f000-\U0001faff\u2600-\u27bf]")
		self.assertNotIn('font-family', css)
		self.assertIn('overflow:auto', css)
		self.assertIn('100dvh', css)
		self.assertIn('max-width:600px', css)
