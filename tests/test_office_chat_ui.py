"""합성 상태·가짜 fetch로 PM 진입과 대화·질문 요청 계약을 검사한다."""

import re
import shutil
import subprocess
import unittest

from office.server import STATIC
from test_office_meeting_ui import HARNESS as MEETING_HARNESS


SETUP = r"""
const CHAT='c-'+'c'.repeat(32),CARD='q-'+'d'.repeat(32);
let chatDetail={id:CHAT,target:'xyz',plan:'p01',tool:'codex',status:'idle',needs_user:'',calls:0,seconds:0,turns:[],card:null,dev_draft:'',failure:'',updated:'2026-10-25T12:00:00+09:00'};
state.chats={status:'정상',items:[{...chatDetail,preview:'합성 마지막 답'}]};state.chat_plans=[{id:'p01',tool:'codex'},{id:'p02',tool:'claude'}];
state.people[0].task='합성 기본 작업';
let clock=0,serial=0,frame;const timers=new Map(),chatGets=[],observers=[];
sandbox.performance.now=()=>clock;sandbox.requestAnimationFrame=fn=>frame=fn;
sandbox.setInterval=(fn,ms)=>{const id=++serial;timers.set(id,{fn,ms,due:clock+ms,repeat:true});if(ms===10000)interval=fn;return id};
sandbox.clearInterval=id=>timers.delete(id);
sandbox.setTimeout=(fn,ms)=>{const id=++serial;timers.set(id,{fn,ms,due:clock+ms});return id};sandbox.clearTimeout=id=>timers.delete(id);
sandbox.MutationObserver=class{constructor(fn){observers.push(fn)}observe(){}};
const modal=Element.prototype.showModal,closeModal=Element.prototype.close;
Element.prototype.showModal=function(){modal.call(this);for(const observer of observers)observer()};
Element.prototype.close=function(){closeModal.call(this);for(const observer of observers)observer()};
Object.defineProperty(Element.prototype,'scrollHeight',{get(){return this.textContent.length+100}});
Object.defineProperty(Element.prototype,'clientHeight',{get(){return 100}});
window.visualViewport=new Events();window.visualViewport.height=height;window.visualViewport.offsetTop=0;
let getChat=async id=>response({...chatDetail,id});
const baseFetch=sandbox.fetch;
sandbox.fetch=async(url,options={})=>{
	if(url.startsWith('/api/chat?')){
		assert(/^\/api\/chat\?id=c-[a-f0-9]{32}$/.test(url));assert.equal(options.method,'GET');assert.equal(options.cache,'no-store');assert(options.signal);assert.equal(options.body,undefined);
		chatGets.push(url);return getChat(url.split('=')[1],options.signal);
	}
	if(url.startsWith('/api/meeting?'))return response({id:MID,target:'xyz',agenda:'합성 회의',status:'completed',participants:[],rounds:{'1':[],'2':[]},calls:0});
	return baseFetch(url,options);
};
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve()};
async function advance(ms){
	const end=clock+ms;
	while(true){const next=[...timers].filter(([id,t])=>t.due<=end).sort((a,b)=>a[1].due-b[1].due)[0];if(!next)break;
		const [id,t]=next;clock=t.due;if(t.repeat)t.due+=t.ms;else timers.delete(id);await t.fn();await flush();
	}
	clock=end;frame?.(clock);await flush();
}
async function bootChat(order='early'){
	for(const name of ['meeting_scene.js','meeting_panel.js',...(order==='early'?['chat.js','chat_dock.js']:[])])vm.runInContext(fs.readFileSync(root+'/'+name,'utf8'),sandbox);
	await boot();if(order==='late')for(const name of ['chat.js','chat_dock.js'])vm.runInContext(fs.readFileSync(root+'/'+name,'utf8'),sandbox);
	await flush();return window.officeChatDock;
}
function openPM(){click($('directory'));click(button('[data-room="'+scene.people[0].department+'"]'));click(button('[data-person="0"]'))}
function openNewChat(){openPM();button('[data-chat-open]').focus();click(button('[data-chat-open]'));assert($('chat-dialog').open)}
const edit=(id,value)=>{const el=$(id);el.value=value;return id==='chat-text'?el.oninput():$('chat-body').oninput({target:el})};
const chatChoice=(id,value='a')=>click(button('[data-chat-answer="choice"][data-question="'+id+'"][data-value="'+value+'"]'));
const chatDelegate=id=>click(button('[data-chat-answer="delegate"][data-question="'+id+'"]'));
function waiting(){chatDetail.card={id:CARD,turn:1,status:'waiting',questions:plain(questions),summary:''}}
async function openChatQuestions(){await window.officeChatDock.open(CHAT);click($('chat-question'));assert.equal($('chat-title').textContent,'대화 질문')}
function completeAnswers(){chatChoice('scope');edit('chat-text-1','합성 직접 답변');chatDelegate('format')}
"""
HARNESS = MEETING_HARNESS + SETUP


