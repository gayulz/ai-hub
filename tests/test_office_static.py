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
const labels=new Map(),events=new Map(),elements=new Map(),classes=new Set();
let frame,interval,apiOK=true,requests=[];
const context2d=id=>new Proxy({measureText:text=>({width:text.length*7}),fillText:(text,x,y)=>{if(id==='map')labels.set(text,[x,y])}},
	{get:(object,key)=>key in object?object[key]:(()=>{}),set:(object,key,value)=>{object[key]=value;return true}});
class Element{
	constructor(id=''){this.id=id;this.hidden=id==='side'||id==='nearby';this.dataset={};this.style={setProperty(){}};this.innerHTML='';this.textContent='';this.scrollTop=0;this.ctx=context2d(id);this.listeners=new Map()}
	getContext(){return this.ctx}getBoundingClientRect(){return {width:1000,height:650,left:0,top:0}}
	setAttribute(){}append(){}setPointerCapture(){}querySelectorAll(){return []}
	addEventListener(name,fn){this.listeners.set(name,fn)}focus(){document.activeElement=this}
}
const document={getElementById:id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id)},
	createElement:()=>new Element(),querySelectorAll:()=>[],visibilityState:'visible',
	addEventListener:(name,fn)=>events.set(name,fn),documentElement:new Element(),
	body:{classList:{add:value=>classes.add(value),remove:value=>classes.delete(value),contains:value=>classes.has(value)}}};
let state={updated:'2026-10-24T12:00:00+09:00',people:scene.people.map(p=>({...p,status:'작업 중',engine:'Codex',task:'합성 작업',updated:null,project:p.department==='company'?'p01':'',duration:60})),
	approvals:{c4:{status:'정상',items:[]},runs:{status:'정상',items:[]},decisions:{status:'정상',items:[]}},
	usage:{c4:{status:'정상',subscription:3,local:5},limit:{status:'정상',daily_limit_calls:20},plans:{status:'정상',items:[]},notebooklm:{count:null}},
	schedule:{status:'정상',items:[{title:'합성 후속 일정',source:'개인',due:'2026-10-26'}]},
	activity:[],board:{status:'정상',items:[{kind:'chat',title:'결정',text:'합성 결정',at:'2026-10-24T12:00:00+09:00'}],progress:{items:[]}},engines:[]};
const sandbox={document,window:{addEventListener(){}},matchMedia:()=>({matches:false}),innerWidth:1200,devicePixelRatio:1,
	performance:{now:()=>100},requestAnimationFrame:fn=>{frame=fn},ResizeObserver:class{observe(){}},AbortController,
	console,setTimeout:()=>1,clearTimeout(){},setInterval:(fn,ms)=>{interval=fn;assert.equal(ms,10000)},
	fetch:async url=>{requests.push(url);return {ok:url==='/scene.json'||apiOK,json:async()=>url==='/scene.json'?scene:state}}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(root+'/pixel-art.js','utf8'),sandbox);
const element=id=>document.getElementById(id);
const click=data=>events.get('click')({target:{closest:()=>({dataset:data})}});
(async()=>{
	await vm.runInContext(fs.readFileSync(root+'/office.js','utf8'),sandbox);
	assert.deepEqual(requests,['/scene.json','/api/state']);
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
		for word in ("이 안 선택", "시안", "예시", 'id="choose"', 'id="copy-dialog"'):
			self.assertNotIn(word, html)

	@unittest.skipUnless(shutil.which("node"), "JavaScript 실행기 없음")
	def test_fetch_refresh_panel_retention_error_and_fixed_actor_positions_without_browser(self):
		result = subprocess.run([shutil.which("node"), "-e", HARNESS, str(STATIC)], capture_output=True, text=True, timeout=10)
		self.assertEqual(result.returncode, 0, result.stderr)
