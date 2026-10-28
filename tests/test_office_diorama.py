"""임시 원장과 정적 자산의 화면 연결 경계를 확인한다."""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from office import projects
from test_office import OfficeCase


class DioramaStateTest(OfficeCase):
	def setUp(self):
		super().setUp()
		patch = mock.patch("socket.create_connection", side_effect=OSError("합성 연결 거부"))
		patch.start()
		self.addCleanup(patch.stop)

	def record(self, number, status):
		identity = f"p-20261028-{number:03d}"
		folder = self.hub / "projects" / identity
		folder.mkdir(parents=True)
		row = {"version": 1, "id": identity, "title": "합성 프로젝트", "department": "dev", "status": status,
			"tasks": [{"status": "완료", "title": "비공개 작업"}, {"status": "대기"}],
			"budget": {"calls": 8, "minutes": 60}, "used": {"calls": 3, "seconds": 72},
			"blocked_reason": "검수 실패" if status == "막힘" else "",
			"request_text": "비공개 요청 본문", "spec": {"goal": "비공개 명세 본문"},
			"decisions": [{"topic": "비공개 결정 본문"}]}
		path = folder / "project.json"
		path.write_text(json.dumps(row, ensure_ascii=False))
		return folder, path

	def test_read_only_summary_and_question_presence_without_private_contents(self):
		folder, _ = self.record(1, "접수 중")
		self.record(2, "납품 대기")
		self.record(3, "막힘")
		(folder / "questions.json").write_text(json.dumps({"next_no": 2, "open": [
			{"no": 1, "question": "비공개 질문 본문", "asked": "2026-10-28T10:00:00+09:00"}]}, ensure_ascii=False))
		before = self.snapshot()
		state = self.state()["projects"]
		self.assertEqual(state["status"], "정상")
		first, delivery, blocked = state["items"]
		self.assertTrue(first["question_waiting"])
		self.assertEqual((first["completed"], first["total"]), (1, 2))
		self.assertEqual((first["used"], first["budget"]), ({"calls": 3, "seconds": 72}, {"calls": 8, "minutes": 60}))
		self.assertEqual(delivery["status"], "납품 대기")
		self.assertEqual(blocked["blocked_reason"], "검수 실패")
		payload = json.dumps(state, ensure_ascii=False)
		for private in ("비공개 요청 본문", "비공개 명세 본문", "비공개 결정 본문", "비공개 질문 본문", "비공개 작업"):
			self.assertNotIn(private, payload)
		self.assertEqual(self.snapshot(), before)

	def test_missing_questions_and_invalid_ledger_are_reported_safely(self):
		folder, path = self.record(1, "접수 중")
		self.assertFalse(projects.read_projects()["items"][0]["question_waiting"])
		(folder / "questions.json").write_text("잘못된 자료")
		self.assertEqual(projects.read_projects()["status"], "확인 불가")
		(folder / "questions.json").unlink()
		row = json.loads(path.read_text())
		row["used"]["seconds"] = float("nan")
		path.write_text(json.dumps(row))
		self.assertEqual(projects.read_projects()["status"], "확인 불가")


