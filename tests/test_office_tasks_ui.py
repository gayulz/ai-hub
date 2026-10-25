"""합성 DOM으로 TODO 등록·완료·입력 보존과 요청 계약을 확인한다."""

import shutil
import subprocess
import unittest

from office.server import STATIC
from test_office_meeting_ui import HARNESS


SETUP = r"""
sandbox.crypto=require('crypto').webcrypto;
state.tasks={status:'정상',items:[]};state.approvals.tasks={status:'정상',items:[]};
state.questions.items=[];state.approvals.questions.count=0;
function publish(row){
	state.tasks.items=state.tasks.items.filter(r=>!(r.kind===row.kind&&r.id===row.id)).concat(row);
	state.approvals.tasks.items=state.tasks.items.filter(r=>r.kind==='task'&&r.status==='open');
	state.approvals.decisions.items=state.tasks.items.filter(r=>r.kind==='decision'&&r.status==='open');
}
reply=async payload=>{
	const row={...payload,kind:payload.kind||'task',version:(payload.version||0)+1,status:payload.action==='task_complete'?'done':'open',created:state.updated,updated:state.updated,completed:payload.action==='task_complete'?state.updated:null};delete row.action;publish(row);return response({ok:true,task:row,message:'합성 저장 완료'});
};
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve()};
async function bootTasks(){await boot();vm.runInContext(fs.readFileSync(root+'/tasks.js','utf8'),sandbox);await flush();click(button('[data-view="approvals"]'))}
function editTask(id,value){$(id).value=value;$(id).oninput()}
async function actTask(id){await click($(id));await flush()}
function newTask(){click(button('[data-task-add]'));assert($('task-dialog').open)}
const taskButton=kind=>button('[data-task-'+kind+']');
"""


