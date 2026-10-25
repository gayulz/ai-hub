"""Node DOM 대역으로 접수 카드·기록 진입·뒤로 가기와 요청 계약을 검사한다."""

import json
import re
import shutil
import subprocess
import unittest

from office.server import STATIC
from office.shorts_steps import STEPS
from test_office_meeting_ui import HARNESS as BASE

SETUP = r"""
let uuid=0,scrolls=0;
sandbox.crypto={randomUUID:()=>String(++uuid).padStart(32,'0').replace(/(.{8})(.{4})(.{4})(.{4})(.{12})/,'$1-$2-$3-$4-$5')};
Element.prototype.scrollIntoView=function(){scrolls++};
window.visualViewport=new Events();window.visualViewport.height=height;window.visualViewport.offsetTop=0;
let entries=[{p01:true}],position=0;
const history={get state(){return entries[position]},get length(){return entries.length},
	pushState(state){entries=entries.slice(0,position+1);entries.push(state);position++},
	go(amount){const target=position+amount;if(target<0||target>=entries.length)return;Promise.resolve().then(()=>{position=target;window.dispatchEvent(new Event('popstate',{state:history.state}))})},
	back(){this.go(-1)},forward(){this.go(1)}};
sandbox.history=history;window.history=history;
state.shorts={status:'정상',items:Array.from({length:6},(_,i)=>({id:'s-'+String(i).repeat(32),created:'2026-10-25T12:00:00+09:00',status:'intake_confirmed',story_preview:'합성 <script>접수</script>'}))};
let stepFailure=false,stepRequests=0;
const fetchBase=sandbox.fetch;
sandbox.fetch=async(url,options={})=>{
	if(url==='/api/shorts/steps'){stepRequests++;assert.equal(options.method,'GET');assert.equal(options.cache,'no-store');assert(options.signal);assert.equal(options.body,undefined);return response(intake,stepFailure?503:200)}
	return fetchBase(url,options);
};
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve()};
async function bootShorts(order='early'){
	if(order==='early')for(const file of ['shorts_wizard.js','shorts.js'])vm.runInContext(fs.readFileSync(root+'/'+file,'utf8'),sandbox);
	await boot();
	if(order==='late')for(const file of ['shorts_wizard.js','shorts.js'])vm.runInContext(fs.readFileSync(root+'/'+file,'utf8'),sandbox);
	await flush();
}
function openPD(){click($('directory'));click(button('[data-room="shorts"]'));click(button('[data-person="'+scene.people.find(p=>p.department==='shorts'&&p.name==='PD').id+'"]'))}
async function openShorts(){openPD();button('[data-shorts-open]').focus();click(button('[data-shorts-open]'));await flush();assert($('shorts-dialog').open)}
const write=(name,value)=>{const el=$(name);el.value=value;$('shorts-body').oninput({target:el})};
const nextCard=async()=>{await click($('shorts-next'));await flush()};
async function reconfirm(key){click(button('[data-shorts-edit="'+key+'"]'));assert.equal($('shorts-next').textContent,'확정');await nextCard();history.go(-2);await flush();assert.equal($('shorts-title').textContent,'최종 요약')}
async function fill(){
	write('shorts-story','합성 이야기 <script>장면</script>. 다음 문장.');await nextCard();click(button('[data-shorts-preset="pictures"]'));await nextCard();
	for(const step of intake.steps){assert.equal($('shorts-title').textContent,step.label);write('shorts-value','합성 '+step.label);await nextCard()}
	assert.equal($('shorts-title').textContent,'저장 폴더');click(button('[data-shorts-preset="movies"]'));await nextCard();
	assert.equal($('shorts-title').textContent,'최종 요약');assert(!$('shorts-next').disabled);
}
"""
HARNESS = BASE + "\nconst intake=" + json.dumps({"steps": STEPS, "source_presets": [{"id": "pictures", "label": "사진"},
	{"id": "movies", "label": "영상"}], "save_presets": [{"id": "movies", "label": "영상"}]}, ensure_ascii=False) + ";\n" + SETUP


