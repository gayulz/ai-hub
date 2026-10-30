"""합성 DOM으로 질문 입력과 두 단계 납품 확인을 검사한다."""

import shutil
import subprocess
import unittest

from office.server import STATIC
from test_office_meeting_ui import HARNESS


SETUP = r"""
const PID='p-20261028-001',HASH='a'.repeat(64);
const question={id:PID,title:'합성 <script>질문</script>',questions:[{no:1,question:'첫 질문?'},{no:2,question:'둘째 질문?'}]};
const bundle={id:PID,title:'합성 납품',hash:HASH,criteria:[{criterion:'합성 기준',verdict:'pass'}],tests:{ok:true,detail:'합성 시험'},changed:2,actions:['merge','push'],restart_notice:''};
const baseFetch=sandbox.fetch;
let detailReply=null;
sandbox.fetch=async(url,options={})=>{
	if(url.startsWith('/api/agency?')){assert.equal(options.headers['X-Office-Token'],token);return detailReply?detailReply(url):response(url.endsWith('questions')?question:bundle)}
	return baseFetch(url,options);
};
const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve()};
async function bootAgency(){
	state.projects={status:'정상',items:[{id:PID,title:'합성 프로젝트',department:'xyz',status:'납품 대기',question_waiting:true,completed:0,total:1,used:{calls:1,seconds:3},budget:{calls:3,minutes:5}}]};
	await boot();vm.runInContext(fs.readFileSync(root+'/agency.js','utf8'),sandbox);await flush();click(button('[data-view="projects"]'));
}
async function openAgency(view){click(button('[data-agency-view="'+view+'"]'));await flush();assert($('agency-dialog').open)}
"""


