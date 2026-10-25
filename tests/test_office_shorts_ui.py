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
let entries=[{p01:true}],position=0,historyLimit=Infinity,dropPopstate=false;
const history={get state(){return entries[position]},get length(){return entries.length},
	pushState(state){entries=entries.slice(0,position+1);entries.push(state);position++;if(entries.length>historyLimit){entries.shift();position--}},
	replaceState(state){entries[position]=state},
	go(amount){const target=position+amount;if(dropPopstate||target<0||target>=entries.length)return;Promise.resolve().then(()=>{position=target;window.dispatchEvent(new Event('popstate',{state:history.state}))})},
	back(){this.go(-1)},forward(){this.go(1)}};
sandbox.history=history;window.history=history;
let clock=0,timerId=0;
const timers=new Map();
sandbox.setTimeout=(fn,delay)=>{timers.set(++timerId,{fn,at:clock+delay});return timerId};
sandbox.clearTimeout=id=>timers.delete(id);
function tick(ms){clock+=ms;for(const [key,timer] of [...timers])if(timer.at<=clock){timers.delete(key);timer.fn()}}
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
async function reconfirm(key){click(button('[data-shorts-edit="'+key+'"]'));assert.equal($('shorts-next').textContent,'확정');await nextCard();assert.equal($('shorts-title').textContent,'최종 요약')}
async function fill(story='합성 이야기 <script>장면</script>. 다음 문장.'){
	write('shorts-story',story);await nextCard();click(button('[data-shorts-preset="pictures"]'));await nextCard();
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
		stepFailure=false;await openShorts();assert.equal(stepRequests,2);assert.equal($('shorts-count').textContent,'1 / 17');assert.equal(document.activeElement,$('shorts-title'));assert(!$('shorts-next').disabled);assert.equal($('shorts-next').getAttribute('aria-disabled'),'true');assert($('shorts-previous').disabled);assert.equal($('shorts-progress').getAttribute('role'),'progressbar');
		click($('shorts-close'));await flush();assert(!$('shorts-dialog').open);assert(!entries.some(e=>e?.shorts));
		""")

	def test_story_limits_progress_direct_path_disclosure_and_preserved_back_answers(self):
		self.run_js(r"""
		await bootShorts();await openShorts();write('shorts-story','가'.repeat(2001));assert.equal($('shorts-next').getAttribute('aria-disabled'),'true');write('shorts-story','합성 첫 문장. 다음 문장.');assert.equal($('shorts-next').getAttribute('aria-disabled'),'false');await nextCard();
		assert.equal($('shorts-count').textContent,'2 / 17');assert.equal($('shorts-progress').getAttribute('aria-valuenow'),'2');assert($('shorts-body').textContent.includes('휴대폰 파일'));assert.equal(document.querySelector('.shorts-direct').getAttribute('open'),null);assert($('shorts-next').disabled);
		write('shorts-path','~/Pictures');assert(!$('shorts-next').disabled);await nextCard();assert.equal($('shorts-count').textContent,'3 / 17');click($('shorts-previous'));await flush();assert.equal($('shorts-path').value,'~/Pictures');assert.notEqual(document.querySelector('.shorts-direct').getAttribute('open'),null);
		history.back();await flush();assert.equal($('shorts-story').value,'합성 첫 문장. 다음 문장.');assert.equal($('shorts-count').textContent,'1 / 17');await nextCard();assert.equal($('shorts-path').value,'~/Pictures');
		""")

	def test_story_blank_format_characters_and_accessible_reason_focus(self):
		self.run_js(r"""
		await bootShorts();await openShorts();write('shorts-story','\u200b\u200c\u200d\u2060\ufeff');await nextCard();assert.equal($('shorts-title').textContent,'이야기');
		const field=$('shorts-story'),rule=$('shorts-story-rule'),description=field.getAttribute('aria-describedby').split(' ');
		assert.equal(field.nextElementSibling,rule);assert.equal(rule.textContent,'한 글자 이상 적으면 다음으로 갈 수 있어요');assert(description.includes(rule.id));
		assert($('shorts-body').textContent.includes('최근 겪은 일, 생활 꿀팁, 소개하고 싶은 장소나 물건 등 무엇이든 좋아요. 완성된 대본이 아니어도 괜찮아요.'));
		assert.equal($('shorts-story-warning').textContent,'회사·서비스 이름, 비밀값, 이메일·홈 경로는 적지 마세요. 질문은 고정 목록으로 진행됩니다.');assert(description.includes('shorts-story-warning'));
		for(const value of ['', ' \t ', '\n\r\n', '\u200b\u200c\u200d\u2060\ufeff', ' \u00ad\u061c\u202e\u2063\n']){
			write('shorts-story',value);assert(!$('shorts-next').disabled);assert.equal($('shorts-next').getAttribute('aria-disabled'),'true');$('shorts-next').focus();await nextCard();
			assert.equal($('shorts-title').textContent,'이야기');assert($('shorts-status').textContent.includes('한 글자 이상'));assert.equal(document.activeElement,field);assert.equal(field.value,value);
		}
		assert.equal($('shorts-status').getAttribute('role'),'status');write('shorts-story','가');assert.equal($('shorts-next').getAttribute('aria-disabled'),'false');await nextCard();assert.equal($('shorts-title').textContent,'소스 폴더');assert.equal(requests.length,0);
		""")

	def test_story_soft_limit_counter_large_paste_and_original_retained(self):
		self.run_js(r"""
		await bootShorts();await openShorts();assert.equal($('shorts-story').getAttribute('maxlength'),null);assert.equal($('shorts-remaining').textContent,'0 / 2,000자');assert.equal($('shorts-story-rule').textContent,'한 글자 이상 적으면 다음으로 갈 수 있어요');
		for(const size of [1,1999,2000,2001,10000]){
			const value='가'.repeat(size);write('shorts-story',value);assert.equal($('shorts-story').value,value);assert(!$('shorts-next').disabled);assert(!$('shorts-body').textContent.includes('자 남음'));
			if(size<=2000){assert.equal($('shorts-next').getAttribute('aria-disabled'),'false');assert.equal($('shorts-remaining').textContent,size.toLocaleString('ko-KR')+' / 2,000자');await nextCard();assert.equal($('shorts-title').textContent,'소스 폴더');history.back();await flush();assert.equal($('shorts-story').value,value)}
			else{const extra=(size-2000).toLocaleString('ko-KR');assert.equal($('shorts-next').getAttribute('aria-disabled'),'true');assert.equal($('shorts-remaining').textContent,'2,000자 초과 (+'+extra+'자)');assert.equal($('shorts-story-rule').textContent,'최대 2,000자까지 입력할 수 있어요. '+extra+'자를 줄여 주세요');$('shorts-next').focus();await nextCard();assert.equal($('shorts-title').textContent,'이야기');assert.equal(document.activeElement,$('shorts-story'));assert.equal($('shorts-status').textContent,$('shorts-story-rule').textContent);assert.equal($('shorts-story').value,value)}
		}
		write('shorts-story','가'.repeat(2000));await nextCard();history.back();await flush();assert.equal($('shorts-story').value,'가'.repeat(2000));
		await fill();for(const size of [2001,10000]){click(button('[data-shorts-edit="story"]'));write('shorts-story','가'.repeat(size));await nextCard();assert.equal($('shorts-title').textContent,'이야기');history.back();await flush();assert.equal($('shorts-title').textContent,'최종 요약');assert($('shorts-next').disabled);click(button('[data-shorts-edit="story"]'));assert.equal($('shorts-story').value,'가'.repeat(size));history.back();await flush()}
		click(button('[data-shorts-edit="story"]'));write('shorts-story',' '.repeat(2001));assert.equal($('shorts-story-rule').textContent,'최대 2,000자까지 입력할 수 있어요. 1자를 줄여 주세요');
		""")

	def test_story_graphemes_crlf_and_unmodified_payload(self):
		self.run_js(r"""
		await bootShorts();await openShorts();const family='\ud83d\udc68\u200d\ud83d\udc69\u200d\ud83d\udc67\u200d\ud83d\udc66';
		for(const value of [family.repeat(1000)+'가'.repeat(1000),'e\u0301'.repeat(2000),'가\r\n'.repeat(1000),'가'.repeat(1998)+' \n']){
			write('shorts-story',value);assert.equal($('shorts-remaining').textContent,'2,000 / 2,000자');assert.equal($('shorts-next').getAttribute('aria-disabled'),'false');await nextCard();assert.equal($('shorts-title').textContent,'소스 폴더');history.back();await flush();assert.equal($('shorts-story').value,value);
		}
		const original=' \u1100\u1161\r\n합성 이야기\t ';await fill(original);await nextCard();assert.equal(requests.at(-1).story,original);
		""")

	def test_story_code_point_limit_reason_focus_and_unmodified_payload(self):
		self.run_js(r"""
		await bootShorts();await openShorts();const family='\ud83d\udc68\u200d\ud83d\udc69\u200d\ud83d\udc67\u200d\ud83d\udc66',combined='e\u0301\u0302\u0303',boundary=family.repeat(1142)+'가'.repeat(6),reason='이모지·결합 문자가 많아 입력 한도를 넘어요. 줄여 주세요';
		assert.equal(Array.from(boundary).length,8000);
		for(const [value,count] of [[family.repeat(1142),1142],[boundary,1148],[combined.repeat(2000),2000]]){
			write('shorts-story',value);assert.equal($('shorts-remaining').textContent,count.toLocaleString('ko-KR')+' / 2,000자');assert.equal($('shorts-next').getAttribute('aria-disabled'),'false');await nextCard();assert.equal($('shorts-title').textContent,'소스 폴더');history.back();await flush();assert.equal($('shorts-story').value,value);
		}
		for(const [value,count] of [[family.repeat(1143),1143],[family.repeat(1500),1500],[family.repeat(2000),2000],[combined.repeat(2000)+'\u0304',2000],[(combined+'\u0304').repeat(2000),2000]]){
			assert(Array.from(value).length>8000);write('shorts-story',value);assert.equal($('shorts-story').getAttribute('maxlength'),null);assert.equal($('shorts-story').value,value);assert(!$('shorts-next').disabled);assert.equal($('shorts-next').getAttribute('aria-disabled'),'true');assert.equal($('shorts-remaining').textContent,count.toLocaleString('ko-KR')+' / 2,000자');assert.equal($('shorts-story-rule').textContent,reason);
			$('shorts-next').focus();await nextCard();assert.equal($('shorts-title').textContent,'이야기');assert.equal($('shorts-status').textContent,reason);assert.equal(document.activeElement,$('shorts-story'));assert.equal($('shorts-story').value,value);assert.equal(requests.length,0);
		}
		await fill(boundary);await nextCard();assert.equal(requests.at(-1).story,boundary);
		""")

	def test_story_segmenter_fallback_counts_code_points(self):
		self.run_js(r"""
		vm.runInContext('Intl.Segmenter=undefined',sandbox);await bootShorts();await openShorts();
		const value='\ud83d\udc68\u200d\ud83d\udc69\u200d\ud83d\udc67\u200d\ud83d\udc66';write('shorts-story',value);assert.equal($('shorts-remaining').textContent,'7 / 2,000자');
		write('shorts-story','가\r\n'.repeat(1000));assert.equal($('shorts-remaining').textContent,'2,000 / 2,000자');assert.equal($('shorts-next').getAttribute('aria-disabled'),'false');
		write('shorts-story','e\u0301'.repeat(1001));assert.equal($('shorts-remaining').textContent,'2,000자 초과 (+2자)');assert.equal($('shorts-next').getAttribute('aria-disabled'),'true');
		""")

	def test_story_composition_input_updates_without_replacing_field_and_recounts_end(self):
		self.run_js(r"""
		await bootShorts();await openShorts();const field=$('shorts-story'),body=$('shorts-body'),prefix='가'.repeat(1999);field.focus();body.dispatchEvent(new Event('compositionstart',{target:field}));
		for(const value of [prefix+'\u1100',prefix+'\u1100\u1161']){field.value=value;body.oninput({target:field,isComposing:true});assert.equal($('shorts-remaining').textContent,'2,000 / 2,000자');assert.equal($('shorts-next').getAttribute('aria-disabled'),'false');assert.equal($('shorts-story'),field);assert.equal(document.activeElement,field)}
		field.value=prefix+'가나';body.oninput({target:field,isComposing:true});assert.equal($('shorts-remaining').textContent,'2,000자 초과 (+1자)');assert.equal(field.value,prefix+'가나');
		field.value=prefix+'각';body.oncompositionend({target:field});assert.equal($('shorts-remaining').textContent,'2,000 / 2,000자');assert.equal($('shorts-next').getAttribute('aria-disabled'),'false');await nextCard();history.back();await flush();assert.equal($('shorts-story').value,prefix+'각');
		""")

	def test_chips_one_difference_proposal_requires_choice_and_summary_escape(self):
		self.run_js(r"""
		intake.steps[0].chips[0].difference='합성 <script>차이</script>';await bootShorts();await openShorts();write('shorts-story','합성 첫 문장입니다. 두 번째입니다.');await nextCard();click(button('[data-shorts-preset="pictures"]'));await nextCard();
		click(button('[data-shorts-chip]'));assert.equal(document.activeElement,button('[data-shorts-chip]'));assert.equal($('shorts-difference').textContent,'합성 <script>차이</script>');assert(!$('shorts-difference').querySelector('script'));assert.equal($('shorts-body').querySelectorAll('[aria-expanded="true"]').length,1);assert.equal($('shorts-body').querySelectorAll('[aria-pressed="true"]').length,1);
		click(document.querySelectorAll('[data-shorts-chip]')[1]);assert.equal($('shorts-difference').textContent,intake.steps[0].chips[1].difference);assert.equal($('shorts-body').querySelectorAll('[aria-expanded="true"]').length,1);await nextCard();click(button('[data-shorts-chip]'));await nextCard();
		assert.equal($('shorts-title').textContent,'핵심 메시지');assert.equal(document.querySelectorAll('[data-shorts-chip]').length,0);assert($('shorts-body').textContent.includes('제안: 합성 첫 문장입니다.'));assert($('shorts-next').disabled);click(button('[data-shorts-proposal]'));assert.equal(document.activeElement,button('[data-shorts-proposal]'));assert.equal($('shorts-value').value,'합성 첫 문장입니다.');assert.equal($('shorts-remaining').textContent,(300-Array.from('합성 첫 문장입니다.').length)+'자 남음');assert(!$('shorts-next').disabled);
		await nextCard();for(const key of ['facts','hook','cuts','length']){assert.equal($('shorts-title').textContent,intake.steps.find(s=>s.id===key).label);write('shorts-value','합성 선택');await nextCard()}assert.equal($('shorts-title').textContent,'화면 비율');assert($('shorts-body').textContent.includes('제안: 9:16'));assert($('shorts-next').disabled);click(button('[data-shorts-proposal]'));assert.equal($('shorts-value').value,'9:16');assert(!$('shorts-next').disabled);
		""")

	def test_dependency_reconfirmation_preserves_values_excludes_summary_and_edit_returns(self):
		self.run_js(r"""
		await bootShorts();await openShorts();await fill();assert(!$('shorts-body').querySelector('script'));assert($('shorts-body').textContent.includes('합성 이야기 <script>장면</script>'));
		click(button('[data-shorts-edit="length"]'));write('shorts-value','합성 길이 변경');await nextCard();assert.equal($('shorts-title').textContent,'최종 요약');assert($('shorts-next').disabled);assert(!$('shorts-body').textContent.includes('합성 분위기와 스타일'));assert(!$('shorts-body').textContent.includes('합성 소리'));assert($('shorts-body').textContent.includes('합성 소스와 필수 컷'));
		click(button('[data-shorts-edit="style"]'));assert.equal($('shorts-value').value,'합성 분위기와 스타일');assert($('shorts-body').textContent.includes('다시 확인 필요'));assert.equal($('shorts-next').textContent,'확정');await nextCard();assert($('shorts-next').disabled);await reconfirm('sound');assert(!$('shorts-next').disabled);
		click(button('[data-shorts-edit="message"]'));write('shorts-value','합성 핵심 변경');await nextCard();assert(!$('shorts-body').textContent.includes('합성 훅과 흐름'));assert($('shorts-body').textContent.includes('합성 소스와 필수 컷'));await reconfirm('hook');assert(!$('shorts-next').disabled);
		click(button('[data-shorts-edit="platform"]'));write('shorts-value','합성 플랫폼 변경');await nextCard();assert(!$('shorts-body').textContent.includes('합성 화면 비율'));assert(!$('shorts-body').textContent.includes('합성 자막'));assert($('shorts-body').textContent.includes('합성 소리'));await reconfirm('ratio');await reconfirm('caption');assert(!$('shorts-next').disabled);
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

	def test_cleanup_without_return_popstate_reopens_after_500ms(self):
		self.run_js(r"""
		await bootShorts();await openShorts();await fill();dropPopstate=true;reply=async()=>response({ok:true,message:'합성 접수 확정'});await nextCard();assert(!$('shorts-dialog').open);assert.equal(requests.length,1);
		await window.officeShorts.open();assert(!$('shorts-dialog').open);tick(499);await window.officeShorts.open();assert(!$('shorts-dialog').open);tick(1);await openShorts();assert.equal($('shorts-title').textContent,'이야기');assert.equal(requests.length,1);
		dropPopstate=false;click($('shorts-close'));await flush();assert.equal(history.state.shorts,undefined);tick(500);await openShorts();assert.equal($('shorts-title').textContent,'이야기');
		""")

	def test_cleanup_without_trim_popstate_reopens_and_does_not_affect_new_dialog(self):
		self.run_js(r"""
		await bootShorts();await openShorts();const go=history.go.bind(history);let calls=0;history.go=amount=>{if(++calls!==2)go(amount)};
		click($('shorts-close'));await flush();assert(!$('shorts-dialog').open);tick(500);await openShorts();write('shorts-story','합성 새 이야기');tick(500);assert($('shorts-dialog').open);assert.equal($('shorts-story').value,'합성 새 이야기');
		""")

	def test_history_depth_is_bounded_and_summary_edit_survives_back_forward(self):
		self.run_js(r"""
		historyLimit=50;await bootShorts();await openShorts();await fill();
		click(button('[data-shorts-edit="story"]'));assert.equal($('shorts-next').textContent,'확정');history.back();await flush();assert.equal($('shorts-title').textContent,'최종 요약');history.forward();await flush();assert.equal($('shorts-title').textContent,'이야기');await nextCard();assert.equal($('shorts-title').textContent,'최종 요약');
		for(const key of ['source','save']){click(button('[data-shorts-edit="'+key+'"]'));await nextCard();assert.equal($('shorts-title').textContent,'최종 요약')}
		for(let i=0;i<60;i++){click(button('[data-shorts-edit="facts"]'));write('shorts-value','합성 사실 '+i);await nextCard();assert.equal($('shorts-title').textContent,'최종 요약');assert(history.state.shorts.depth<=30);assert(history.length<=31)}
		const older=history.state.shorts.depth-1;history.back();await flush();assert.equal(history.state.shorts.depth,older);assert($('shorts-dialog').open);history.forward();await flush();assert.equal($('shorts-title').textContent,'최종 요약');
		click($('shorts-close'));click($('shorts-discard'));await flush();assert(!entries.some(e=>e?.shorts));assert.equal(requests.length,0);await openShorts();assert.equal($('shorts-title').textContent,'이야기');
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
		self.assertIn('#shorts-next[aria-disabled=true]', css)
		self.assertEqual(set(re.findall(r"action:'([^']+)'", (STATIC / "shorts_wizard.js").read_text())), {"shorts_intake"})
		self.assertNotIn('제작 시작', html + (STATIC / "shorts.js").read_text() + (STATIC / "shorts_wizard.js").read_text())