@unittest.skipUnless(shutil.which("node"), "JavaScript 실행기 없음")
class OfficeShortsUITest(unittest.TestCase):
	def run_js(self, checks):
		result = subprocess.run([shutil.which("node"), "-e", HARNESS + checks + "\n})().catch(e=>{console.error(e);process.exitCode=1});",
			str(STATIC)], capture_output=True, text=True, timeout=15)
		self.assertEqual(result.returncode, 0, result.stderr)

	def test_pd_entry_recent_five_escape_loading_late_init_and_failure(self):
		self.run_js(r"""
		await bootShorts('late');openPD();assert.equal(document.querySelectorAll('.shorts-recent').length,5);assert(!$('panel-body').querySelector('script'));assert($('panel-body').textContent.includes('s-000000'));assert(!button('[data-shorts-open]').disabled);
		stepFailure=true;click(button('[data-shorts-open]'));await flush();assert(!$('shorts-dialog').open);assert($('toast').textContent.includes('접수 질문을 읽지 못했습니다'));
		stepFailure=false;await openShorts();assert.equal(stepRequests,2);assert.equal($('shorts-count').textContent,'1 / 17');assert.equal(document.activeElement,$('shorts-title'));assert($('shorts-next').disabled);assert($('shorts-previous').disabled);assert.equal($('shorts-progress').getAttribute('role'),'progressbar');
		click($('shorts-close'));await flush();assert(!$('shorts-dialog').open);assert(!entries.some(e=>e?.shorts));
		""")

	def test_story_limits_progress_direct_path_disclosure_and_preserved_back_answers(self):
		self.run_js(r"""
		await bootShorts();await openShorts();write('shorts-story','가'.repeat(2001));assert($('shorts-next').disabled);write('shorts-story','합성 첫 문장. 다음 문장.');assert(!$('shorts-next').disabled);await nextCard();
		assert.equal($('shorts-count').textContent,'2 / 17');assert.equal($('shorts-progress').getAttribute('aria-valuenow'),'2');assert($('shorts-body').textContent.includes('휴대폰 파일'));assert.equal(document.querySelector('.shorts-direct').getAttribute('open'),null);assert($('shorts-next').disabled);
		write('shorts-path','~/Pictures');assert(!$('shorts-next').disabled);await nextCard();assert.equal($('shorts-count').textContent,'3 / 17');click($('shorts-previous'));await flush();assert.equal($('shorts-path').value,'~/Pictures');assert.notEqual(document.querySelector('.shorts-direct').getAttribute('open'),null);
		history.back();await flush();assert.equal($('shorts-story').value,'합성 첫 문장. 다음 문장.');assert.equal($('shorts-count').textContent,'1 / 17');await nextCard();assert.equal($('shorts-path').value,'~/Pictures');
		""")

	def test_chips_one_difference_proposal_requires_choice_and_summary_escape(self):
		self.run_js(r"""
		intake.steps[0].chips[0].difference='합성 <script>차이</script>';await bootShorts();await openShorts();write('shorts-story','합성 첫 문장입니다. 두 번째입니다.');await nextCard();click(button('[data-shorts-preset="pictures"]'));await nextCard();
		click(button('[data-shorts-chip]'));assert.equal($('shorts-difference').textContent,'합성 <script>차이</script>');assert(!$('shorts-difference').querySelector('script'));assert.equal($('shorts-body').querySelectorAll('[aria-expanded="true"]').length,1);assert.equal($('shorts-body').querySelectorAll('[aria-pressed="true"]').length,1);
		click(document.querySelectorAll('[data-shorts-chip]')[1]);assert.equal($('shorts-difference').textContent,intake.steps[0].chips[1].difference);assert.equal($('shorts-body').querySelectorAll('[aria-expanded="true"]').length,1);await nextCard();click(button('[data-shorts-chip]'));await nextCard();
		assert.equal($('shorts-title').textContent,'핵심 메시지');assert.equal(document.querySelectorAll('[data-shorts-chip]').length,0);assert($('shorts-body').textContent.includes('제안: 합성 첫 문장입니다.'));assert($('shorts-next').disabled);click(button('[data-shorts-proposal]'));assert.equal($('shorts-value').value,'합성 첫 문장입니다.');assert(!$('shorts-next').disabled);
		await nextCard();for(const key of ['facts','hook','cuts','length']){assert.equal($('shorts-title').textContent,intake.steps.find(s=>s.id===key).label);write('shorts-value','합성 선택');await nextCard()}assert.equal($('shorts-title').textContent,'화면 비율');assert($('shorts-body').textContent.includes('제안: 9:16'));assert($('shorts-next').disabled);click(button('[data-shorts-proposal]'));assert.equal($('shorts-value').value,'9:16');assert(!$('shorts-next').disabled);
		""")

	def test_dependency_reconfirmation_preserves_values_excludes_summary_and_edit_returns(self):
		self.run_js(r"""
		await bootShorts();await openShorts();await fill();assert(!$('shorts-body').querySelector('script'));assert($('shorts-body').textContent.includes('합성 이야기 <script>장면</script>'));
		click(button('[data-shorts-edit="length"]'));write('shorts-value','합성 길이 변경');await nextCard();history.go(-2);await flush();assert.equal($('shorts-title').textContent,'최종 요약');assert($('shorts-next').disabled);assert(!$('shorts-body').textContent.includes('합성 분위기와 스타일'));assert(!$('shorts-body').textContent.includes('합성 소리'));assert($('shorts-body').textContent.includes('합성 소스와 필수 컷'));
		click(button('[data-shorts-edit="style"]'));assert.equal($('shorts-value').value,'합성 분위기와 스타일');assert($('shorts-body').textContent.includes('다시 확인 필요'));assert.equal($('shorts-next').textContent,'확정');await nextCard();history.go(-2);await flush();assert($('shorts-next').disabled);await reconfirm('sound');assert(!$('shorts-next').disabled);
		click(button('[data-shorts-edit="message"]'));write('shorts-value','합성 핵심 변경');await nextCard();history.go(-2);await flush();assert(!$('shorts-body').textContent.includes('합성 훅과 흐름'));assert($('shorts-body').textContent.includes('합성 소스와 필수 컷'));await reconfirm('hook');assert(!$('shorts-next').disabled);
		click(button('[data-shorts-edit="platform"]'));write('shorts-value','합성 플랫폼 변경');await nextCard();history.go(-2);await flush();assert(!$('shorts-body').textContent.includes('합성 화면 비율'));assert(!$('shorts-body').textContent.includes('합성 자막'));assert($('shorts-body').textContent.includes('합성 소리'));await reconfirm('ratio');await reconfirm('caption');assert(!$('shorts-next').disabled);
		""")

	def test_exact_payload_id_retries_rejections_go_to_field_and_success_closes(self):
		self.run_js(r"""
		await bootShorts();await openShorts();await fill();reply=async()=>response({ok:false,message:'합성 폴더 거부 <script>표시</script>',field:'source'},400);await nextCard();
		const first=requests.at(-1);assert.deepEqual(Object.keys(first).sort(),['action','conditions','id','save','source','story']);assert(/^s-[a-f0-9]{32}$/.test(first.id));assert.equal(first.action,'shorts_intake');assert.equal(first.conditions.length,intake.steps.length);assert.deepEqual(first.conditions.map(c=>c.id),intake.steps.map(s=>s.id));assert.equal($('shorts-title').textContent,'소스 폴더');assert.equal($('shorts-status').textContent,'합성 폴더 거부 <script>표시</script>');assert.equal($('shorts-status').children.length,0);await nextCard();history.go(-2);await flush();
		reply=async()=>response({ok:false,message:'이미 접수한 요청입니다',field:'summary'},409);await nextCard();assert.equal($('shorts-title').textContent,'최종 요약');assert.equal($('shorts-status').textContent,'이미 접수한 요청입니다');assert.equal(requests.at(-1).id,first.id);assert.equal(requests.length,2);
		reply=async()=>{throw new Error('합성 연결 거부')};await nextCard();assert($('shorts-status').textContent.includes('결과를 받지 못했습니다'));assert.equal(requests.length,3);
		reply=async()=>response({ok:true,message:'접수를 확정했습니다. 제작은 따로 시작합니다.',draft:{id:first.id,status:'intake_confirmed',created:'2026-10-25T12:00:00+09:00'}});await nextCard();assert(!$('shorts-dialog').open);assert($('toast').textContent.includes('접수를 확정했습니다. 제작은 따로 시작합니다.'));assert(!entries.some(e=>e?.shorts));assert.equal(requests.at(-1).id,first.id);assert.equal(requests.length,4);
		""")

	def test_close_escape_gesture_confirmation_once_and_keyboard_viewport(self):
		self.run_js(r"""
		await bootShorts();await openShorts();write('shorts-story','합성 입력 중');$('shorts-story').focus();window.visualViewport.height=400;window.visualViewport.offsetTop=20;window.visualViewport.dispatchEvent(new Event('resize'));assert.equal($('shorts-dialog').style.values['--shorts-height'],'400px');assert.equal($('shorts-dialog').style.values['--shorts-top'],'32px');assert(scrolls>0);
		window.dispatchEvent(new Event('keydown',{key:'Escape',target:$('shorts-story')}));assert($('shorts-dialog').open);assert(!$('shorts-exit').hidden);assert.equal(document.activeElement,$('shorts-stay'));window.dispatchEvent(new Event('keydown',{key:'Escape'}));assert($('shorts-dialog').open);click($('shorts-stay'));assert($('shorts-exit').hidden);assert.equal($('shorts-story').value,'합성 입력 중');
		history.back();await flush();assert($('shorts-dialog').open);assert(!$('shorts-exit').hidden);click($('shorts-stay'));assert.equal($('shorts-story').value,'합성 입력 중');assert.equal(history.state.shorts.depth,1);click($('shorts-close'));click($('shorts-discard'));await flush();assert(!$('shorts-dialog').open);assert(!entries.some(e=>e?.shorts));assert.equal(requests.length,0);
		await openShorts();$('shorts-dialog').dispatchEvent(new Event('cancel'));await flush();assert(!$('shorts-dialog').open);assert(!entries.some(e=>e?.shorts));
		""")

	def test_busy_and_other_dialogs_prevent_duplicate_actions_or_new_modal(self):
		self.run_js(r"""
		await bootShorts();openPD();for(const id of ['meeting-dialog','chat-dialog','dev-dialog','action-dialog']){$(id).showModal();await window.officeShorts.open();assert(!$('shorts-dialog').open);$(id).close()}
		await openShorts();await fill();let resolve;reply=()=>new Promise(r=>resolve=r);const pending=click($('shorts-next'));assert($('shorts-next').disabled&&$('shorts-close').disabled&&$('shorts-previous').disabled);click($('shorts-next'));window.dispatchEvent(new Event('keydown',{key:'Escape'}));assert($('shorts-dialog').open);assert.equal(requests.length,1);resolve(response({ok:false,message:'합성 거부'},400));await pending;assert(!$('shorts-close').disabled);
		const key=new Event('keydown',{key:'ArrowDown',target:$('shorts-title')});window.dispatchEvent(key);assert(key.stopped);assert(!key.defaultPrevented);assert(!window.officeBridge.openDevWith('합성 지시','ai-hub'));
		""")


