"""브라우저 없이 블로그 역할의 확인 창과 사무실 요청을 시험한다."""

import shutil
import subprocess
import unittest

from office.server import STATIC
from test_office_static import HARNESS


BLOG_TEST = r"""
	if(process.argv[2].startsWith('blog-ops')){
		const actions=[['blog_state','작업 상태 보기'],['blog_stage_retry','임시저장 다시 시도'],
			['blog_redraft','초안 다시 쓰기'],['blog_clip_scan','클립 스캔']];
		const blogPeople=scene.people.filter(person=>person.department==='blog');
		assert(blogPeople.length>0);
		for(const person of scene.people){
			click({person:String(person.id)});
			const body=element('panel-body').innerHTML;
			for(const [action,label] of actions){
				const pattern=new RegExp('<button[^>]*data-blog-action="'+action+'"[^>]*>'+label+'</button>','g');
				const matches=body.match(pattern)||[];
				assert.equal(matches.length,person.department==='blog'?1:0);
				if(matches.length)assert.equal(matches[0].includes(' disabled'),!sessionOK);
			}
		}
		click({person:String(blogPeople[0].id)});
		if(!sessionOK){
			for(const [action] of actions)click({blogAction:action});
			assert(!element('action-dialog').open);assert.equal(actionCount,0);return;
		}
		const status={running_jobs:[{id:34,kind:'STAGE',started_at:'2026-10-28T10:00:00'}],
			recent_failures:[{kind:'GENERATE',finished_at:'2026-10-28T09:00:00'}],
			circuits:{STAGE:{open:true,consecutive_failures:3},GENERATE:{open:false,consecutive_failures:0}},
			post_counts:Object.fromEntries(['PLANNED','AWAITING_EXPERIENCE','DRAFTING','GATE_CHECK','POLISHING',
				'REVIEWING','RENDERED','STAGING','STAGED','PUBLISHED','ARCHIVED','FAILED'].map((key,i)=>[key,i]))};
		for(const [action,label] of actions){
			const start=requests.length,button=click({blogAction:action});
			assert(element('action-dialog').open);assert(element('action-title').textContent.includes(label));
			if(action==='blog_redraft')assert(element('action-description').textContent.includes('기존 초안이 바뀔 수'));
			if(action==='blog_state')assert.equal(document.activeElement,element('action-cancel'));
			else assert.equal(document.activeElement,element(action==='blog_clip_scan'?'action-blog-kind':'action-blog-id'));
			await interval();assert.equal(requests.length,start);
			element('action-dialog').listeners.get('cancel')({preventDefault(){}});
			assert(!element('action-dialog').open);assert.equal(document.activeElement,button);
			assert.equal(requests.length,start);
			click({blogAction:action});
			const field=element(action==='blog_clip_scan'?'action-blog-kind':'action-blog-id');
			if(action!=='blog_state'){
				assert(element('action-confirm').disabled);
				field.value=action==='blog_clip_scan'?'invalid':'../invalid';
				(field.oninput||field.onchange)();
				assert(element('action-confirm').disabled);await element('action-confirm').onclick();
				assert.equal(requests.length,start);
				if(action!=='blog_clip_scan')for(const bad of ['.','..','post\n','a'.repeat(256)]){
					field.value=bad;field.oninput();assert(element('action-confirm').disabled);
				}
				field.value=action==='blog_clip_scan'?'comments':'post-001';
				(field.oninput||field.onchange)();
			}
			assert(!element('action-confirm').disabled);
			const pending=action==='blog_stage_retry'?
				element('action-dialog').listeners.get('keydown')({key:'Enter',target:field,preventDefault(){}}):
				element('action-confirm').onclick();
			assert.equal(requests.length,start+1);assert.equal(requests.at(-1),'/api/action');
			assert.equal(actionBody.action,action);
			assert.equal(actionBody.id,action==='blog_state'?'state':action==='blog_clip_scan'?'comments':'post-001');
			assert.deepEqual(Object.keys(actionBody).sort(),['action','id']);
			assert(element('action-confirm').disabled&&element('action-cancel').disabled);
			await interval();assert.equal(requests.length,start+1);
			await element('action-confirm').onclick();
			element('action-dialog').listeners.get('cancel')({preventDefault(){}});
			assert(element('action-dialog').open);assert.equal(requests.length,start+1);
			actionResponse({ok:true,json:async()=>action==='blog_state'?
				{ok:true,result:'조회 완료',message:'합성 조회 완료',state:status}:
				{ok:true,result:'접수',message:'합성 접수',job_id:42}});
			await pending;assert.equal(requests.length,start+1);
			assert(element('action-confirm').hidden);assert(!element('action-cancel').disabled);
			if(action==='blog_state'){
				const output=element('action-blog-output').textContent;
				for(const word of ['진행 작업','34','STAGE','최근 실패','GENERATE','서킷','열림','닫힘',
					'글 상태별 건수',...Object.keys(status.post_counts)])assert(output.includes(word),word);
				for(const [name,count] of Object.entries(status.post_counts))assert(output.includes(name+' · '+count+'건'));
				assert(output.includes('#34 · STAGE · 2026-10-28T10:00:00'));
				assert(output.includes('GENERATE · 2026-10-28T09:00:00'));
				assert(output.includes('STAGE · 열림 · 연속 실패 3건'));
				assert(output.includes('GENERATE · 닫힘 · 연속 실패 0건'));
				assert(!element('action-blog-output').hidden);
			}else{
				assert(element('action-status').textContent.includes('접수'));
				assert(element('action-status').textContent.includes('42'));
				assert(element('action-status').textContent.includes('완료 여부'));
			}
			await element('action-confirm').onclick();assert.equal(requests.length,start+1);
			element('action-cancel').onclick();
		}
		click({blogAction:'blog_clip_scan'});
		element('action-blog-kind').value='followers';element('action-blog-kind').onchange();
		let start=requests.length,pending=element('action-confirm').onclick();
		assert.equal(actionBody.id,'followers');actionResponse({ok:true,json:async()=>({ok:true,result:'접수',job_id:8})});
		await pending;assert.equal(requests.length,start+1);element('action-cancel').onclick();
		for(const failure of ['rejected','uncertain','network','invalid-json','invalid-success','invalid-counts']){
			click({blogAction:'blog_state'});start=requests.length;pending=element('action-confirm').onclick();
			if(failure==='network')actionReject(new Error('합성 연결 실패'));
			else actionResponse({ok:failure==='invalid-success'||failure==='invalid-counts',json:async()=>{
				if(failure==='invalid-json')throw new Error('합성 형식 오류');
				if(failure==='invalid-counts')return {ok:true,result:'조회 완료',state:{...status,post_counts:{}}};
				return failure==='invalid-success'?{ok:true,result:'조회 완료',state:{}}:{ok:false,result:failure==='uncertain'?'결과 불명':'거부',message:'합성 오류'};
			}});
			await pending;assert.equal(requests.length,start+1);
			assert(element('action-status').textContent.includes('상태를 확인'));
			assert(element('action-confirm').hidden);element('action-cancel').onclick();
		}
		for(const action of ['blog_stage_retry','blog_redraft','blog_clip_scan']){
			click({blogAction:action});const field=element(action==='blog_clip_scan'?'action-blog-kind':'action-blog-id');
			field.value=action==='blog_clip_scan'?'comments':'post-001';(field.oninput||field.onchange)();
			start=requests.length;pending=element('action-confirm').onclick();actionReject(new Error('합성 연결 실패'));
			await pending;assert.equal(requests.length,start+1);assert(element('action-status').textContent.includes('상태를 확인'));
			await element('action-confirm').onclick();assert.equal(requests.length,start+1);element('action-cancel').onclick();
		}
		click({blogAction:'blog_state'});start=requests.length;pending=element('action-confirm').onclick();
		const markup='<img onerror="unexpected">';status.running_jobs[0].kind=markup;status.post_counts.unexpected=999;
		actionResponse({ok:true,json:async()=>({ok:true,result:'조회 완료',state:status})});await pending;
		assert.equal(requests.length,start+1);assert(element('action-blog-output').textContent.includes(markup));
		assert(!element('action-blog-output').textContent.includes('unexpected · 999'));
		assert.equal(element('action-blog-output').innerHTML,'');element('action-cancel').onclick();
		const original=click({blogAction:'blog_state'}),replacement=new Element();
		replacement.dataset.blogAction='blog_state';original.isConnected=false;
		element('panel-body').querySelector=selector=>selector.includes('blog_state')?replacement:null;
		await interval();element('action-cancel').onclick();assert.equal(document.activeElement,replacement);
		assert(requests.every(url=>url.startsWith('/')&&!url.startsWith('//')));
		return;
	}
"""