@unittest.skipUnless(shutil.which("node"), "JavaScript 실행기 없음")
class OfficeTasksUITest(unittest.TestCase):
	def run_js(self, checks):
		result = subprocess.run([shutil.which("node"), "-e", HARNESS + SETUP + checks
			+ "\n})().catch(e=>{console.error(e);process.exitCode=1});", str(STATIC)], capture_output=True, text=True, timeout=15)
		self.assertEqual(result.returncode, 0, result.stderr)

	def test_register_edit_complete_archive_and_reopen(self):
		self.run_js(r"""
		await bootTasks();newTask();assert($('task-save').disabled);assert.equal(document.activeElement,$('task-name'));
		editTask('task-name','합성 <script>아이디어</script>');editTask('task-memo','첫 줄\n둘째 줄');
		await actTask('task-save');assert(!$('task-dialog').open);assert.equal(requests.length,1);assert.equal(requests[0].action,'task_create');assert(/^t-[a-f0-9]{32}$/.test(requests[0].id));
		assert.equal($('approval-count').textContent,'1');assert.equal($('panel-body').querySelectorAll('script').length,0);assert.equal(taskButton('toggle').getAttribute('aria-checked'),'false');
		click(taskButton('id'));editTask('task-memo','처리한 내용');await actTask('task-save');assert.equal(requests.at(-1).action,'task_update');assert.equal(requests.at(-1).version,1);
		click(taskButton('toggle'));await flush();assert.equal(requests.at(-1).action,'task_complete');assert.equal($('approval-count').textContent,'0');assert.equal(document.querySelectorAll('[data-task-id]').length,0);
		click(button('[data-filter="completed"]'));assert($('panel-body').textContent.includes('처리한 내용'));assert($('panel-body').textContent.includes('완료'));assert.equal(taskButton('toggle').getAttribute('aria-checked'),'true');
		click(taskButton('id'));assert.equal($('task-toggle').textContent,'다시 열기');await actTask('task-toggle');assert.equal(requests.at(-1).action,'task_reopen');assert.equal($('approval-count').textContent,'1');
		click(button('[data-filter="task"]'));assert($('panel-body').textContent.includes('합성 <script>아이디어</script>'));
		""")

	def test_auto_refresh_preserves_draft_focus_and_failed_save(self):
		self.run_js(r"""
		await bootTasks();newTask();editTask('task-name','합성 아이디어');editTask('task-memo','아직 저장 안 한 메모');$('task-memo').focus();
		const editor=$('task-memo');await interval();assert.strictEqual($('task-memo'),editor);assert.equal(editor.value,'아직 저장 안 한 메모');assert.equal(document.activeElement,editor);
		editTask('task-name','가'.repeat(201));assert($('task-save').disabled);editTask('task-name','합성 아이디어');editTask('task-memo','가'.repeat(2001));assert($('task-save').disabled);editTask('task-memo','입력 보존');
		reply=async()=>response({ok:false,message:'합성 저장 실패'},400);await actTask('task-save');assert.equal($('task-status').textContent,'합성 저장 실패');assert.equal($('task-memo').value,'입력 보존');assert(!$('task-save').disabled);
		reply=async()=>{throw new Error('합성 연결 실패')};await actTask('task-save');assert($('task-save').disabled);assert(!$('task-reload').hidden);assert.equal($('task-memo').value,'입력 보존');assert.equal(requests.length,2);
		await actTask('task-reload');assert(!$('task-save').disabled);assert.equal($('task-memo').value,'입력 보존');assert.equal(requests.length,2);
		$('task-dialog').dispatchEvent(new Event('cancel'));assert(!$('task-dialog').open);
		""")

	def test_busy_duplicate_clicks_and_conflict_reload(self):
		self.run_js(r"""
		await bootTasks();newTask();editTask('task-name','합성 작업');let resolve;reply=()=>new Promise(r=>resolve=r);
		const sending=click($('task-save'));assert($('task-save').disabled&&$('task-close').disabled);click($('task-save'));assert.equal(requests.length,1);
		$('task-dialog').dispatchEvent(new Event('cancel'));assert($('task-dialog').open);
		const created={...requests[0],kind:'task',version:1,status:'open',created:state.updated,completed:null};delete created.action;publish(created);resolve(response({ok:true,task:created,message:'합성 완료'}));await sending;await flush();
		click(taskButton('id'));editTask('task-memo','오래된 화면 수정');reply=async()=>response({ok:false,message:'합성 충돌'},409);
		publish({...created,version:2,memo:'다른 화면에서 수정'});await actTask('task-save');assert($('task-save').disabled);assert.equal($('task-memo').value,'오래된 화면 수정');
		await actTask('task-reload');assert.equal($('task-memo').value,'다른 화면에서 수정');assert(!$('task-save').disabled);
		""")

	def test_decisions_only_get_manual_completion_and_no_approval_actions(self):
		self.run_js(r"""
		publish({id:'d01',kind:'decision',title:'합성 결정',reply:'합성 선택 회신 <script>글</script>',memo:'',version:0,status:'open'});
		state.approvals.runs.items=[{id:'2026-10-26-001',status:'승인 필요'}];await bootTasks();
		assert.equal(document.querySelectorAll('[data-task-toggle]').length,1);assert.equal(document.querySelectorAll('[data-action="apply"]').length,1);
		click(taskButton('id'));assert($('task-name').readOnly);assert.equal($('task-source').textContent,'합성 선택 회신 <script>글</script>');assert.equal($('task-source').querySelectorAll('script').length,0);editTask('task-memo','합성 처리 완료');await actTask('task-toggle');
		assert.equal(requests.at(-1).kind,'decision');assert.equal(requests.at(-1).action,'task_complete');assert.equal(requests.at(-1).memo,'합성 처리 완료');assert.equal($('approval-count').textContent,'1');
		assert.equal(document.querySelectorAll('[data-action="apply"]').length,1);
		""")

	def test_no_session_or_unreadable_store_disables_mutations(self):
		for preparation in ("token=null;", "state.tasks.status='확인 불가';state.approvals.tasks.status='확인 불가';"):
			with self.subTest(preparation=preparation):
				self.run_js(preparation + r"""
				await bootTasks();assert(button('[data-task-add]').disabled);click(button('[data-task-add]'));assert(!$('task-dialog').open);assert.equal(requests.length,0);
				""")