@unittest.skipUnless(shutil.which("node"), "JavaScript 실행기 없음")
class OfficeAgencyUITest(unittest.TestCase):
	def run_js(self, checks):
		result = subprocess.run([shutil.which("node"), "-e", HARNESS + SETUP + checks
			+ "\n})().catch(e=>{console.error(e);process.exitCode=1});", str(STATIC)], capture_output=True, text=True, timeout=15)
		self.assertEqual(result.returncode, 0, result.stderr)

	def test_answers_preserve_other_text_and_refresh(self):
		self.run_js(r"""
		await bootAgency();await openAgency('questions');assert($('agency-body').textContent.includes('첫 질문?'));
		assert.equal($('agency-body').querySelectorAll('script').length,0);
		$('agency-answer-1').value='합성 답';$('agency-answer-2').value='아직 입력 중';const editor=$('agency-answer-2');
		await interval();assert.strictEqual($('agency-answer-2'),editor);assert.equal(editor.value,'아직 입력 중');
		reply=async()=>{question.questions=question.questions.filter(q=>q.no!==1);return response({ok:true,message:'저장 완료'})};
		click(button('[data-agency-send="1"]'));await flush();
		assert.deepEqual(requests[0],{action:'agency_answer',id:PID,no:1,text:'합성 답'});
		assert.equal($('agency-answer-2').value,'아직 입력 중');assert(!$('agency-answer-1'));
		""")

	def test_approval_requires_two_confirmations_and_repeats_actions(self):
		self.run_js(r"""
		await bootAgency();click(button('[data-view="approvals"]'));await openAgency('delivery');
		assert($('agency-body').textContent.includes('합성 기준'));assert($('agency-body').textContent.includes('합성 시험'));
		assert($('agency-body').textContent.includes(HASH.slice(0,12)));assert.equal(requests.length,0);
		click($('agency-submit'));assert($('agency-title').textContent.includes('1 / 2'));assert($('agency-body').textContent.includes('push'));assert.equal(requests.length,0);
		click($('agency-submit'));assert($('agency-title').textContent.includes('2 / 2'));assert($('agency-body').textContent.includes('push'));assert.equal(requests.length,0);
		reply=async()=>response({ok:true,status:'완료',message:'완료',pushed:true,remote_matches:true});
		await click($('agency-submit'));await flush();assert.deepEqual(requests[0],{action:'agency_approve',id:PID,hash:HASH});
		assert($('agency-body').textContent.includes('원격 대조: 일치'));assert($('agency-submit').hidden);
		""")

	def test_periodic_states_partial_counts_and_unconfirmed_approval(self):
		self.run_js(r"""
		Object.assign(bundle,{kind:'document',documents:['t1'],actions:['document'],periodic_evidence:{verified:true,
			checks:[{kind:'tests',status:'unverified'},{kind:'dependencies',status:'failed'},{kind:'tests',status:'passed'}],
			synthetic:[{status:'passed',counts:{ran:3,failures:0,errors:0,skipped:1}}]}});
		await bootAgency();await openAgency('delivery');
		for(const text of ['시험 · 미검증','의존성 · 실패','시험 · 통과','합성 부분 시험 · 통과','건너뜀 1개','실제 저장소 전체 시험은 미검증'])assert($('agency-body').textContent.includes(text));
		click($('agency-submit'));assert($('agency-title').textContent.includes('1 / 2'));
		click($('agency-submit'));assert($('agency-title').textContent.includes('2 / 2'));assert.equal(requests.length,0);
		click($('agency-close'));
		for(const value of [null,{verified:false,checks:[],synthetic:[]},
			{verified:true,checks:[{kind:'tests',status:'unknown'}],synthetic:[]},
			{verified:true,checks:[{kind:'tests',status:'unverified'}],synthetic:[{status:'passed',counts:{ran:0,failures:0,errors:0,skipped:0}}]}]){
			bundle.periodic_evidence=value;await openAgency('delivery');
			assert($('agency-body').textContent.includes('근거 미확인'));assert($('agency-submit').hidden);
			click($('agency-submit'));assert(!$('agency-title').textContent.includes('1 / 2'));assert.equal(requests.length,0);
			click($('agency-close'));
		}
		delete bundle.periodic_evidence;await openAgency('delivery');
		assert(!$('agency-body').textContent.includes('점검 근거'));assert(!$('agency-submit').hidden);
		click($('agency-submit'));assert($('agency-title').textContent.includes('1 / 2'));
		""")

	def test_refresh_after_answer_holds_dialog_and_preserves_other_input_on_failure(self):
		self.run_js(r"""
		await bootAgency();await openAgency('questions');$('agency-answer-1').value='합성 답';$('agency-answer-2').value='보존할 입력';
		let resolve;detailReply=()=>new Promise(r=>resolve=r);reply=async()=>response({ok:true,message:'저장 완료'});
		click(button('[data-agency-send="1"]'));await flush();assert($('agency-close').disabled);click($('agency-close'));assert($('agency-dialog').open);
		resolve(response({message:'합성 조회 실패'},409));await flush();assert(!$('agency-close').disabled);
		assert.equal($('agency-answer-2').value,'보존할 입력');assert($('agency-status').textContent.includes('답변은 저장됐지만'));
		assert(button('[data-agency-send="2"]').disabled);assert.equal(requests.length,1);
		""")

	def test_confirmation_answer_displays_new_project_status(self):
		self.run_js(r"""
		await bootAgency();await openAgency('questions');$('agency-answer-1').value='맞아';
		reply=async()=>response({ok:true,status:'명세 확정',message:'답변 저장'});
		click(button('[data-agency-send="1"]'));await flush();assert($('agency-body').textContent.includes('명세 확정'));
		assert(!$('agency-body').textContent.includes('다시 읽지 못했습니다'));assert.equal(requests.length,1);
		""")

	def test_reject_requires_reason_and_failed_result_is_visible(self):
		self.run_js(r"""
		await bootAgency();await openAgency('delivery');click($('agency-reject'));await click($('agency-submit'));assert.equal(requests.length,0);
		$('agency-reason').value='합성 반려 이유';reply=async()=>response({ok:true,status:'막힘',message:'반려했습니다'});
		await click($('agency-submit'));assert.deepEqual(requests[0],{action:'agency_reject',id:PID,hash:HASH,text:'합성 반려 이유'});
		click($('agency-close'));await openAgency('delivery');click($('agency-submit'));click($('agency-submit'));
		reply=async()=>response({ok:false,status:'막힘',message:'납품 실패',reason:'합성 오류',pushed:true,remote_matches:false},409);
		await click($('agency-submit'));assert($('agency-body').textContent.includes('합성 오류'));assert($('agency-body').textContent.includes('push: 완료'));assert($('agency-body').textContent.includes('미확인 또는 불일치'));
		""")

	def test_unknown_network_result_blocks_retry_in_same_dialog(self):
		self.run_js(r"""
		await bootAgency();await openAgency('delivery');click($('agency-submit'));click($('agency-submit'));
		reply=async()=>{throw new Error('합성 연결 실패')};await click($('agency-submit'));await flush();
		assert.equal(requests.length,1);assert($('agency-submit').disabled);assert($('agency-status').textContent.includes('결과를 받지 못했습니다'));
		click($('agency-submit'));assert.equal(requests.length,1);click($('agency-close'));assert(!$('agency-dialog').open);
		""")