class OfficeBlogOpsUITest(unittest.TestCase):
	@unittest.skipUnless(shutil.which("node"), "JavaScript 실행기 없음")
	def test_synthetic_role_controls_confirmations_and_safe_results(self):
		harness = HARNESS.replace("process.argv[2]==='mobile'", "['mobile','blog-ops-mobile'].includes(process.argv[2])")
		harness = harness.replace("process.argv[2]!=='no-session'", "!['no-session','blog-ops-no-session'].includes(process.argv[2])")
		harness = harness.replace("actionCount++;actionBody=JSON.parse(options.body);", "actionCount++;actionBody=JSON.parse(options.body);if(actionBody.action.startsWith('blog_'))assert.equal(options.redirect,'error');")
		marker = "assert.deepEqual(requests,['/scene.json','/api/session','/api/state']);"
		self.assertEqual(harness.count(marker), 1)
		harness = harness.replace(marker, marker + BLOG_TEST, 1)
		for mode in ("blog-ops", "blog-ops-mobile", "blog-ops-no-session"):
			with self.subTest(mode=mode):
				result = subprocess.run([shutil.which("node"), "-e", harness, str(STATIC), mode], capture_output=True, text=True, timeout=10)
				self.assertEqual(result.returncode, 0, result.stderr)

	def test_fields_are_labelled_and_results_use_the_existing_dialog(self):
		html = (STATIC / "index.html").read_text()
		self.assertIn('for="action-blog-id"', html)
		self.assertIn('aria-describedby="action-blog-id-help"', html)
		self.assertIn('for="action-blog-kind"', html)
		self.assertIn('value="comments"', html)
		self.assertIn('value="followers"', html)
		self.assertIn('id="action-blog-output" tabindex="0"', html)
		self.assertEqual(html.count('id="action-dialog"'), 1)
		self.assertIn('.dev-editor[hidden]{display:none}', (STATIC / "office.css").read_text())
		self.assertIn('#action-blog-output:focus-visible', (STATIC / "office.css").read_text())
