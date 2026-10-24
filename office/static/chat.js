'use strict';
(()=>{
	function init(){
		const bridge=window.officeBridge,$=id=>document.getElementById(id),esc=bridge.esc;
		const dialog=$('chat-dialog'),body=$('chat-body'),submit=$('chat-submit');
		const state=()=>bridge.state(),rows=()=>state()?.chats?.items||[],cards=()=>(state()?.questions?.items||[]).filter(c=>c.kind==='chat');
		const labels={idle:'대기',busy:'답을 쓰는 중',closed:'닫힘'};
		let phase='',busy=false,stale=false,focus=null,chat=null,answers=new Map();
		function unavailable(newChat=false){
			if(!bridge.token())return '반영 세션을 읽지 못했습니다. 화면을 다시 열어 주세요.';
			if(!state()?.chats||state().chats.status==='확인 불가')return state()?.chats?.reason||'대화 상태를 확인할 수 없습니다.';
			if(newChat&&!state()?.chat_plans?.length)return '사용할 수 있는 대화 AI가 없습니다.';
			return '';
		}
		function person(){
			const reason=unavailable(true),unreadable=!state()?.chats||state().chats.status==='확인 불가';
			return '<button class="outline-button" data-chat-open'+(reason?' disabled aria-describedby="chat-unavailable"':'')+'>대화</button><p id="chat-unavailable" class="chat-notice">'+esc(reason)+'</p><div class="section"><h3>최근 대화</h3>'+(rows().slice(0,5).map(row=>'<button class="row chat-recent" data-chat-id="'+esc(row.id)+'"'+(unreadable?' disabled':'')+'><span>'+esc(row.target)+' · '+esc(row.plan)+' · '+esc(row.tool)+'</span><small>'+esc(labels[row.status]||'확인 불가')+'</small><small>'+esc(row.preview||'아직 답이 없습니다.')+'</small></button>').join('')||'<p class="muted">최근 대화가 없습니다.</p>')+'</div>';
		}
		async function send(payload){
			if(!bridge.token())return {ok:false,message:unavailable()};
			const response=await fetch('/api/action',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-Office-Token':bridge.token()},body:JSON.stringify(payload)}),data=await response.json();
			return {...data,ok:response.ok&&data.ok===true,conflict:response.status===409};
		}
		function close(){
			if(busy)return;
			dialog.close();body.replaceChildren();phase='';chat=null;answers.clear();
			if(focus?.isConnected)focus.focus({preventScroll:true});else $('close-panel').focus();
		}
		function begin(title,next){
			if(document.querySelector('dialog[open]')&&!dialog.open)return false;
			if(!dialog.open){focus=document.activeElement;$('meeting-popover').hidden=true;dialog.showModal()}
			phase=next;stale=false;$('chat-title').textContent=title;$('chat-status').textContent='';
			$('chat-title').focus({preventScroll:true});dialog.scrollTop=0;
			return true;
		}
		function status(text){$('chat-status').textContent=text;$('chat-status').focus({preventScroll:true})}
		function controls(){
			const reason=unavailable(phase==='new'),locked=busy||stale||!!reason;
			body.querySelectorAll('button,textarea,select').forEach(el=>el.disabled=locked);
			$('chat-close').disabled=busy;dialog.setAttribute('aria-busy',String(busy));
			let valid=phase==='summary'||phase==='end';
			if(phase==='new')valid=!!$('chat-plan').value&&!!$('chat-target').value;
			if(phase==='questions')valid=chat.card.questions.length>0&&chat.card.questions.every(q=>answers.has(q.id));
			submit.disabled=locked||!valid;
			if(reason&&!busy&&!stale)$('chat-status').textContent=reason;
		}
		async function act(payload,waiting,success){
			controls();if(submit.disabled)return;
			busy=true;controls();status(waiting);
			try{
				const data=await send(payload);
				if(!data.ok){
					status(data.message||'대화 결과를 확인할 수 없습니다.');
					if(data.conflict){stale=true;await bridge.refresh();if(payload.action!=='chat_new')await window.officeChatDock.read(payload.id)}
				}else await success(data.draft);
			}catch{
				stale=true;status('결과를 받지 못했습니다. 상태를 다시 읽고 대화를 확인해 주세요.');
				await bridge.refresh();if(payload.action!=='chat_new')await window.officeChatDock.read(payload.id);
			}finally{busy=false;if(dialog.open)controls()}
		}
		function openNew(){
			if(document.querySelector('dialog[open]')||unavailable(true)||!begin('새 대화','new'))return;
			const plans=state().chat_plans,targets=state().dev_targets||[];
			body.innerHTML='<div class="dev-editor"><label for="chat-plan">대화 AI</label><select id="chat-plan">'+plans.map(p=>'<option value="'+esc(p.id)+'">'+esc(p.id)+' · '+esc(p.tool)+'</option>').join('')+'</select><label for="chat-target">대상 저장소</label><select id="chat-target" aria-describedby="chat-target-hint">'+targets.map(t=>'<option value="'+esc(t.id)+'">'+esc(t.label||t.id)+'</option>').join('')+'</select><p id="chat-target-hint" class="chat-notice">선택한 AI가 대상 저장소 글을 읽습니다.</p></div>';
			$('chat-plan').value=plans[0].id;$('chat-target').value=(targets.find(t=>t.id==='ai-hub')||targets[0])?.id||'';
			submit.textContent='대화 시작';submit.onclick=()=>act({action:'chat_new',id:$('chat-target').value,plan:$('chat-plan').value},'대화 준비 중',async draft=>{
				if(!/^c-[a-f0-9]{32}$/.test(draft?.id)){stale=true;status('새 대화 정보를 읽지 못했습니다. 상태를 다시 읽어 주세요.');return}
				busy=false;close();await window.officeChatDock.open(draft.id);await bridge.refresh();
			});
			controls();if(!targets.length)status('대상 저장소를 확인할 수 없습니다.');$('chat-plan').focus();
		}
		function updateAnswers(){
			body.querySelectorAll('[data-chat-answer]').forEach(b=>{
				const a=answers.get(b.dataset.question);b.setAttribute('aria-pressed',String(a?.kind===b.dataset.chatAnswer&&a?.value===b.dataset.value));
			});
			body.querySelectorAll('[data-chat-text]').forEach(input=>input.dataset.selected=String(answers.get(input.dataset.chatText)?.kind==='text'));
			controls();
		}
		function showSummary(value,summary){
			if(!begin('답변 확인','summary'))return;
			chat=value;body.innerHTML='<p class="chat-summary">'+esc(summary)+'</p>';
			submit.textContent='확인';submit.onclick=()=>act({action:'chat_confirm',id:chat.id,card:chat.card.id},'답변 확인 중',async()=>{
				const id=chat.id;window.officeChatDock.update(id,{status:'busy',card:{...chat.card,status:'confirmed'},failure:''});
				busy=false;close();bridge.toast('답변을 확인했습니다. PM이 답을 쓰는 중입니다.');
			});
			controls();
		}
		function openQuestions(value){
			if(busy||unavailable()||value.status==='closed'||value.status==='busy')return;
			if(value.card?.status==='answered'){showSummary(value,value.card.summary);return}
			if(value.card?.status!=='waiting'){bridge.toast('질문 상태가 바뀌었습니다. 대화를 확인해 주세요.');return}
			if(!begin('대화 질문','questions'))return;
			chat=value;answers=new Map();
			body.innerHTML=chat.card.questions.map((q,i)=>{
				const option=(kind,value,label,detail='')=>'<button class="chat-option" data-chat-answer="'+kind+'" data-question="'+esc(q.id)+'" data-value="'+esc(value)+'" aria-pressed="false">'+label+detail+'</button>';
				return '<fieldset class="chat-question"><legend>'+(i+1)+'. '+esc(q.text)+'</legend><div class="chat-options">'+q.options.map(o=>option('choice',o.id,'<strong>'+esc(o.label)+'</strong>'+(q.recommended===o.id?' <span>추천</span>':''),'<small>'+esc(o.difference)+'</small>'+(q.recommended===o.id?'<small>추천 이유: '+esc(q.reason)+'</small>':''))).join('')+(q.allow_delegate?option('delegate','알아서 정해 줘','<strong>알아서 정해 줘</strong>'):'')+'</div><div class="dev-editor"><label for="chat-text-'+i+'">직접 쓰기 (2자 이상)</label><textarea id="chat-text-'+i+'" data-chat-text="'+esc(q.id)+'" rows="2" minlength="2" maxlength="2000"></textarea></div></fieldset>';
			}).join('');
			submit.textContent='제출';submit.onclick=()=>act({action:'chat_answer',id:chat.id,card:chat.card.id,answers:chat.card.questions.map(q=>({question_id:q.id,...answers.get(q.id)}))},'답변 제출 중',async draft=>{
				if(typeof draft?.summary!=='string'){stale=true;status('답변 요약을 읽지 못했습니다. 대화를 다시 열어 주세요.');return}
				const value=chat;await window.officeChatDock.read(value.id);await bridge.refresh();showSummary(value,draft.summary);
			});
			updateAnswers();
		}
		function openEnd(value){
			if(unavailable()||busy||value.status!=='idle'||!begin('대화 끝내기','end'))return;
			chat=value;body.innerHTML='<p class="chat-summary">대화를 끝내면 더 보낼 수 없습니다. 대화 기록은 남습니다. 끝내시겠습니까?</p>';
			submit.textContent='대화 끝내기';submit.onclick=()=>act({action:'chat_close',id:chat.id},'대화 끝내는 중',async()=>{
				const id=chat.id;window.officeChatDock.update(id,{status:'closed'});busy=false;close();await bridge.refresh();
			});controls();
		}
		window.officeChat={person,unavailable,send,openNew,openQuestions,openEnd,
			approvals:()=>cards().map(c=>({...c,id:c.card,kind:'question',label:'AI 질문',title:'대화 · '+(rows().find(r=>r.id===c.chat)?.target||'대상 확인 대기')+' · 질문 '+c.questions.length+'개'})),
			button:row=>'<button class="apply-button" data-chat-question="'+esc(row.chat)+'"'+(unavailable()?' disabled':'')+'>답하기</button>'
		};
		body.onclick=e=>{
			const b=e.target.closest('[data-chat-answer]');if(!b||b.disabled||busy||stale)return;
			answers.set(b.dataset.question,{kind:b.dataset.chatAnswer,value:b.dataset.value});updateAnswers();
		};
		body.oninput=e=>{
			if(e.target.dataset.chatText!==undefined){
				const value=e.target.value.trim(),id=e.target.dataset.chatText,length=Array.from(value).length;
				if(length>=2&&length<=2000)answers.set(id,{kind:'text',value});else answers.delete(id);
				updateAnswers();
			}else controls();
		};body.onchange=controls;
		$('chat-close').onclick=close;dialog.addEventListener('cancel',e=>{e.preventDefault();close()});
		document.addEventListener('click',async e=>{
			const b=e.target.closest('button');if(!b||b.disabled||document.querySelector('dialog[open]'))return;
			if(b.dataset.chatOpen!==undefined)openNew();
			else if(b.dataset.chatId)await window.officeChatDock.open(b.dataset.chatId);
			else if(b.dataset.chatQuestion){
				if(unavailable())return;
				await window.officeChatDock.open(b.dataset.chatQuestion);
				const value=window.officeChatDock.detail();if(value?.id===b.dataset.chatQuestion&&!dialog.open)openQuestions(value);
			}
		});
		window.addEventListener('keydown',e=>{if(dialog.open){e.stopImmediatePropagation();if(e.key==='Escape'){e.preventDefault();close()}}},true);
		document.addEventListener('office-state',()=>{if(dialog.open)controls()});
		if(state())bridge.refresh();
	}
	if(window.officeBridge)init();else document.addEventListener('office-ready',init,{once:true});
})();
