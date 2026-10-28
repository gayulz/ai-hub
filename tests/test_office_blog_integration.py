"""합성 화면에서 사무실과 가짜 블로그 HTTP 처리기까지 검증한다."""

import json

from office_blog_integration_support import BlogIntegrationCase, REMOTE_HOST, REMOTE_LOGIN


DOM = r"""
	const actions=[['blog_state','작업 상태 보기','블로그 상태'],
		['blog_stage_retry','임시저장 다시 시도','임시저장 재시도'],
		['blog_redraft','초안 다시 쓰기','초안 다시 쓰기'],
		['blog_clip_scan','클립 스캔','클립 스캔']];
	const person=scene.people.find(p=>p.department==='blog');assert(person);
	const markers=await bridge({kind:'markers'});
	const safe=()=>{for(const element of elements.values())for(const marker of markers){
		assert(!String(element.textContent).includes(marker));assert(!String(element.innerHTML).includes(marker));
	}};
	for(const mode of MODES){
		await bridge({kind:'mode',mode});
		for(const [action,label,activity] of actions){
			click({person:String(person.id)});
			assert(element('panel-body').innerHTML.includes('data-blog-action="'+action+'"'));
			const before=await bridge({kind:'count'}),requestsBefore=requests.filter(url=>url==='/api/action').length;
			const button=click({blogAction:action});
			assert(element('action-dialog').open);assert(element('action-title').textContent.includes(label));
			assert.equal(await bridge({kind:'count'}),before);
			element('action-dialog').listeners.get('cancel')({preventDefault(){}});
			assert(!element('action-dialog').open);assert.equal(document.activeElement,button);
			assert.equal(await bridge({kind:'count'}),before);
			assert.equal(requests.filter(url=>url==='/api/action').length,requestsBefore);
			click({blogAction:action});
			if(action!=='blog_state'){
				const field=element(action==='blog_clip_scan'?'action-blog-kind':'action-blog-id');
				assert(element('action-confirm').disabled);
				field.value=action==='blog_clip_scan'?'followers':'post-001';
				(field.oninput||field.onchange)();
			}
			assert(!element('action-confirm').disabled);
			assert.equal(await bridge({kind:'count'}),before);
			const pending=element('action-confirm').onclick();
			assert.equal(requests.filter(url=>url==='/api/action').length,requestsBefore+1);
			assert.equal(actionBody.action,action);
			assert.equal(actionBody.id,action==='blog_state'?'state':action==='blog_clip_scan'?'followers':'post-001');
			await pending;
			assert.equal(await bridge({kind:'count'}),before+1);
			assert(element('action-confirm').hidden);assert(!element('action-cancel').disabled);
			let result;
			if(mode==='success'){
				result=action==='blog_state'?'조회 완료':'접수';
				if(action==='blog_state'){
					assert(element('action-status').textContent.includes('조회 완료'));
					assert(!element('action-blog-output').hidden);
					assert(element('action-blog-output').textContent.includes('#7 · STAGE'));
				}else{
					assert(element('action-status').textContent.includes('접수'));
					assert(element('action-status').textContent.includes('42'));
					assert(element('action-status').textContent.includes('완료 여부'));
				}
			}else{
				result=['auth','denied'].includes(mode)?'거부':action==='blog_state'?'실패':'결과 불명';
				assert(element('action-status').textContent.includes('상태를 확인'));
				assert(element('action-blog-output').hidden);
			}
			safe();element('action-cancel').onclick();await interval();
			const entry=activity+' · '+result;
			assert(sandbox.window.officeBridge.state().activity.some(row=>row.text===entry));
			click({view:'office'});assert(element('panel-body').innerHTML.includes(entry));safe();
		}
	}
"""


class OfficeBlogIntegrationTest(BlogIntegrationCase):
	def test_local_and_registered_remote_use_same_controls_and_record_each_action(self):
		for site in ("local", "remote"):
			with self.subTest(site=site):
				self.dom(DOM.replace("MODES", "['success']"), site)
		expected = [("blog_state", "조회 완료"), ("blog_stage_retry", "접수"),
			("blog_redraft", "접수"), ("blog_clip_scan", "접수")] * 2
		rows = self.rows()
		self.assertEqual([(row["action"], row["result"]) for row in rows], expected)
		self.assertTrue(all(set(row) == {"at", "action", "result"} for row in rows))
		self.assertEqual(len(self.fake.received), 8)
		for index, (method, path, secret, body) in enumerate(self.fake.received):
			self.assertTrue(secret == self.secret, "가짜 서버 인증 머리글 불일치")
			expected_call = (("GET", "/api/ops/state"), ("POST", "/api/ops"),
				("POST", "/api/ops"), ("POST", "/api/ops"))[index % 4]
			self.assertEqual((method, path), expected_call)
			if index % 4 == 0:
				self.assertEqual(body, b"")
			else:
				self.assertEqual(json.loads(body), (
					{"action": "stage_retry", "slug": "post-001"},
					{"action": "redraft", "slug": "post-001"},
					{"action": "clip_scan", "kind": "followers"})[index % 4 - 1])

	def test_unregistered_device_and_bad_login_never_reach_blog(self):
		headers = {"Content-Type": "application/json", "X-Office-Token": self.office.office_token}
		for action, identity in (("blog_state", "state"), ("blog_stage_retry", "post-001"),
				("blog_redraft", "post-001"), ("blog_clip_scan", "followers")):
			body = json.dumps({"action": action, "id": identity})
			for host, login in (("other.synthetic.ts.net", REMOTE_LOGIN), (REMOTE_HOST, None),
					(REMOTE_HOST, "other-owner")):
				with self.subTest(action=action, host=host, login=login):
					response = self.http("/api/action", "POST", headers, body, "remote", host, login)
					self.assertEqual(response["status"], 403)
					self.assertEqual(len(self.fake.received), 0)
		self.assertFalse((self.hub / "logs/office-actions.jsonl").exists())

	def test_failure_responses_and_timeouts_stay_private_in_screen_and_activity(self):
		self.dom(DOM.replace("MODES", "['auth','denied','timeout','error']"), "local")
		rows = self.rows()
		self.assertEqual(len(self.fake.received), 16)
		self.assertEqual(len(rows), 16)
		self.assertTrue(all(set(row) == {"at", "action", "result"} for row in rows))
		expected = ["거부"] * 8 + ["실패", "결과 불명", "결과 불명", "결과 불명"] * 2
		self.assertEqual([row["result"] for row in rows], expected)
