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
const mobileMode=process.argv[2]==='mobile',sessionOK=process.argv[2]!=='no-session';
const labels=new Map(),events=new Map(),elements=new Map(),classes=new Set();
let frame,interval,apiOK=true,requests=[],actionResponse,actionReject,actionCount=0,actionBody;
const context2d=id=>new Proxy({measureText:text=>({width:text.length*7}),fillText:(text,x,y)=>{if(id==='map')labels.set(text,[x,y])}},
	{get:(object,key)=>key in object?object[key]:(()=>{}),set:(object,key,value)=>{object[key]=value;return true}});
class Element{
	constructor(id=''){this.id=id;this.hidden=id==='side'||id==='nearby';this.open=false;this.disabled=false;this.dataset={};this.style={setProperty(){}};this.innerHTML='';this.textContent='';this.scrollTop=0;this.ctx=context2d(id);this.listeners=new Map()}
	getContext(){return this.ctx}getBoundingClientRect(){return {width:1000,height:650,left:0,top:0}}
	setAttribute(){}append(){}setPointerCapture(){}querySelectorAll(){return []}
	addEventListener(name,fn){this.listeners.set(name,fn)}focus(){document.activeElement=this}
	showModal(){this.open=true}close(){this.open=false}
}
const document={getElementById:id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id)},
	createElement:()=>new Element(),querySelectorAll:()=>[],visibilityState:'visible',
	addEventListener:(name,fn)=>events.set(name,fn),documentElement:new Element(),
	body:{classList:{add:value=>classes.add(value),remove:value=>classes.delete(value),contains:value=>classes.has(value)}}};
let state={updated:'2026-10-24T12:00:00+09:00',people:scene.people.map(p=>({...p,status:'작업 중',engine:'Codex',task:'합성 작업',updated:null,project:p.department==='company'?'p01':'',duration:60})),
	approvals:{c4:{status:'정상',items:[{id:'c4-'+'a'.repeat(16),prepared:3,held:1,reply:'C4 합성 반영해'}]},
		runs:{status:'정상',items:[{id:'2026-10-24-001',status:'승인 필요'}]},r1:{status:'정상',items:[{id:'AI & Prompts',pending:2}]},
		decisions:{status:'정상',items:[{id:'d01',title:'합성 결정'}]}},
	usage:{c4:{status:'정상',subscription:3,local:5},limit:{status:'정상',daily_limit_calls:20},plans:{status:'정상',items:[]},notebooklm:{count:null}},
	schedule:{status:'정상',items:[{title:'합성 후속 일정',source:'개인',due:'2026-10-26'}]},
	activity:[],board:{status:'정상',items:[{kind:'chat',title:'결정',text:'합성 결정',at:'2026-10-24T12:00:00+09:00'}],progress:{items:[]}},engines:[]};
const sandbox={document,window:{addEventListener(){}},matchMedia:query=>({matches:mobileMode&&query.includes('900px')}),innerWidth:mobileMode?390:1200,devicePixelRatio:1,
	performance:{now:()=>100},requestAnimationFrame:fn=>{frame=fn},ResizeObserver:class{observe(){}},AbortController,
	console,setTimeout:()=>1,clearTimeout(){},setInterval:(fn,ms)=>{interval=fn;assert.equal(ms,10000)},
	fetch:async(url,options)=>{
		requests.push(url);
		if(url==='/api/session')return {ok:sessionOK,json:async()=>({token:'xyz-session'})};
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
const click=data=>events.get('click')({target:{closest:()=>({dataset:data})}});
(async()=>{
	await vm.runInContext(fs.readFileSync(root+'/office.js','utf8'),sandbox);
	assert.deepEqual(requests,['/scene.json','/api/session','/api/state']);
	if(!sessionOK){
		click({view:'approvals'});
		assert(element('panel-body').innerHTML.includes('반영 세션을 읽지 못했습니다.'));
		assert.equal((element('panel-body').innerHTML.match(/class="apply-button"[^>]* disabled/g)||[]).length,3);
		click({action:'c4',id:'c4-'+'a'.repeat(16)});
		assert(!element('action-dialog').open);assert.equal(actionCount,0);return;
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
	assert(element('action-description').textContent.includes('준비 3건'));
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
	def test_original_pixel_art_and_22_seat_names_are_preserved(self):
		art = "\n".join(line.strip() for line in (STATIC / "pixel-art.js").read_text().strip().splitlines())
		self.assertEqual(hashlib.sha256(art.encode()).hexdigest(), "6c4490d3b0da307cf4dc916aa8b5c5f168cb11fa9361eb5e8cdadfd52d2640fe")
		scene = json.loads((STATIC / "scene.json").read_text())
		self.assertEqual([p["name"] for p in scene["people"]], ["PM", "기록", "총무", "기획", "백엔드", "프론트", "디자인", "QA", "작가", "분석", "클립", "티스토리", "PD", "대본", "썸네일", "회사 개발", "레거시 분석", "이관 QA", "회사 기록", "이웃", "편집", "음악"])
		self.assertEqual([p["seat"] for p in scene["people"]], list(range(1, 23)))
		self.assertEqual(sum(p["type"] == "desk" for p in scene["props"]), 25)
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
		for word in ("이 안 선택", "시안", "예시", 'id="choose"', 'id="copy-dialog"'):
			self.assertNotIn(word, html)

	@unittest.skipUnless(shutil.which("node"), "JavaScript 실행기 없음")
	def test_fetch_refresh_panel_retention_error_and_fixed_actor_positions_without_browser(self):
		for mode in ("desktop", "mobile", "no-session"):
			with self.subTest(mode=mode):
				result = subprocess.run([shutil.which("node"), "-e", HARNESS, str(STATIC), mode], capture_output=True, text=True, timeout=10)
				self.assertEqual(result.returncode, 0, result.stderr)