SCENE_HARNESS = r'''
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const dir=path.resolve('office/static');
const worldSource=fs.readFileSync(path.join(dir,'diorama-world.js'),'utf8')+'\nexport { walks, inWater };';
const worldURL='data:text/javascript;base64,'+Buffer.from(worldSource).toString('base64');
let sceneSource=fs.readFileSync(path.join(dir,'diorama.js'),'utf8')
	.replace("import * as THREE from '/vendor/three-0.160.0.module.min.js';",`import * as Real from '${pathToFileURL(path.join(dir,'vendor/three-0.160.0.module.min.js')).href}';\nconst THREE={...Real,WebGLRenderer:globalThis.FakeRenderer};`)
	.replace("'/diorama-world.js'",JSON.stringify(worldURL));
sceneSource+='\nexport { findWalkRoute, canWalkBetween, walkArea, project, moveOwnerTo, syncState, updateWater, updateOwner, resize };\nexport function testState(){const p=person("owner");return {owner:[p.x+.45,p.y+.45],target:ownerTarget,zoom:goal.zoom,pan:[goal.panX,goal.panZ],reduced,waterClock}}';
class El {
	constructor(name){this.name=name;this.style={};this.hidden=false;this.dataset={};this.listeners={};this.nodes={};this.offsetWidth=70;this.offsetHeight=30;this.classList={set:new Set(),add(x){this.set.add(x)},remove(x){this.set.delete(x)},contains(x){return this.set.has(x)}}}
	addEventListener(type,fn){this.listeners[type]=fn} append(...args){} replaceChildren(){} setAttribute(){} closest(){return null} getBoundingClientRect(){return {width:globalThis.testWidth||1280,height:720,left:0,top:0}} setPointerCapture(){} focus(){}
	querySelector(s){return this.nodes[s]??=new El(s)}
	getContext(){return new Proxy({}, {get:(obj,key)=>obj[key]??(()=>{}) ,set:(obj,key,value)=>(obj[key]=value,true)})}
}
const nodes=new Map(); const get=s=>{if(!nodes.has(s))nodes.set(s,new El(s));return nodes.get(s)};
const listeners={};
globalThis.document={querySelector(s){return s==='dialog[open]'?null:get(s)},getElementById(id){return get('#'+id)},createElement(type){return new El(type)},addEventListener(type,fn){(listeners[type]??=[]).push(fn)},dispatchEvent(event){for(const fn of listeners[event.type]||[])fn(event)},body:get('body')};
let count=2,opened=0,toasts=[];
document.addEventListener('office-open-approvals',()=>opened++);
globalThis.window={addEventListener(type,fn){(listeners['window:'+type]??=[]).push(fn)},officeBridge:{state:()=>null,toast(t){toasts.push(t)},esc:s=>s,openPanel(){}}};
globalThis.matchMedia=query=>({matches:query.includes('prefers-reduced-motion')&&!process.env.TEST_MOTION});globalThis.devicePixelRatio=1;
globalThis.ResizeObserver=class{observe(){}};
let frames=[];globalThis.requestAnimationFrame=cb=>(frames.push(cb),frames.length);globalThis.cancelAnimationFrame=()=>{};
globalThis.FakeRenderer=class{constructor(){if(process.env.TEST_NO_WEBGL)throw new Error('WebGL 없음')} setPixelRatio(){} setSize(){} render(){globalThis.renders=(globalThis.renders||0)+1}};
const scene=await import('data:text/javascript;base64,'+Buffer.from(sceneSource).toString('base64'));
const world=await import(worldURL);
if(process.env.TEST_NO_WEBGL){assert(!get('#world').classList.contains('diorama-active'));assert.equal(get('#diorama-error').hidden,false);process.exit(0)}
assert(get('#world').classList.contains('diorama-active'),'장면 초기화 실패');
assert.equal(world.rooms.length,12);assert.equal(world.staff.length,26);assert(world.cubes.length>9000);
assert(world.cubes.every(b=>[b.x,b.y,b.z,b.w,b.d,b.h].every(Number.isFinite)&&b.w>0&&b.d>0&&b.h>0&&/^#[a-f\d]{6}$/i.test(b.color)));
assert(world.cubes.some(b=>b.walkable==='path'));assert(world.cubes.some(b=>b.blocked));
let f=frames.shift();f(100);assert.equal(globalThis.renders,1);
get('#mailbox-marker').dataset.pending=String(count);scene.syncState({people:[]});f=frames.shift();f(120);assert.equal(get('#mailbox-marker').hidden,false);
get('#mailbox-marker').onclick();assert.equal(opened,1);
count=0;get('#mailbox-marker').dataset.pending=String(count);scene.syncState({people:[]});f=frames.shift();f(140);assert.equal(get('#mailbox-marker').hidden,true);
for(const points of Object.values(world.walks)){
	const a=points[0],b=points.at(-1);assert(scene.findWalkRoute(a[0],a[1],b[0],b[1])?.length>0);
}
document.body.classList.add('panel-open');get('#diorama-zoom-in').onclick();
const before=scene.testState();for(const fn of listeners['window:keydown'])fn({key:'s',target:new El('target'),preventDefault(){}});
for(let i=0;i<15&&frames.length;i++){f=frames.shift();f(160+i*16)}
for(const fn of listeners['window:keyup'])fn({key:'s'});
assert.notDeepEqual(before.owner,scene.testState().owner);assert.equal(scene.testState().reduced,!process.env.TEST_MOTION);
assert.notDeepEqual(scene.testState().pan,[0,0]);
const waterBefore=scene.testState().waterClock;scene.updateWater(.25);
assert.equal(scene.testState().waterClock>waterBefore,!!process.env.TEST_MOTION);
let r=world.stream[0],pt={x:r.x+r.w/2,y:r.y+1,z:r.z+.16},v=scene.project(pt.x,pt.y,pt.z);
assert.equal(scene.walkArea(pt.x,pt.y),null);scene.moveOwnerTo(v.x,v.y);
assert.equal(scene.testState().target,null);assert(toasts.includes('방, 길 또는 다리를 선택해 주세요.'));
const point=world.walks.owner[4],screen=scene.project(...point);
const touch={button:0,pointerId:7,clientX:screen.x,clientY:screen.y,shiftKey:false,pointerType:'touch',preventDefault(){}};
get('#diorama-canvas').listeners.pointerdown(touch);get('#diorama-canvas').listeners.pointerup(touch);
assert(Math.abs(scene.testState().target.x-point[0])<.1);
const office=fs.readFileSync(path.join(dir,'office.js'),'utf8');
const approvalSource=office.slice(office.indexOf('function approvals(){'),office.indexOf('const activityHTML='));
const collect=new Function('state','window',"const items=group=>Array.isArray(group?.items)?group.items:[];"+approvalSource+";return {rows:approvals(),count:approvalCount()}");
const sample={approvals:{c4:{items:[{id:'x'}]},tasks:{items:[{id:'t'}]}},projects:{items:[{id:'p1',title:'가',status:'납품 대기'},{id:'p2',title:'나',status:'접수 중',question_waiting:true}]}};
const inbox=collect(sample,{officeMeeting:{approvals:()=>[{kind:'question'}]}});
assert.equal(inbox.rows.length,5);assert.equal(inbox.count,4);
assert.equal(collect({approvals:{tasks:{items:[{id:'t'}]}},projects:{items:[]}},{}) .count,0);
for(const width of [390,1280]){
	globalThis.testWidth=width;count=1;get('#mailbox-marker').dataset.pending=String(count);scene.resize();scene.syncState({people:[]});
	if(frames.length){f=frames.shift();f(1000+width)}
	const marker=get('#mailbox-marker');
	if(!marker.hidden)assert(Number.parseFloat(marker.style.left)<=width-25);
}
'''


class DioramaSceneTest(unittest.TestCase):
	def test_new_screen_assets_use_local_sources(self):
		static = Path(__file__).resolve().parents[1] / "office/static"
		for name in ("index.html", "office.css", "diorama.js", "diorama-world.js"):
			with self.subTest(name=name):
				self.assertNotRegex((static / name).read_text(), r"https?://")

	@unittest.skipUnless(shutil.which("node"), "Node 실행기가 없습니다")
	def test_scene_navigation_water_mailbox_motion_and_fallback(self):
		for mode in ("reduced", "normal", "no-webgl"):
			with self.subTest(mode=mode):
				env = dict(os.environ)
				env.pop("TEST_NO_WEBGL", None)
				env.pop("TEST_MOTION", None)
				if mode == "no-webgl":
					env["TEST_NO_WEBGL"] = "1"
				elif mode == "normal":
					env["TEST_MOTION"] = "1"
				result = subprocess.run([shutil.which("node"), "--no-warnings", "--input-type=module", "-e", SCENE_HARNESS],
					cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=30)
				self.assertEqual(result.returncode, 0, result.stderr[-3000:])
