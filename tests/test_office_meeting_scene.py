"""가짜 시계와 경로로 의자 배정·걷기·복귀를 검사한다."""

import shutil
import subprocess
import unittest

from office.server import STATIC


HARNESS = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const sandbox={window:{},document:{addEventListener(){}}};vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1]+'/meeting_scene.js','utf8'),sandbox);
const api=sandbox.window.officeMeetingScene,plain=v=>JSON.parse(JSON.stringify(v));
const MID='m-'+'a'.repeat(32),chairs=[{x:100,y:100},{x:160,y:100},{x:100,y:200}],roles=[{role:'backend',name:'백엔드'},{role:'qa',name:'QA'},{role:'plan',name:'기획'}];
let clock=0,reduced=false,noPath=false;
const actors=roles.map((p,i)=>({id:i,name:p.name,x:10+i*30,y:20,face:'down',walking:false,status:'대기',task:'기존 합성 작업',...(i===0?{facing:'back'}:{})}));
const homes=plain(actors),bridge={actors:()=>actors,chairs:()=>chairs,state:()=>({meeting_roles:roles.map(p=>({key:p.role,name:p.name}))}),
	walkable:()=>true,routeFrom:(x0,y0,x,y)=>noPath?[]:[{x,y}],reducedMotion:()=>reduced};
