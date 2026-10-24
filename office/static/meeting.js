'use strict';
(()=>{
	function init(){
		const bridge=window.officeBridge,$=id=>document.getElementById(id),esc=bridge.esc;
		const dialog=$('meeting-dialog'),body=$('meeting-body'),submit=$('meeting-submit'),popover=$('meeting-popover');
		const statuses={participants_pending:'참석자 확인 대기',questions_pending:'질문 답변 대기',answers_pending:'답변 확인 대기',running:'진행 중',completed:'완료',failed:'실패',budget_exhausted:'예산 소진',questions_exhausted:'질문 한도 소진',record_pending:'기록 대기'};
		const items=group=>Array.isArray(group?.items)?group.items:[],proposals=new Map();
		let phase='',busy=false,stale=false,focus=null,popoverFocus=null,point={x:12,y:12},card=null,meeting=null,answers=new Map(),selected=new Set(),avatarArt;
		const state=()=>bridge.state(),meetings=()=>items(state()?.meetings),cards=()=>items(state()?.questions).filter(c=>c.kind!=='chat');
		function unavailable(){
			if(!bridge.token())return '반영 세션을 읽지 못했습니다. 화면을 다시 열어 주세요.';
			if(!state()?.meetings||state().meetings.status==='확인 불가')return state()?.meetings?.reason||'회의 상태를 확인할 수 없습니다.';
			return '';
		}
		const otherDialog=()=>!!document.querySelector('dialog[open]');
		function positionPopover(){
			const rect=popover.getBoundingClientRect();
			popover.style.left=Math.max(12,Math.min(point.x,innerWidth-rect.width-12))+'px';
			popover.style.top=Math.max(12,Math.min(point.y,innerHeight-rect.height-12))+'px';
		}
		function closePopover(restore=true){
			if(popover.hidden)return;
			popover.hidden=true;
			if(restore&&popoverFocus?.isConnected)popoverFocus.focus({preventScroll:true});
		}
		function openPopover(anchor){
			if(otherDialog())return;
			popoverFocus=document.activeElement;
			point=anchor?.x!==undefined?anchor:{x:anchor?.getBoundingClientRect().left||12,y:anchor?.getBoundingClientRect().bottom||12};
			const reason=unavailable();
			$('meeting-unavailable').textContent=reason;$('meeting-request').disabled=!!reason;
			const unreadable=!state()?.meetings||state().meetings.status==='확인 불가';
			$('meeting-recent').innerHTML=meetings().slice(0,5).map(row=>'<button class="row meeting-recent" data-meeting-id="'+esc(row.id)+'"'+(unreadable||reason&&['participants_pending','questions_pending','answers_pending'].includes(row.status)?' disabled':'')+'><span>'+esc(Array.from(row.agenda||'회의').slice(0,40).join(''))+'</span><small>'+esc(statuses[row.status]||'확인 불가')+'</small></button>').join('')||'<p class="muted">최근 회의가 없습니다.</p>';
			popover.hidden=false;positionPopover();$('meeting-popover-close').focus();
		}
		function hideDialog(){
			dialog.close();body.replaceChildren();phase='';card=null;meeting=null;answers.clear();selected.clear();
			if(focus?.isConnected)focus.focus({preventScroll:true});else $('close-panel').focus();
		}
		function closeDialog(){if(!busy)hideDialog()}
		function begin(title,next){
			if(document.querySelector('dialog[open]')&&!dialog.open)return false;
			if(!dialog.open){focus=popover.hidden?document.activeElement:popoverFocus;closePopover(false);dialog.showModal()}
			phase=next;stale=false;$('meeting-title').textContent=title;$('meeting-status').textContent='';submit.hidden=false;
			$('meeting-title').focus({preventScroll:true});dialog.scrollTop=0;
			return true;
		}
		function controls(){
			const locked=busy||stale||!!unavailable();
			body.querySelectorAll('button,textarea,select').forEach(el=>el.disabled=locked);
			$('meeting-close').disabled=busy;dialog.setAttribute('aria-busy',String(busy));
			let valid=false;
			if(phase==='request'){
				const text=$('meeting-text').value,length=Array.from(text).length;
				$('meeting-remaining').textContent=(2000-length)+'자 남음';
				valid=!!text.trim()&&length<=2000&&!!$('meeting-target').value;
			}else if(phase==='questions')valid=card.questions.length>0&&card.questions.every(q=>answers.has(q.id));
			else if(phase==='summary'||phase==='waiting')valid=true;
			else if(phase==='participants'){
				valid=selected.size>=2&&selected.size<=4;
				$('meeting-participant-hint').textContent=valid?'':'2~4명을 골라 주세요';
			}
			submit.disabled=phase==='waiting'?busy:locked||!valid;
		}
		function status(text){$('meeting-status').textContent=text;$('meeting-status').focus({preventScroll:true})}
		async function send(payload,waiting,success){
			if(busy||stale||unavailable()||submit.disabled)return;
			busy=true;controls();status(waiting);
			try{
				const response=await fetch('/api/action',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-Office-Token':bridge.token()},body:JSON.stringify(payload)}),data=await response.json();
				if(!response.ok||!data.ok){
					status(data.message||'회의 결과를 확인할 수 없습니다.');
					if(response.status===409){stale=true;await bridge.refresh()}
				}else await success(data.draft);
			}catch{
				stale=true;status('결과를 받지 못했습니다. 상태를 다시 읽고 회의를 확인해 주세요.');await bridge.refresh();
			}finally{busy=false;if(dialog.open)controls()}
		}
		function openRequest(){
			if(otherDialog()||unavailable())return;
			if(!begin('회의 요청','request'))return;
			const targets=state()?.dev_targets||[{id:'ai-hub',label:'ai-hub'}];
			body.innerHTML='<div class="dev-editor"><label for="meeting-target">대상 저장소</label><select id="meeting-target">'+targets.map(t=>'<option value="'+esc(t.id)+'">'+esc(t.label)+'</option>').join('')+'</select><label for="meeting-text">안건</label><textarea id="meeting-text" rows="6" maxlength="2000" aria-describedby="meeting-warning meeting-remaining"></textarea><p id="meeting-remaining" class="muted">2000자 남음</p></div>';
			$('meeting-target').value=targets.find(t=>t.id==='ai-hub')?'ai-hub':targets[0]?.id||'';
			submit.textContent='분석';submit.onclick=()=>{
				controls();const people=bridge.people(),origin=(people.find(p=>p.name==='PM')||people[0])?.id,text=$('meeting-text').value;
				return send({action:'meeting_analyze',id:$('meeting-target').value,text,origin},'분석 중(1~3분)',async draft=>{
					await bridge.refresh();
					if(!draft?.id){stale=true;status('분석 결과의 회의 정보를 읽지 못했습니다.');return}
					proposals.set(draft.id,draft);openMeeting(draft);
				});
			};
			controls();$('meeting-text').focus();
		}
		function updateAnswers(){
			body.querySelectorAll('[data-answer-kind]').forEach(button=>{
				const a=answers.get(button.dataset.question);
				button.setAttribute('aria-pressed',String(a?.kind===button.dataset.answerKind&&a?.value===button.dataset.value));
			});
			body.querySelectorAll('[data-question-text]').forEach(input=>input.dataset.selected=String(answers.get(input.dataset.questionText)?.kind==='text'));
			controls();
		}
		function answerText(input){
			const value=input.value.trim(),id=input.dataset.questionText;
			if(Array.from(value).length>=2)answers.set(id,{kind:'text',value});else answers.delete(id);
			updateAnswers();
		}
		function showQuestions(value){
			if(!begin('회의 질문','questions'))return;
			card=value;answers=new Map();
			body.innerHTML='<p class="muted">회의 '+esc(card.meeting)+' · 카드 '+esc(card.card)+'</p>'+card.questions.map((q,i)=>{
				const option=(kind,value,label,detail='')=>'<button class="meeting-option" data-answer-kind="'+kind+'" data-question="'+esc(q.id)+'" data-value="'+esc(value)+'" aria-pressed="false">'+label+detail+'</button>';
				return '<fieldset class="meeting-question"><legend>'+(i+1)+'. '+esc(q.text)+'</legend><div class="meeting-options">'+q.options.map(o=>option('choice',o.id,'<strong>'+esc(o.label)+'</strong>'+(q.recommended===o.id?'<span class="state">추천</span>':''),'<small>'+esc(o.difference)+'</small>'+(q.recommended===o.id?'<small>추천 이유: '+esc(q.reason)+'</small>':''))).join('')+(q.allow_delegate?option('delegate','알아서 정해 줘','<strong>알아서 정해 줘</strong>'):'')+'</div><div class="dev-editor"><label for="meeting-text-'+i+'">직접 쓰기 (2자 이상)</label><textarea class="meeting-text" id="meeting-text-'+i+'" data-question-text="'+esc(q.id)+'" rows="2" minlength="2" maxlength="2000"></textarea></div></fieldset>';
			}).join('');
			submit.textContent='제출';submit.onclick=()=>{
				controls();
				return send({action:'meeting_answer',id:card.meeting,card:card.card,answers:card.questions.map(q=>({question_id:q.id,...answers.get(q.id)}))},'답변 제출 중',async draft=>{
					await bridge.refresh();
					if(typeof draft?.summary!=='string'){stale=true;status('답변 요약을 읽지 못했습니다.');return}
					begin('답변 확인','summary');body.innerHTML='<p class="meeting-summary">'+esc(draft.summary)+'</p>';
					submit.textContent='확인';submit.onclick=()=>send({action:'meeting_confirm',id:card.meeting,card:card.card},'답변 확인 중',async()=>{
						const id=card.meeting;await bridge.refresh();
						const row=meetings().find(m=>m.id===id);
						if(row?.status==='participants_pending'||cards().some(c=>c.meeting===id))openMeeting(row||{id});
						else{hideDialog();bridge.toast('답변을 확인했습니다')}
					});
				});
			};
			updateAnswers();
		}
		function drawAvatars(roles){
			body.querySelectorAll('[data-role-avatar]').forEach(canvas=>{
				const role=roles.find(r=>r.key===canvas.dataset.roleAvatar),person=bridge.people().find(p=>p.name===role.name);
				if(!person||typeof createPixelArt!=='function')return;
				if(!avatarArt)avatarArt=createPixelArt({width:1,height:1,ground:'tile',palette:{grass:getComputedStyle(document.documentElement).getPropertyValue('--ground').trim()}});
				avatarArt.drawActor(canvas.getContext('2d'),{...person,x:18,y:54,face:'down',facing:'front',walking:false},0);
				canvas.hidden=false;canvas.nextElementSibling.hidden=true;
			});
		}
		function updateParticipants(){
			body.querySelectorAll('[data-participant]').forEach(button=>{
				const on=selected.has(button.dataset.participant);button.setAttribute('aria-pressed',String(on));button.querySelector('small').textContent=on?'참석':'제외';
			});
			const proposed=proposals.get(meeting.id)?.participants||Object.entries(meeting.participant_reasons||{}).map(([role,reason])=>({role,reason}));
			const reasons=proposed.filter(p=>selected.has(p.role||p.key)&&p.reason);
			$('meeting-reasons').innerHTML=reasons.length?reasons.map(p=>'<p title="'+esc(p.reason)+'">'+esc((state()?.meeting_roles||[]).find(r=>r.key===(p.role||p.key))?.name||p.role)+': '+esc(p.reason)+'</p>').join(''):'<p class="meeting-summary">'+esc(meeting.summary||proposals.get(meeting.id)?.cards?.map(c=>c.summary).filter(Boolean).join('\n')||'안건 분석 요약을 현재 상태에서 확인할 수 없습니다.')+'</p>';
			controls();
		}
		function showParticipants(row){
			if(!begin('참석자 확인','participants'))return;
			meeting=row;const roles=state()?.meeting_roles||[],proposed=proposals.get(row.id)?.participants||row.participants||[];
			selected=new Set(proposed.map(p=>typeof p==='string'?p:p.role||p.key).filter(key=>roles.some(r=>r.key===key)));
			body.innerHTML='<p class="intro">'+esc(row.agenda||'회의 참석자를 확인해 주세요.')+'</p><div class="meeting-chips">'+roles.map(r=>'<button class="meeting-chip" data-participant="'+esc(r.key)+'" aria-pressed="false"><canvas width="36" height="56" data-role-avatar="'+esc(r.key)+'" aria-hidden="true" hidden></canvas><span class="meeting-initial" aria-hidden="true">'+esc(Array.from(r.name)[0]||'')+'</span><span>'+esc(r.name)+'</span><small></small></button>').join('')+'</div><div id="meeting-reasons" class="meeting-reasons"></div><p id="meeting-participant-hint" class="muted" role="status"></p>';
			submit.textContent='확정하고 회의 시작';submit.onclick=()=>{
				controls();const id=meeting.id;return send({action:'meeting_start',id,participants:[...selected]},'회의 시작 중',async()=>{hideDialog();bridge.toast('회의를 시작했습니다');await window.officeMeetingPanel?.open(id);await bridge.refresh()});
			};
			updateParticipants();drawAvatars(roles);
		}
		function openMeeting(value){
			const row={...value,...meetings().find(m=>m.id===value.id)},waiting=cards().find(c=>c.meeting===row.id);
			if(waiting)showQuestions(waiting);
			else if(row.status==='questions_pending'||value.cards?.length){
				if(!begin('회의 질문','waiting'))return;
				body.innerHTML='<p class="intro">질문 카드를 읽지 못했습니다. 상태를 다시 읽어 주세요.</p>';submit.textContent='다시 읽기';submit.disabled=false;
				submit.onclick=async()=>{submit.disabled=true;await bridge.refresh();if(dialog.open&&phase==='waiting')openMeeting({id:row.id})};
			}else if(row.status==='participants_pending')showParticipants(row);
			else{
				closePopover(false);if(dialog.open)hideDialog();
				if(window.officeMeetingPanel)window.officeMeetingPanel.open(row.id);
				else bridge.openPanel('회의','<p class="intro">'+esc(statuses[row.status]||'확인 불가')+'</p>');
			}
		}
		window.officeMeeting={
			approvals:()=>cards().map(c=>({...c,id:c.card,kind:'question',label:'AI 질문',title:(meetings().find(m=>m.id===c.meeting)?.agenda||'회의 질문')+' · 질문 '+c.questions.length+'개 · '+c.meeting.slice(0,8)})),
			summary:()=>'<button data-filter="question"><b>'+(!state()||state()?.approvals?.questions?.status==='확인 불가'?'?':esc(state()?.approvals?.questions?.count??0))+'</b>AI 질문</button>',
			button:row=>'<button class="apply-button" data-question-card="'+esc(row.card)+'" data-question-meeting="'+esc(row.meeting)+'"'+(unavailable()?' disabled':'')+'>답하기</button>'
		};
		body.onclick=e=>{
			const button=e.target.closest('button');if(!button||button.disabled||busy||stale)return;
			if(button.dataset.answerKind){answers.set(button.dataset.question,{kind:button.dataset.answerKind,value:button.dataset.value});updateAnswers()}
			else if(button.dataset.participant){const key=button.dataset.participant;if(selected.has(key))selected.delete(key);else selected.add(key);updateParticipants()}
		};
		body.oninput=e=>{if(e.target.dataset.questionText!==undefined)answerText(e.target);else controls()};
		$('meeting-recent').onclick=e=>{const b=e.target.closest('[data-meeting-id]');if(b&&!b.disabled&&!otherDialog())openMeeting({id:b.dataset.meetingId})};
		$('meeting-request').onclick=openRequest;$('meeting-close').onclick=closeDialog;$('meeting-popover-close').onclick=()=>closePopover();
		dialog.addEventListener('cancel',e=>{e.preventDefault();closeDialog()});
		document.addEventListener('office-meeting-room',e=>openPopover(e.detail));
		document.addEventListener('click',e=>{
			const b=e.target.closest('button');if(!b||b.disabled)return;
			if(b.dataset.room==='meeting'){
				e.preventDefault();e.stopImmediatePropagation();openPopover(b);
			}else if(b.dataset.questionCard&&b.dataset.questionMeeting){
				e.preventDefault();e.stopImmediatePropagation();if(otherDialog()||unavailable())return;
				const value=cards().find(c=>c.card===b.dataset.questionCard&&c.meeting===b.dataset.questionMeeting);
				if(value)showQuestions(value);else bridge.refresh();
			}
		},true);
		document.addEventListener('pointerdown',e=>{if(!popover.hidden&&!popover.contains(e.target))closePopover(false)},true);
		window.addEventListener('keydown',e=>{
			if(dialog.open){e.stopImmediatePropagation();if(e.key==='Escape'){e.preventDefault();closeDialog()}}
			else if(!popover.hidden&&e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();closePopover()}
		},true);
		window.addEventListener('resize',()=>{if(!popover.hidden)positionPopover()});
		if(state())bridge.refresh();
	}
	if(window.officeBridge)init();else document.addEventListener('office-ready',init,{once:true});
})();
