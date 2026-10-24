"""가짜 DOM·fetch·타이머로 서랍·재진입·개발 지시 연결을 확인한다."""

import re
import shutil
import subprocess
import unittest

from office.server import STATIC
from test_office_meeting_ui import HARNESS


SETUP = r"""
let clock=0,serial=0,frame,observer;
const timers=new Map(),gets=[];
sandbox.performance.now=()=>clock;sandbox.requestAnimationFrame=fn=>frame=fn;
sandbox.setInterval=(fn,ms)=>{const id=++serial;timers.set(id,{fn,ms,due:clock+ms,repeat:true});if(ms===10000)interval=fn;return id};
sandbox.clearInterval=id=>timers.delete(id);
sandbox.setTimeout=(fn,ms)=>{const id=++serial;timers.set(id,{fn,ms,due:clock+ms});return id};sandbox.clearTimeout=id=>timers.delete(id);
sandbox.MutationObserver=class{constructor(fn){observer=fn}observe(){}};
const modal=Element.prototype.showModal,closeModal=Element.prototype.close;
Element.prototype.showModal=function(){modal.call(this);observer?.()};Element.prototype.close=function(){closeModal.call(this);observer?.()};
Object.defineProperty(Element.prototype,'scrollHeight',{get(){return this.textContent.length+100}});
let detail={id:MID,status:'running',phase:'round1',agenda:'합성 안건 '+ '가'.repeat(80),target:'xyz',participants:[{role:'backend',name:'백엔드',reason:'서버 검토'},{role:'qa',name:'QA',reason:'시험 검토'},{role:'design',name:'디자인',reason:'화면 검토'}],
	absent:[{role:'design',name:'디자인',phase:'round1',reason:'시간 초과'}],rounds:{'1':[{role:'backend',name:'백엔드',text:'합성 첫 발언'}],'2':[]},summary:null,calls:3,calls_max:14,active_seconds:12,seconds_max:1200,failure:'',updated:'2026-10-24T12:00:00+09:00'};
let getReply=async id=>response({...detail,id});
const originalFetch=sandbox.fetch;
sandbox.fetch=async(url,options)=>{
	if(!url.startsWith('/api/meeting?'))return originalFetch(url,options);
	assert(/^\/api\/meeting\?id=m-[a-f0-9]{32}$/.test(url));assert.equal(options.method,'GET');assert.equal(options.cache,'no-store');assert.equal(options.body,undefined);gets.push(url);
	return getReply(url.split('=')[1]);
};
const flush=async()=>{for(let i=0;i<15;i++)await Promise.resolve()};
async function advance(ms){
	const end=clock+ms;
	while(true){const next=[...timers].filter(([id,t])=>t.due<=end).sort((a,b)=>a[1].due-b[1].due)[0];if(!next)break;
		const [id,t]=next;clock=t.due;if(t.repeat)t.due+=t.ms;else timers.delete(id);await t.fn();await flush();
	}
	clock=end;frame?.(clock);await flush();
}
async function fullBoot(){
	for(const name of ['meeting_scene.js','meeting_panel.js'])vm.runInContext(fs.readFileSync(root+'/'+name,'utf8'),sandbox);
	await boot();await flush();return window.officeMeetingPanel;
}
const summary=()=>({conclusion:['합성 결론'],todo:['합성 할 일 하나','합성 할 일 둘'],open_questions:['합성 열린 질문'],decisions:['합성 결정 후보']});
"""