const controller=api.createController(bridge,()=>clock);
const record=(status='running')=>({id:MID,status,agenda:'합성 안건입니다 길게 표시합니다',participants:roles,absent:[]});
"""


@unittest.skipUnless(shutil.which("node"), "JavaScript 실행기 없음")
class OfficeMeetingSceneTest(unittest.TestCase):
	def run_js(self, checks):
		result = subprocess.run([shutil.which("node"), "-e", HARNESS + checks, str(STATIC)], capture_output=True, text=True, timeout=10)
		self.assertEqual(result.returncode, 0, result.stderr)

	def test_seat_assignment_is_name_sorted_unique_and_uses_walkable_spares(self):
		self.run_js(r"""
		const extra=Array.from({length:9},(_,i)=>({role:'p'+i,name:'합성 '+i}));
		const seats=api.assignSeats([...roles,...extra],chairs,(x,y)=>x!==80);
		assert.deepEqual(plain(seats),plain(api.assignSeats([...extra,...roles].reverse(),chairs,(x,y)=>x!==80)));
		assert.equal(new Set(seats.map(p=>p.x+','+p.y)).size,seats.length);
		assert(seats.slice(chairs.length).every(p=>p.x!==80));
		const first=api.assignSeats(roles,chairs);assert.equal(first[0].name,'기획');assert.equal(first[0].x,112);assert.equal(first[0].y,125);
		assert.equal(first[0].face,'down');assert(first.every(p=>['up','down','left','right'].includes(p.face)));
		""")

	def test_step_speed_direction_waypoints_and_input_are_pure(self):
		self.run_js(r"""
		const start={x:0,y:0,face:'down'},path=[{x:45,y:0},{x:45,y:90}],copy=plain(path);
		const next=api.step(start,path,1);assert.equal(next.x,45);assert.equal(next.y,45);assert.equal(next.face,'down');assert(next.walking);
		assert.deepEqual(plain(path),copy);assert.equal(start.x,0);
		const end=api.step(next,next.path,1);assert.equal(end.y,90);assert(!end.walking);assert.equal(end.path.length,0);
		assert.equal(api.step({x:5,y:0},[{x:0,y:0}],.01).face,'left');
		""")

	def test_walks_then_three_second_fallback_seats_and_absent_do_not_move(self):
		self.run_js(r"""
		const row=record();row.absent=[{role:'qa',reason:'합성 불참'}];controller.sync(row);
		assert(actors[0].walking);assert.equal(actors[0].facing,undefined);assert.deepEqual(plain(actors[1]),homes[1]);
		const x=actors[0].x;clock=1000;controller.tick(clock,.1);assert.notEqual(actors[0].x,x);
		clock=2999;controller.tick(clock,0);assert(actors[0].walking);
		clock=3000;controller.tick(clock,0);assert(!actors[0].walking);
		assert.equal(actors[0].status,'작업 중');assert.equal(actors[0].task,'회의 중 · '+Array.from(row.agenda).slice(0,12).join(''));
		const seated=plain(actors[0]);controller.sync(row);assert.deepEqual(plain(actors[0]),seated);
		""")

	def test_no_path_reduced_motion_and_initial_running_seat_immediately(self):
		for setup, initial in (("noPath=true;", "false"), ("reduced=true;", "false"), ("", "true")):
			with self.subTest(setup=setup, initial=initial):
				self.run_js(setup + "controller.sync(record()," + initial + ");" + r"""
				const seats=api.assignSeats(roles,chairs);
				for(const actor of actors){const seat=seats.find(p=>p.name===actor.name);assert.equal(actor.x,seat.x);assert.equal(actor.y,seat.y);assert.equal(actor.face,seat.face);assert(!actor.walking)}
				""")

	def test_each_terminal_status_waits_four_seconds_and_returns_with_same_safety(self):
		for status in ("completed", "failed", "budget_exhausted", "questions_exhausted"):
			with self.subTest(status=status):
				self.run_js("const terminal='" + status + "';" + r"""
				controller.sync(record(),true);const seated=plain(actors);
				controller.sync(record(terminal));clock=3999;controller.tick(clock,0);assert.deepEqual(plain(actors),seated);
				controller.sync(record(terminal));controller.sync(record());
				clock=4000;controller.tick(clock,0);assert(actors.every(a=>a.walking));
				clock=6999;controller.tick(clock,0);assert(actors.every(a=>a.walking));
				clock=7000;controller.tick(clock,0);assert.deepEqual(plain(actors),homes);
				controller.sync(record(terminal));clock=20000;controller.tick(clock,0);assert.deepEqual(plain(actors),homes);
				""")

	def test_return_honors_reduced_motion_and_latest_signals(self):
		self.run_js(r"""
		controller.sync(record(),true);
		controller.captureSignals({people:actors.map(a=>({id:a.id,status:'오류',task:'새 합성 작업'}))});
		assert(actors.every(a=>a.status==='작업 중'));
		controller.sync(record('completed'));clock=4000;reduced=true;controller.tick(clock,0);
		for(let i=0;i<actors.length;i++){assert.equal(actors[i].x,homes[i].x);assert.equal(actors[i].y,homes[i].y);assert.equal(actors[i].status,'오류');assert.equal(actors[i].task,'새 합성 작업')}
		""")

	def test_safety_timer_grows_with_path_length_and_is_capped(self):
		self.run_js(r"""
		bridge.routeFrom=(x0,y0,x,y)=>Array.from({length:30},(_,i)=>({x:x0+i*16,y:y0}));
		controller.sync(record());assert(actors[0].walking);
		clock=3000;controller.tick(clock,0);assert(actors[0].walking,'긴 길은 3초에 보정되면 안 된다');
		clock=3000+29*16/90*1000-1;controller.tick(clock,0);assert(actors[0].walking);
		clock=3000+29*16/90*1000+1;controller.tick(clock,0);assert(!actors[0].walking);
		""")

	def test_no_meeting_changes_nothing_and_late_absent_returns(self):
		self.run_js(r"""
		clock=9000;controller.tick(clock,.04);controller.sync(record('participants_pending'));assert.deepEqual(plain(actors),homes);
		controller.sync(record(),true);const row=record();row.absent=[{role:'qa'}];controller.sync(row);assert(actors[1].walking);
		clock+=3000;controller.tick(clock,0);assert.deepEqual(plain(actors[1]),homes[1]);assert.equal(actors[0].status,'작업 중');
		""")
