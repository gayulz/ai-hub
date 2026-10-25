"""합성 대화로 서랍·폴링·말풍선·기존 개발 지시 연결을 검사한다."""

import json
import re
import shutil
import subprocess
import unittest

from office.server import STATIC
from test_office_chat_ui import HARNESS


@unittest.skipUnless(shutil.which("node"), "JavaScript 실행기 없음")
class OfficeChatDockTest(unittest.TestCase):
	def run_js(self, checks):
		result = subprocess.run([shutil.which("node"), "-e", HARNESS + checks + "\n})().catch(e=>{console.error(e);process.exitCode=1});",
			str(STATIC)], capture_output=True, text=True, timeout=15)
		self.assertEqual(result.returncode, 0, result.stderr)

	def test_logs_escaped_roles_sources_append_scrolling_status_and_collapse(self):
		self.run_js(r"""
		chatDetail.turns=[{role:'user',text:'합성 <script>글</script>\n다음 줄',source:'office'},{role:'assistant',text:'합성 <img src=x onerror="공격"> 답',source:'telegram'}];
		chatDetail.calls=2;chatDetail.needs_user='합성 확인 <script>필요</script>';
		const panel=await bootChat();await panel.open(CHAT);assert.equal($('chat-dock-title').textContent,'대화 · xyz');assert.equal($('chat-dock-ai').textContent,'p01 · codex');assert.equal($('chat-dock-state').textContent,'대기');assert.equal($('chat-dock-calls').textContent,'호출 2');
		const log=$('chat-log');assert.equal(log.children.length,2);assert.equal(log.querySelectorAll('script,img').length,0);assert(log.textContent.includes('사무실'));assert(log.textContent.includes('텔레그램'));assert(log.textContent.includes('\n다음 줄'));assert(log.children[0].className.includes('chat-user'));assert(log.children[1].className.includes('chat-assistant'));assert.equal(log.scrollTop,log.scrollHeight);assert($('chat-needs-user').textContent.includes('사용자 확인 필요: 합성 확인'));assert.equal($('chat-needs-user').children.length,0);
		const first=log.children[0];chatDetail.turns.push({role:'assistant',text:'합성 추가 답',source:'cli'});await panel.read(CHAT);assert(log.children[0]===first);assert(log.textContent.includes('명령줄'));assert.equal(log.scrollTop,log.scrollHeight);
		log.scrollTop=0;chatDetail.turns.push({role:'assistant',text:'긴 합성 답'.repeat(150),source:'office'});await panel.read(CHAT);assert.equal(log.scrollTop,0);
		click($('chat-toggle'));assert($('chat-dock-content').hidden);assert.equal($('chat-toggle').getAttribute('aria-expanded'),'false');click($('chat-toggle'));assert(!$('chat-dock-content').hidden);
		const idleGets=chatGets.length;await advance(3000);assert.equal(chatGets.length,idleGets);
		""")

	def test_send_exact_payload_busy_poll_success_idle_stop_and_user_input(self):
		self.run_js(r"""
		const panel=await bootChat();await panel.open(CHAT);assert($('chat-send').disabled);edit('chat-text','합성 질문\n둘째 줄');assert(!$('chat-send').disabled);assert.equal($('chat-remaining').textContent,(2000-Array.from($('chat-text').value).length)+'자 남음');
		let resolve;reply=()=>new Promise(r=>resolve=r);const posting=click($('chat-send'));assert($('chat-send').disabled&&$('chat-text').disabled);assert.deepEqual(requests.at(-1),{action:'chat_send',id:CHAT,text:'합성 질문\n둘째 줄'});assert.equal($('chat-text').value,'합성 질문\n둘째 줄');
		chatDetail.status='busy';chatDetail.calls=1;resolve(response({ok:true,message:'답을 쓰는 중입니다'}));await posting;assert.equal($('chat-text').value,'');assert.equal($('chat-dock-state').textContent,'답을 쓰는 중');assert($('chat-send').disabled);const before=chatGets.length;
		await advance(2999);assert.equal(chatGets.length,before);await advance(1);assert.equal(chatGets.length,before+1);assert.equal($('chat-dock-calls').textContent,'호출 1');
		chatDetail.status='idle';chatDetail.turns=[{role:'user',text:'합성 질문\n둘째 줄',source:'office'},{role:'assistant',text:'합성 답변',source:'office'}];await advance(3000);assert.equal($('chat-dock-state').textContent,'대기');assert($('chat-log').textContent.includes('합성 답변'));assert(![...timers.values()].some(t=>t.repeat&&t.ms===3000));const stopped=chatGets.length;await advance(3000);assert.equal(chatGets.length,stopped);
		edit('chat-text','다음 질문');assert(!$('chat-send').disabled);await refresh();assert.equal($('chat-text').value,'다음 질문');
		""")

	def test_all_send_disabled_reasons_length_limit_failure_allows_retry_and_closed_view(self):
		self.run_js(r"""
		const panel=await bootChat();await panel.open(CHAT);assert($('chat-send').disabled);assert($('chat-send-reason').textContent.includes('적어 주세요'));edit('chat-text','합성 글');
		chatDetail.status='busy';await panel.read(CHAT);assert($('chat-send').disabled);assert($('chat-send-reason').textContent.includes('답을 쓰는 중'));
		chatDetail.status='idle';waiting();await panel.read(CHAT);assert($('chat-send').disabled);assert($('chat-send-reason').textContent.includes('질문 카드'));assert(!$('chat-question').hidden);
		chatDetail.card.status='answered';await panel.read(CHAT);assert($('chat-send').disabled);assert.equal($('chat-question').textContent,'답변 확인');
		chatDetail.card.status='confirmed';await panel.read(CHAT);assert(!$('chat-send').disabled);
		const getToken=window.officeBridge.token;window.officeBridge.token=()=>null;await refresh();await flush();assert($('chat-send').disabled);assert($('chat-send-reason').textContent.includes('세션'));assert($('chat-text').disabled);window.officeBridge.token=getToken;
		chatDetail.status='closed';await panel.read(CHAT);assert($('chat-composer').hidden);assert($('chat-send').disabled);assert.equal($('chat-dock-state').textContent,'닫힘');
		chatDetail.status='idle';chatDetail.card=null;chatDetail.failure='보이면 안 되는 원문';await panel.read(CHAT);assert(!$('chat-composer').hidden);assert(!$('chat-send').disabled);assert($('chat-failure').textContent.includes('다시 보내'));assert(!$('chat-failure').textContent.includes('원문'));chatDetail.failure_reason='응답이 JSON 계약·질문·보안 검사를 통과하지 못했습니다';await panel.read(CHAT);assert($('chat-failure').textContent.includes('이유: 응답이 JSON 계약'));assert($('chat-failure').textContent.includes('다시 보내'));
		edit('chat-text','가'.repeat(2001));assert($('chat-send').disabled);edit('chat-text','  ');assert($('chat-send').disabled);edit('chat-text','합성 글');chatDetail.turns=Array.from({length:50},()=>({role:'user',text:'합성 글',source:'office'}));await panel.read(CHAT);assert($('chat-send').disabled);assert($('chat-send-reason').textContent.includes('50번'));assert.equal(requests.length,0);
		""")

	def test_read_poll_failure_timeout_dedup_and_closing_keeps_busy_poll(self):
		self.run_js(r"""
		chatDetail.status='busy';const panel=await bootChat();await panel.open(CHAT);getChat=async()=>response({message:'표시하면 안 되는 서버 오류'},503);await advance(3000);assert($('chat-dock-notice').textContent.includes('읽지 못했습니다'));assert(!$('chat-dock-notice').textContent.includes('서버 오류'));
		getChat=(id,signal)=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('합성 제한 시간'))));const pending=panel.read(CHAT),before=chatGets.length;const duplicate=panel.read(CHAT);assert.equal(chatGets.length,before);assert([...timers.values()].some(t=>!t.repeat&&t.ms===5000));const timeout=[...timers.values()].find(t=>!t.repeat&&t.ms===5000);timeout.fn();await pending;await duplicate;assert($('chat-dock-notice').textContent.includes('읽지 못했습니다'));
		getChat=async id=>response({...chatDetail,id});panel.close();const closed=chatGets.length;await advance(3000);assert(chatGets.length>closed);assert($('chat-dock').hidden);chatDetail.status='idle';await advance(3000);assert(![...timers.values()].some(t=>t.repeat&&t.ms===3000));assert.equal(await panel.open('../bad'),false);assert.equal(requests.length,0);
		""")

	def test_post_failures_preserve_input_conflict_reload_no_automatic_retry(self):
		self.run_js(r"""
		const panel=await bootChat();await panel.open(CHAT);edit('chat-text','합성 유지할 글');reply=async()=>response({ok:false,message:'합성 거부 <script>그대로</script>'},400);await click($('chat-send'));assert.equal($('chat-text').value,'합성 유지할 글');assert.equal($('chat-dock-notice').textContent,'합성 거부 <script>그대로</script>');assert.equal($('chat-dock-notice').children.length,0);assert(!$('chat-send').disabled);
		reply=async()=>{chatDetail.status='closed';return response({ok:false,message:'다른 곳에서 닫았습니다.'},409)};const gets=chatGets.length;await click($('chat-send'));assert(chatGets.length>gets);assert.equal(panel.detail().status,'closed');assert.equal($('chat-text').value,'합성 유지할 글');assert.equal($('chat-dock-notice').textContent,'다른 곳에서 닫았습니다.');
		chatDetail.status='idle';await panel.read(CHAT);reply=async()=>{throw new Error('합성 연결 거부')};await click($('chat-send'));assert.equal($('chat-text').value,'합성 유지할 글');assert($('chat-dock-notice').textContent.includes('다시 보내기 전에'));assert.equal(requests.length,3);await advance(6000);assert.equal(requests.length,3);
		""")

	def test_enter_newline_and_ctrl_meta_enter_send_and_safe_viewport(self):
		self.run_js(r"""
		const panel=await bootChat();await panel.open(CHAT);edit('chat-text','합성 줄바꿈');const enter=new Event('keydown',{key:'Enter',target:$('chat-text')});$('chat-text').dispatchEvent(enter);assert(!enter.defaultPrevented);assert.equal(requests.length,0);
		reply=async()=>response({ok:false,message:'합성 거부'},400);for(const key of ['ctrlKey','metaKey']){const event=new Event('keydown',{key:'Enter',[key]:true,target:$('chat-text')});$('chat-text').dispatchEvent(event);await flush();assert(event.defaultPrevented)}assert.equal(requests.length,2);
		window.visualViewport.height=400;window.visualViewport.dispatchEvent(new Event('resize'));assert.equal($('chat-dock').style.bottom,'calc(340px + env(safe-area-inset-bottom))');assert.equal($('chat-dock').style.maxHeight,'384px');window.visualViewport.height=height;window.visualViewport.dispatchEvent(new Event('resize'));assert.equal($('chat-dock').style.bottom,'');
		""")

	def test_dev_handoff_arguments_uses_existing_editor_no_execution_and_dialog_hide(self):
		self.run_js(r"""
		chatDetail.dev_draft='합성 개발 지시\n사용자가 확인할 내용';const panel=await bootChat();await panel.open(CHAT);assert(!$('chat-to-dev').hidden);assert(!$('chat-to-dev').disabled);const original=window.officeBridge.openDevWith,calls=[];window.officeBridge.openDevWith=(...args)=>{calls.push(args);return original(...args)};
		click($('chat-to-dev'));assert.deepEqual(calls,[[chatDetail.dev_draft,'xyz']]);assert($('dev-dialog').open);assert($('chat-dock').hidden);assert($('chat-to-dev').disabled);assert.equal($('dev-text').value,chatDetail.dev_draft);assert.equal($('dev-target').value,'xyz');assert.equal(requests.length,0);click($('dev-close'));assert(!$('chat-dock').hidden);assert(!$('chat-to-dev').disabled);
		$('blog-dialog').showModal();assert($('chat-dock').hidden);assert.equal(await panel.open(CHAT),false);assert.equal(window.officeBridge.openDevWith('합성 글','xyz'),false);$('blog-dialog').close();assert(!$('chat-dock').hidden);
		""")

	def test_read_only_without_token_and_mutually_exclusive_drawers_both_directions(self):
		self.run_js(r"""
		token=null;chatDetail.dev_draft='합성 초안';const panel=await bootChat();await panel.open(CHAT);assert(!$('chat-dock').hidden);assert($('chat-to-dev').disabled);assert($('chat-send').disabled);
		await window.officeMeetingPanel.open(MID);assert(!$('meeting-dock').hidden);assert($('chat-dock').hidden);await panel.open(CHAT);assert(!$('chat-dock').hidden);assert($('meeting-dock').hidden);panel.close();assert($('chat-dock').hidden&&$('meeting-dock').hidden);assert.equal(requests.length,0);
		""")

	def test_pm_bubble_busy_closed_idle_open_restore_state_and_reduced_motion(self):
		self.run_js(r"""
		sandbox.matchMedia=q=>({matches:true});const panel=await bootChat();const actor=window.officeBridge.actors().find(a=>a.name==='PM'),position={x:actor.x,y:actor.y};assert.equal(actor.task,'합성 기본 작업');await panel.open(CHAT);assert.equal(actor.task,'대화 중 · xyz');assert.equal(actor.status,'작업 중');await refresh();await flush();assert.equal(actor.task,'대화 중 · xyz');frame(100);assert.deepEqual({x:actor.x,y:actor.y},position);assert(!actor.walking);
		panel.close();assert.equal(actor.task,'합성 기본 작업');assert.equal(actor.status,'대기');chatDetail.status='busy';await panel.read(CHAT);assert.equal(actor.task,'대화 중 · 답을 쓰는 중');await refresh();assert.equal(actor.task,'대화 중 · 답을 쓰는 중');frame(200);assert.equal(actor.task,'대화 중 · 답을 쓰는 중');
		state.people[0].task='다음 합성 기본 작업';chatDetail.status='idle';await panel.read(CHAT);await flush();assert.equal(actor.task,'다음 합성 기본 작업');await panel.open(CHAT);assert.equal(actor.task,'대화 중 · xyz');chatDetail.status='closed';await panel.read(CHAT);assert.equal(actor.task,'다음 합성 기본 작업');
		""")

	def test_initial_background_busy_and_stale_state_do_not_restart_poll(self):
		self.run_js(r"""
		chatDetail.status='busy';state.chats.items[0].status='busy';const panel=await bootChat();assert($('chat-dock').hidden);assert.equal(chatGets.length,1);assert.equal(window.officeBridge.actors()[0].task,'대화 중 · 답을 쓰는 중');await advance(3000);assert.equal(chatGets.length,2);chatDetail.status='idle';await advance(3000);const stopped=chatGets.length;await refresh();await advance(3000);assert.equal(chatGets.length,stopped);assert.equal(window.officeBridge.actors()[0].task,'합성 기본 작업');
		""")

	def test_old_get_cannot_overwrite_accepted_send_or_closed_state(self):
		self.run_js(r"""
		const panel=await bootChat();await panel.open(CHAT);let resolve;getChat=()=>new Promise(r=>resolve=r);const oldRead=panel.read(CHAT);edit('chat-text','합성 질문');reply=async()=>response({ok:true});await click($('chat-send'));assert.equal($('chat-dock-state').textContent,'답을 쓰는 중');resolve(response(chatDetail));await oldRead;assert.equal(panel.detail().status,'busy');assert($('chat-send').disabled);
		getChat=async id=>response({...chatDetail,id});await panel.read(CHAT);getChat=()=>new Promise(r=>resolve=r);const beforeClose=panel.read(CHAT);click($('chat-end'));await click($('chat-submit'));resolve(response(chatDetail));await beforeClose;assert.equal(panel.detail().status,'closed');assert($('chat-composer').hidden);
		""")


class OfficeChatContrastTest(unittest.TestCase):
	def test_bubbles_use_ink_on_paper_and_soft_with_aa_contrast(self):
		def luminance(color):
			parts = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
			return sum(weight * (part / 12.92 if part <= .04045 else ((part + .055) / 1.055) ** 2.4)
				for weight, part in zip((.2126, .7152, .0722), parts))

		css = (STATIC / 'office.css').read_text()
		root = dict(re.findall(r'--([\w-]+):(#[\da-f]{6})', css.split('*{', 1)[0]))
		scene = json.loads((STATIC / 'scene.json').read_text())['palette']
		for theme in (root, {**scene, 'soft': '#edf1e6'}, {'ink': '#f1f5ea', 'paper': '#1b292b', 'soft': '#263b3c'}):
			for background in ('paper', 'soft'):
				values = sorted((luminance(theme['ink']), luminance(theme[background])))
				self.assertGreaterEqual((values[1] + .05) / (values[0] + .05), 4.5)
		self.assertIn('background:var(--paper);color:var(--ink)', css.split('.chat-message{', 1)[1])
		self.assertIn('.chat-user{align-self:flex-end;background:var(--soft)}', css)