@unittest.skipUnless(shutil.which("node"), "JavaScript 실행기 없음")
class OfficeChatUITest(unittest.TestCase):
	def run_js(self, checks):
		result = subprocess.run([shutil.which("node"), "-e", HARNESS + checks + "\n})().catch(e=>{console.error(e);process.exitCode=1});",
			str(STATIC),], capture_output=True, text=True, timeout=15)
		self.assertEqual(result.returncode, 0, result.stderr)

	def test_pm_entry_recent_five_escape_reopen_and_closed_label(self):
		self.run_js(r"""
		state.chats.items=Array.from({length:6},(_,i)=>({...state.chats.items[0],id:'c-'+String(i).repeat(32),target:'합성 <script>대상</script>',status:i===0?'closed':'idle',preview:'마지막 <script>답</script>'}));
		const panel=await bootChat();openPM();const entries=$('panel-body').querySelectorAll('[data-chat-id]');assert.equal(entries.length,5);assert(!$('panel-body').querySelector('script'));assert(entries[0].textContent.includes('닫힘'));assert(entries[0].textContent.includes('p01 · codex'));assert(!$('panel-body').querySelector('[data-chat-open]').disabled);
		entries[0].focus();click(entries[0]);await flush();assert(!$('chat-dock').hidden);assert.equal(panel.detail().id,state.chats.items[0].id);assert.equal(chatGets.length,1);
		click($('chat-dock-close'));assert($('chat-dock').hidden);assert($('toast').textContent.includes('대화 기록은 남고 PM에서 다시 열 수 있습니다'));assert(document.activeElement===entries[0]);
		""")

	def test_new_chat_disabled_with_reason_for_session_state_or_plans(self):
		for preparation, message in (("token=null;", "세션"), ("state.chats.status='확인 불가';state.chats.reason='합성 조회 거부';", "합성 조회 거부"), ("state.chat_plans=[];", "대화 AI")):
			with self.subTest(preparation=preparation):
				self.run_js(preparation + "await bootChat();openPM();assert(button('[data-chat-open]').disabled);assert($('chat-unavailable').textContent.includes('" + message + "'));click(button('[data-chat-open]'));assert(!$('chat-dialog').open);assert.equal(requests.length,0);")

	def test_new_chat_choices_exact_payload_failure_text_and_focus(self):
		self.run_js(r"""
		await bootChat('late');openNewChat();assert.equal($('chat-title').textContent,'새 대화');assert.equal(document.activeElement,$('chat-plan'));assert.equal($('chat-plan').value,'p01');assert.equal($('chat-target').value,'ai-hub');assert($('chat-body').textContent.includes('p02 · claude'));assert($('chat-target-hint').textContent.includes('선택한 AI가 대상 저장소 글을 읽습니다.'));
		const plan=$('chat-plan');plan.value='p02';$('chat-target').value='xyz';await refresh();assert.strictEqual($('chat-plan'),plan);assert.equal(plan.value,'p02');
		let resolve;reply=()=>new Promise(r=>resolve=r);const sending=click($('chat-submit'));assert($('chat-submit').disabled&&$('chat-close').disabled);assert.deepEqual(requests.at(-1),{action:'chat_new',id:'xyz',plan:'p02'});
		resolve(response({ok:false,message:'합성 거부 <script>그대로</script>'},400));await sending;assert.equal($('chat-status').textContent,'합성 거부 <script>그대로</script>');assert.equal($('chat-status').children.length,0);
		window.dispatchEvent(new Event('keydown',{key:'Escape',target:$('chat-plan')}));assert(!$('chat-dialog').open);assert.equal(document.activeElement.id,'close-panel');
		openNewChat();reply=async()=>response({ok:true,draft:{id:CHAT,target:'xyz',plan:'p01',tool:'codex',status:'idle'}});await click($('chat-submit'));assert(!$('chat-dialog').open);assert(!$('chat-dock').hidden);assert.equal(document.activeElement,$('chat-toggle'));assert.equal(requests.length,2);
		""")

	def test_questions_exclusive_complete_answers_summary_and_confirm(self):
		self.run_js(r"""
		waiting();await bootChat();await openChatQuestions();assert.equal(document.activeElement,$('chat-title'));assert.equal($('chat-dialog').scrollTop,0);assert($('chat-submit').disabled);assert($('chat-body').textContent.includes('추천 이유: 합성 추천 이유'));assert(!document.querySelector('[data-chat-answer="delegate"][data-question="tone"]'));
		chatChoice('scope');assert($('chat-submit').disabled);edit('chat-text-1','가');assert($('chat-submit').disabled);edit('chat-text-1','합성 직접 답변');chatDelegate('format');assert(!$('chat-submit').disabled);
		chatChoice('scope','b');edit('chat-text-0','직접 답변');assert.equal(button('[data-chat-answer="choice"][data-question="scope"][data-value="b"]').getAttribute('aria-pressed'),'false');chatChoice('scope');
		const retained=$('chat-text-1');await refresh();assert.strictEqual($('chat-text-1'),retained);assert.equal(retained.value,'합성 직접 답변');
		reply=async()=>{chatDetail.card.status='answered';chatDetail.card.summary='합성 요약 <script>표시</script>';return response({ok:true,draft:{id:CHAT,card:CARD,summary:chatDetail.card.summary}})};
		await click($('chat-submit'));assert.deepEqual(requests.at(-1),{action:'chat_answer',id:CHAT,card:CARD,answers:[{question_id:'scope',kind:'choice',value:'a'},{question_id:'tone',kind:'text',value:'합성 직접 답변'},{question_id:'format',kind:'delegate',value:'알아서 정해 줘'}]});assert.equal($('chat-title').textContent,'답변 확인');assert.equal($('chat-body').textContent,chatDetail.card.summary);assert(!$('chat-body').querySelector('script'));assert.equal(requests.length,1);
		await refresh();assert.equal($('chat-submit').textContent,'확인');reply=async()=>{chatDetail.status='busy';chatDetail.card.status='confirmed';return response({ok:true})};await click($('chat-submit'));assert.deepEqual(requests.at(-1),{action:'chat_confirm',id:CHAT,card:CARD});assert(!$('chat-dialog').open);assert.equal($('chat-dock-state').textContent,'답을 쓰는 중');assert([...timers.values()].some(t=>t.repeat&&t.ms===3000));
		""")

	def test_answered_reentry_resume_conflict_reload_and_no_implicit_confirm(self):
		self.run_js(r"""
		waiting();const panel=await bootChat();await openChatQuestions();completeAnswers();
		reply=async()=>{chatDetail.card.status='answered';chatDetail.card.summary='다른 곳에서 답한 합성 요약';return response({ok:false,message:'이미 다른 곳에서 답했습니다.'},409)};
		const before=chatGets.length;await click($('chat-submit'));assert.equal($('chat-status').textContent,'이미 다른 곳에서 답했습니다.');assert($('chat-submit').disabled);assert(chatGets.length>before);assert.equal(panel.detail().card.status,'answered');
		click($('chat-close'));assert.equal(requests.length,1);await panel.open(CHAT);assert.equal($('chat-title').textContent,'답변 확인');assert.equal($('chat-body').textContent,chatDetail.card.summary);assert(!$('chat-submit').disabled);click($('chat-close'));assert.equal(requests.length,1);
		""")

	def test_mixed_todos_use_correct_module_and_combined_badge(self):
		self.run_js(r"""
		waiting();state.questions.items.push({kind:'chat',chat:CHAT,card:CARD,turn:1,questions:plain(questions)});state.questions.items[0].kind='meeting';state.approvals.questions.count=2;
		await bootChat();assert.equal(window.officeMeeting.approvals().length,1);assert.equal(window.officeChat.approvals().length,1);assert.equal($('approval-count').textContent,'2');
		click(button('[data-view="office"]'));assert($('panel-body').innerHTML.includes('<b>2</b>AI 질문'));click(button('[data-filter="question"]'));assert($('panel-body').textContent.includes('회의와 대화'));assert($('panel-body').textContent.includes('대화 · xyz · 질문 3개'));assert.equal(document.querySelectorAll('[data-question-card]').length,1);assert.equal(document.querySelectorAll('[data-chat-question]').length,1);
		click(button('[data-chat-question]'));await flush();assert.equal($('chat-title').textContent,'대화 질문');assert(!$('meeting-dialog').open);click($('chat-close'));click(button('[data-question-card]'));assert.equal($('meeting-title').textContent,'회의 질문');assert(!$('chat-dialog').open);assert.equal(requests.length,0);
		""")

	def test_end_confirmation_exact_close_and_network_failure_no_retries(self):
		self.run_js(r"""
		const panel=await bootChat();await panel.open(CHAT);click($('chat-end'));assert.equal($('chat-title').textContent,'대화 끝내기');assert($('chat-body').textContent.includes('더 보낼 수 없습니다'));click($('chat-close'));assert.equal(requests.length,0);
		click($('chat-end'));reply=async()=>{chatDetail.status='closed';return response({ok:true})};await click($('chat-submit'));assert.deepEqual(requests.at(-1),{action:'chat_close',id:CHAT});assert($('chat-composer').hidden);assert.equal($('chat-dock-state').textContent,'닫힘');assert.equal(panel.detail().status,'closed');
		click($('chat-new'));reply=async()=>{throw new Error('합성 연결 거부')};await click($('chat-submit'));assert($('chat-status').textContent.includes('결과를 받지 못했습니다'));assert($('chat-submit').disabled);assert(!$('chat-close').disabled);assert.equal(requests.length,2);
		""")

	def test_dialog_exclusion_and_cancel_focus(self):
		self.run_js(r"""
		await bootChat();openPM();for(const id of ['dev-dialog','action-dialog','blog-dialog','meeting-dialog']){$(id).showModal();window.officeChat.openNew();assert(!$('chat-dialog').open);$(id).close()}
		const entry=button('[data-chat-open]');entry.focus();click(entry);assert($('chat-dialog').open);$('chat-dialog').dispatchEvent(new Event('cancel'));assert(!$('chat-dialog').open);assert.strictEqual(document.activeElement,entry);assert.equal(requests.length,0);
		""")

	def test_confirm_conflict_updates_detail_and_keeps_notice_without_post_retry(self):
		self.run_js(r"""
		waiting();chatDetail.card.status='answered';chatDetail.card.summary='합성 확인 대기 요약';const panel=await bootChat();await panel.open(CHAT);assert.equal($('chat-title').textContent,'답변 확인');
		reply=async()=>{chatDetail.card.status='confirmed';chatDetail.status='busy';return response({ok:false,message:'다른 창에서 이미 확인했습니다.'},409)};await click($('chat-submit'));assert.deepEqual(requests.at(-1),{action:'chat_confirm',id:CHAT,card:CARD});assert.equal($('chat-status').textContent,'다른 창에서 이미 확인했습니다.');assert($('chat-submit').disabled);assert.equal(panel.detail().card.status,'confirmed');assert.equal(panel.detail().status,'busy');click($('chat-close'));assert.equal($('chat-dock-state').textContent,'답을 쓰는 중');await advance(3000);assert.equal(requests.length,1);
		""")