@unittest.skipUnless(shutil.which("node"), "JavaScript 실행기 없음")
class OfficeMeetingPanelTest(unittest.TestCase):
	def run_js(self, checks):
		result = subprocess.run([shutil.which("node"), "-e", HARNESS + SETUP + checks + "\n})().catch(e=>{console.error(e);process.exitCode=1});",
			str(STATIC)], capture_output=True, text=True, timeout=15)
		self.assertEqual(result.returncode, 0, result.stderr)

	def test_poll_open_close_running_and_finished_stop_conditions(self):
		self.run_js(r"""
		const panel=await fullBoot();assert.equal(gets.length,0);assert($('meeting-dock').hidden);await advance(3000);assert.equal(gets.length,0);
		await panel.open(MID);assert(!$('meeting-dock').hidden);assert.equal(gets.length,1);assert.equal($('meeting-dock-state').textContent,'진행 중');assert.equal($('meeting-dock-calls').textContent,'호출 3/14');
		click($('meeting-toggle'));assert($('meeting-dock-content').hidden);assert.equal($('meeting-toggle').textContent,'펼치기');await advance(3000);assert.equal(gets.length,2);
		click($('meeting-dock-close'));assert($('meeting-dock').hidden);assert($('toast').textContent.includes('계속 진행'));await advance(3000);assert.equal(gets.length,3);
		detail.status='completed';detail.summary=summary();await advance(3000);const stopped=gets.length;await advance(9000);assert.equal(gets.length,stopped);
		await panel.open(MID);const reopened=gets.length;await advance(3000);assert.equal(gets.length,reopened+1);panel.close();const closed=gets.length;await advance(6000);assert.equal(gets.length,closed);
		""")

	def test_initial_running_closed_polls_and_stale_state_does_not_restart_finished(self):
		self.run_js(r"""
		state.meetings.items[0].status='running';state.questions.items=[];const panel=await fullBoot();assert.equal(gets.length,1);assert($('meeting-dock').hidden);
		const actors=window.officeBridge.actors(),actor=actors.find(a=>a.name==='백엔드'),seated={x:actor.x,y:actor.y};assert(!actor.walking);assert.notEqual(actor.x,scene.people.find(p=>p.name===actor.name).x);
		await advance(3000);assert.equal(gets.length,2);state.people[actor.id].status='오류';state.people[actor.id].task='다음 합성 작업';await refresh();
		assert.equal(actor.status,'작업 중');assert(actor.task.startsWith('회의 중 · '));assert.deepEqual({x:actor.x,y:actor.y},seated);
		detail.status='failed';detail.failure='합성 회의 실패';await advance(3000);const stopped=gets.length;await refresh();await advance(3000);assert.equal(gets.length,stopped);
		""")

	def test_participants_speech_append_summary_escape_labels_and_reentry(self):
		self.run_js(r"""
		const panel=await fullBoot();await panel.open(MID);
		assert($('meeting-dock-participants').textContent.includes('백엔드 · 발언 완료'));assert($('meeting-dock-participants').textContent.includes('QA · 대기'));assert($('meeting-dock-participants').textContent.includes('디자인 · 불참(시간 초과)'));
		assert.equal(Array.from($('meeting-dock-title').textContent).length,30);assert($('meeting-dock-content').textContent.includes('닫아도 회의는 서버에서 계속 진행'));
		const first=$('meeting-log-1').children[0];detail.rounds['1'].push({role:'qa',name:'QA',text:'합성 두 번째 발언'});detail.rounds['2'].push({role:'backend',name:'백엔드',text:'동의: 합성 동의\n보완: 합성 보완'});detail.phase='round2';
		await advance(3000);assert($('meeting-dock-participants').textContent.includes('백엔드 · 발언 완료'));assert($('meeting-dock-participants').textContent.includes('QA · 2라운드 대기'));assert.strictEqual($('meeting-log-1').children[0],first);assert.equal($('meeting-log-1').children.length,2);assert($('meeting-log-2').textContent.includes('합성 보완'));assert.equal($('meeting-logs').scrollTop,$('meeting-logs').scrollHeight);
		detail.status='completed';detail.summary=summary();detail.summary.conclusion.push('<script>합성 결론</script>');await advance(3000);
		for(const text of ['결론','할 일','열린 질문','결정 후보','합성 할 일 둘'])assert($('meeting-result').textContent.includes(text));assert.equal($('meeting-result').querySelectorAll('script').length,0);assert(!$('meeting-to-dev').hidden);
		panel.close();state.questions.items=[];
		for(const [code,label] of Object.entries({running:'진행 중',completed:'완료',failed:'실패',budget_exhausted:'예산 소진',questions_exhausted:'질문 한도 소진'})){
			detail.status=code;state.meetings.items[0].status=code;await refresh();click(button('button.room-label[data-room="meeting"]'));click(button('[data-meeting-id]'));await flush();assert(!$('meeting-dock').hidden);assert.equal($('meeting-dock-state').textContent,label);panel.close();
		}
		assert.equal(requests.length,0);
		""")

	def test_start_opens_drawer_immediately_and_uses_persisted_participant_reasons(self):
		self.run_js(r"""
		state.questions.items=[];state.meetings.items[0].status='participants_pending';state.meetings.items[0].participant_reasons={backend:'저장된 합성 서버 이유',qa:'저장된 합성 시험 이유'};
		await fullBoot();window.officeBridge.routeFrom=(x0,y0,x,y)=>[{x,y}];click(button('button.room-label[data-room="meeting"]'));click(button('[data-meeting-id]'));assert($('meeting-reasons').textContent.includes('저장된 합성 서버 이유'));
		reply=async()=>{state.meetings.items[0].status='running';return response({ok:true,draft:{id:MID,status:'running'}})};
		await click($('meeting-submit'));await flush();assert(!$('meeting-dialog').open);assert(!$('meeting-dock').hidden);assert.equal($('meeting-dock-state').textContent,'진행 중');assert.equal(requests.length,1);assert.equal(requests[0].action,'meeting_start');
		assert(window.officeBridge.actors().find(a=>a.name==='백엔드').walking);
		""")

	def test_handoff_prefills_target_and_text_but_waits_for_user_to_write_draft(self):
		self.run_js(r"""
		detail.status='completed';detail.summary=summary();const panel=await fullBoot();await panel.open(MID);
		click($('meeting-to-dev'));assert($('dev-dialog').open);assert($('meeting-dock').hidden);assert($('meeting-to-dev').disabled);
		assert.equal($('dev-target').value,'xyz');assert.equal($('dev-text').value,'안건: '+Array.from(detail.agenda).slice(0,120).join('')+'\n결론: 합성 결론\n할 일: - 합성 할 일 하나\n- 합성 할 일 둘');assert.equal(requests.length,0);assert(!$('dev-write').disabled);
		$('dev-text').value='사용자가 고친 합성 지시';$('dev-text').oninput();await click($('dev-write'));assert.equal(requests.length,1);assert.equal(requests[0].text,'사용자가 고친 합성 지시');assert.equal(requests[0].target,'xyz');assert.equal(requests[0].action,'dev_draft');
		click($('dev-close'));assert(!$('meeting-dock').hidden);assert(!$('meeting-to-dev').disabled);
		detail.target='';await panel.open(MID);click($('meeting-to-dev'));assert.equal($('dev-target').value,'ai-hub');click($('dev-close'));
		const huge={...detail,agenda:'가'.repeat(200),summary:{...summary(),conclusion:['나'.repeat(3000)]}};const text=panel.devText(huge);assert.equal(Array.from(text).length,2000);assert(text.startsWith('안건: '+'가'.repeat(120)+'\n결론: '));
		""")

	def test_read_only_reentry_without_session_keeps_handoff_locked(self):
		self.run_js(r"""
		token=null;detail.status='completed';detail.summary=summary();state.meetings.items[0].status='completed';state.questions.items=[];await fullBoot();
		click(button('button.room-label[data-room="meeting"]'));assert($('meeting-request').disabled);click(button('[data-meeting-id]'));await flush();assert(!$('meeting-dock').hidden);assert($('meeting-to-dev').disabled);assert.equal(window.officeBridge.openDevWith('합성 지시','xyz'),false);assert.equal(requests.length,0);
		""")

	def test_dialog_exclusion_failed_get_and_read_only_reentry_without_session(self):
		self.run_js(r"""
		detail.status='completed';detail.summary=summary();const panel=await fullBoot();await panel.open(MID);$('blog-dialog').showModal();assert($('meeting-dock').hidden);assert.equal(await panel.open(MID),false);$('blog-dialog').close();assert(!$('meeting-dock').hidden);
		getReply=async()=>response({message:'숨길 오류'},503);await advance(3000);assert($('meeting-dock-notice').textContent.includes('읽지 못했습니다'));assert(!$('meeting-dock').textContent.includes('숨길 오류'));
		panel.close();assert.equal(await panel.open('../xyz'),false);
		""")

	def test_refresh_does_not_duplicate_inflight_queries_or_open_another_drawer(self):
		self.run_js(r"""
		const panel=await fullBoot();let resolve;getReply=()=>new Promise(r=>resolve=r);const opening=panel.open(MID);await flush();assert.equal(gets.length,1);
		const again=panel.open(MID);await flush();assert.equal(gets.length,1);panel.close();resolve(response(detail));await opening;await again;assert($('meeting-dock').hidden);
		getReply=async id=>response({...detail,id});await panel.open('m-'+'b'.repeat(32));assert(!$('meeting-dock').hidden);assert.equal(document.querySelectorAll('section.meeting-dock').length,1);
		""")

	def test_scene_bridge_keeps_paths_open_and_refresh_failure_preserves_meeting(self):
		self.run_js(r"""
		await fullBoot();const bridge=window.officeBridge,path=plain(bridge.routeFrom(610,650,650,650));assert(path.length);assert(path.every(p=>bridge.walkable(p.x,p.y)));assert.equal(bridge.chairs().length,8);
		let ticks=0;const remove=bridge.tickHook(()=>ticks++);frame(100);assert.equal(ticks,1);remove();frame(200);assert.equal(ticks,1);
		await window.officeMeetingPanel.open(MID);assert.deepEqual(plain(bridge.routeFrom(610,650,650,650)),path);
		const actor=bridge.actors().find(a=>a.name==='백엔드');await advance(3000);const seat={x:actor.x,y:actor.y};apiOK=false;await refresh();assert.deepEqual({x:actor.x,y:actor.y},seat);assert.equal(actor.status,'작업 중');assert(actor.task.startsWith('회의 중 · '));
		""")

	def test_static_wiring_limits_existing_tokens_and_no_forbidden_calls(self):
		html = (STATIC / "index.html").read_text()
		for name in ("meeting_panel.js", "meeting_scene.js"):
			self.assertIn('<script src="/' + name + '" defer></script>', html)
			js = (STATIC / name).read_text()
			self.assertLessEqual(len(js.splitlines()), 500)
			self.assertNotRegex(js, r"\b(?:alert|confirm|prompt)\s*\(|localStorage|sessionStorage|console\.")
			self.assertNotRegex(js, r"#[0-9a-fA-F]{3,8}\b|(?:rgb|hsl)a?\(")
			self.assertNotRegex(js, "[\U0001f000-\U0001faff\u2600-\u27bf]")
		css = (STATIC / "office.css").read_text().split('.meeting-dock{', 1)[1]
		self.assertNotRegex(css, r"#[0-9a-fA-F]{3,8}\b|(?:rgb|hsl)a?\(|font-family")
		for expected in ("min-height:72px", "40dvh", "70dvh", "right:344px", "safe-area-inset-bottom", "dialog[open]"):
			self.assertIn(expected, css)
		known = set(re.findall(r"--([\w-]+):", (STATIC / "office.css").read_text().split('*{', 1)[0]))
		self.assertLessEqual(set(re.findall(r"var\(--([\w-]+)\)", css)), known)