class OfficeShortsStaticTest(unittest.TestCase):
	def test_static_contract_size_accessibility_and_existing_tokens(self):
		html = (STATIC / "index.html").read_text()
		css = (STATIC / "office.css").read_text().split('.shorts-dialog{', 1)[1]
		for file in ("shorts.js", "shorts_wizard.js"):
			self.assertIn('<script src="/' + file + '" defer></script>', html)
			js = (STATIC / file).read_text()
			self.assertLessEqual(len(js.splitlines()), 500)
			self.assertIn("if(window.officeBridge)init();else document.addEventListener('office-ready',init,{once:true})", js)
			self.assertNotRegex(js, r"localStorage|sessionStorage|https?://|\b(?:alert|confirm|prompt)\s*\(")
			self.assertNotRegex(js, "[\U0001f000-\U0001faff\u2600-\u27bf]")
			self.assertTrue(all(not line.startswith(' ') for line in js.splitlines()))
		self.assertNotRegex(html, r"\son\w+=")
		self.assertNotRegex(css, r"#[0-9a-fA-F]{3,8}\b|(?:rgb|hsl)a?\(|font-family")
		for text in ("min-height:48px", "min-width:0", "font-size:16px", "safe-area-inset-bottom", "overflow:auto", "position:sticky", "white-space:pre-wrap"):
			self.assertIn(text, css)
		self.assertIn('id="shorts-title" tabindex="-1"', html)
		self.assertIn('id="shorts-status" class="shorts-notice" role="status" aria-live="polite"', html)
		self.assertEqual(set(re.findall(r"action:'([^']+)'", (STATIC / "shorts_wizard.js").read_text())), {"shorts_intake"})
		self.assertNotIn('제작 시작', html + (STATIC / "shorts.js").read_text() + (STATIC / "shorts_wizard.js").read_text())