class OfficeChatStaticTest(unittest.TestCase):
	def test_static_scripts_tokens_accessibility_and_constraints(self):
		html = (STATIC / "index.html").read_text()
		css = (STATIC / "office.css").read_text().split('.chat-recent{', 1)[1]
		for name in ("chat.js", "chat_dock.js"):
			self.assertIn('<script src="/' + name + '" defer></script>', html)
			js = (STATIC / name).read_text()
			self.assertLessEqual(len(js.splitlines()), 500)
			self.assertIn("if(window.officeBridge)init();else document.addEventListener('office-ready',init,{once:true})", js)
			self.assertNotRegex(js, r"\b(?:alert|confirm|prompt)\s*\(|localStorage|sessionStorage|console\.|https?://")
			self.assertNotRegex(js + css, "[\U0001f000-\U0001faff\u2600-\u27bf]")
		self.assertNotRegex(html, r"\son\w+=")
		self.assertNotRegex(css, r"#[0-9a-fA-F]{3,8}\b|(?:rgb|hsl)a?\(|font-family")
		self.assertIn('id="chat-log" class="chat-log" role="log" aria-live="polite"', html)
		self.assertIn('id="chat-text" rows="2" maxlength="2000"', html)
		for expected in ('font-size:16px', 'safe-area-inset-bottom', 'overflow:auto', 'white-space:pre-wrap', 'min-width:0', 'right:344px', '70dvh'):
			self.assertIn(expected, css)
		self.assertTrue(all(not line.startswith(' ') for line in (STATIC / 'chat.js').read_text().splitlines()))
		self.assertEqual(set(re.findall(r"action:'([^']+)'", (STATIC / 'chat.js').read_text() + (STATIC / 'chat_dock.js').read_text())), {'chat_new', 'chat_send', 'chat_answer', 'chat_confirm', 'chat_close'})
