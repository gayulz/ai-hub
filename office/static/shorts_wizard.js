'use strict';
(()=>{
	function init(){
		const bridge=window.officeBridge,esc=bridge.esc,$=id=>document.getElementById(id);
		const dialog=$('shorts-dialog'),body=$('shorts-body'),next=$('shorts-next'),previous=$('shorts-previous');
		let config,callbacks,cards=[],answers={},id='',index=0,depth=0,base=null,dirty=false,busy=false,exiting=false,cleaning=false,outside=false;
		const current=()=>cards[index],answer=key=>answers[key],length=value=>Array.from(value||'').length;
		const marker=()=>({shorts:{id,card:current().id,depth}});
		function proposal(step){
			if(step.proposal?.source==='story'){
				const story=answer('story')?.value||'',sentence=story.match(/^[\s\S]*?(?:[.!?。！？](?=\s|$)|\n|$)/)?.[0]||story;
				return Array.from(sentence.trim()).slice(0,step.proposal.limit).join('');
			}
			return typeof step.proposal==='string'?step.proposal:'';
		}
		function valid(card){
			const value=answer(card.id)?.value;
			if(card.id==='source'||card.id==='save')return value?.kind==='preset'||(value?.kind==='path'&&!!value.value.trim()&&length(value.value)<=4096);
			return typeof value==='string'&&!!value.trim()&&length(value)<=(card.id==='story'?2000:300);
		}
		function complete(){return cards.filter(c=>c.id!=='summary').every(c=>valid(c)&&answer(c.id)?.confirmed)}
		function controls(){
			const locked=busy||exiting;
			body.inert=locked;
			body.querySelectorAll('button,textarea,input,summary').forEach(el=>{el.disabled=locked});
			previous.disabled=locked||depth<=1;next.disabled=locked||(current()?.id==='summary'?!complete():!valid(current()));
			$('shorts-close').disabled=busy;$('shorts-footer').hidden=exiting;
			dialog.setAttribute('aria-busy',String(busy));
		}
		function setAnswer(key,value,choice=''){
			const old=answer(key),changed=JSON.stringify(old?.value)!==JSON.stringify(value);
			answers[key]={...old,value,choice,confirmed:changed?false:old?.confirmed||false};dirty=true;
			if(changed&&old?.accepted!==undefined){
				const card=cards.find(c=>c.id===key);
				for(const dependent of card?.depends_on||[]){
					if(answer(dependent))answers[dependent]={...answer(dependent),confirmed:false,needs:true};
				}
			}
			controls();
		}
		function chip(value,label,selected,attribute='data-shorts-chip'){
			return '<button class="shorts-chip" '+attribute+'="'+esc(value)+'" aria-pressed="'+selected+'" aria-expanded="'+selected+'" aria-controls="shorts-difference">'+esc(label)+'</button>';
		}
		function folderHTML(card){
			const selected=answer(card.id)?.value,presets=config[card.id+'_presets'];
			return '<p class="shorts-notice">'+(card.id==='source'?'사무실 PC에 있는 폴더만 지정할 수 있습니다. 휴대폰 파일은 PC로 옮긴 뒤 접수해 주세요.':'납품할 폴더를 지정해 주세요. 접수할 때는 이 폴더를 만들거나 쓰지 않습니다.')+'</p><div class="shorts-chips">'+presets.map(p=>chip(p.id,p.label,selected?.kind==='preset'&&selected.id===p.id,'data-shorts-preset')).join('')+'</div><p id="shorts-difference" class="shorts-difference"'+(selected?.kind==='preset'?'':' hidden')+'>'+esc(selected?.kind==='preset'?'선택한 '+(presets.find(p=>p.id===selected.id)?.label||'')+' 폴더를 사용합니다.':'')+'</p><details class="shorts-direct"'+(selected?.kind==='path'?' open':'')+'><summary>직접 입력</summary><label for="shorts-path">폴더 경로</label><input id="shorts-path" type="text" maxlength="4096" autocomplete="off" spellcheck="false" aria-describedby="shorts-path-hint"><p id="shorts-path-hint" class="shorts-notice">~/로 시작하는 홈 줄임 표기 또는 절대 경로를 적어 주세요. 보호 폴더·작업 기록·볼트·Git 저장소는 사용할 수 없습니다.</p></details>';
		}
		function conditionHTML(card){
			const value=answer(card.id),proposed=proposal(card),chosen=card.chips.find(c=>c.value===value?.choice);
			return '<p>'+esc(card.question)+'</p>'+(value?.needs?'<p class="shorts-recheck">다시 확인 필요: 값을 확인한 뒤 [확정]을 눌러 주세요.</p>':'')+'<div class="shorts-chips">'+card.chips.map(c=>chip(c.value,c.value,value?.choice===c.value)).join('')+'</div><p id="shorts-difference" class="shorts-difference"'+(chosen?'':' hidden')+'>'+esc(chosen?.difference||'')+'</p>'+(proposed?'<p class="shorts-notice">제안: '+esc(proposed)+'</p><button class="shorts-chip" data-shorts-proposal>제안 선택</button>':'')+'<label for="shorts-value">직접 쓰거나 선택한 답을 고치기</label><textarea id="shorts-value" rows="3" maxlength="300" aria-describedby="shorts-remaining"></textarea><p id="shorts-remaining" class="shorts-notice"></p>';
		}
		function summaryHTML(){
			return '<p class="shorts-notice">모든 답을 확인한 뒤 접수를 확정해 주세요. 제작은 따로 시작합니다.</p>'+cards.filter(c=>c.id!=='summary').map(card=>{
				const row=answer(card.id),ready=valid(card)&&row?.confirmed;
				let value='다시 확인 필요';
				if(ready){
					value=row.value;
					if(card.id==='source'||card.id==='save')value=value.kind==='path'?value.value:config[card.id+'_presets'].find(p=>p.id===value.id)?.label||'선택한 폴더';
				}
				return '<section class="shorts-summary"><h3>'+esc(card.label)+'</h3><p'+(ready?'':' class="shorts-recheck"')+'>'+esc(value)+'</p><button class="shorts-chip" data-shorts-edit="'+esc(card.id)+'" aria-label="'+esc(card.label)+' 고치기">고치기</button></section>';
			}).join('');
		}
		function render(focus=true){
			const card=current();
			$('shorts-title').textContent=card.label;$('shorts-count').textContent=(index+1)+' / '+cards.length;
			$('shorts-progress').setAttribute('aria-valuemax',String(cards.length));$('shorts-progress').setAttribute('aria-valuenow',String(index+1));
			$('shorts-progress').setAttribute('aria-valuetext',(index+1)+' / '+cards.length+' · '+card.label);
			$('shorts-bar').style.width=((index+1)/cards.length*100)+'%';
			if(card.id==='story'){
				body.innerHTML='<label for="shorts-story">영상으로 만들 이야기를 적어 주세요.</label><p id="shorts-story-hint" class="shorts-notice">회사·서비스 이름, 비밀값, 이메일·홈 경로는 적지 마세요. 질문은 고정 목록으로 진행됩니다.</p><textarea id="shorts-story" rows="6" maxlength="2000" aria-describedby="shorts-story-hint shorts-remaining"></textarea><p id="shorts-remaining" class="shorts-notice"></p>';
				$('shorts-story').value=answer('story')?.value||'';
			}else if(card.id==='source'||card.id==='save'){
				body.innerHTML=folderHTML(card);$('shorts-path').value=answer(card.id)?.value?.kind==='path'?answer(card.id).value.value:'';
			}else if(card.id==='summary')body.innerHTML=summaryHTML();
			else{body.innerHTML=conditionHTML(card);$('shorts-value').value=answer(card.id)?.value||''}
			if($('shorts-remaining'))$('shorts-remaining').textContent=((card.id==='story'?2000:300)-length(answer(card.id)?.value))+'자 남음';
			next.textContent=card.id==='summary'?'접수 확정':answer(card.id)?.needs?'확정':'다음';
			$('shorts-status').textContent='';controls();
			if(focus){body.scrollTop=0;$('shorts-title').focus({preventScroll:true})}
		}
		function navigate(key){
			const target=cards.findIndex(c=>c.id===key);if(target<0)return;
			index=target;depth++;history.pushState(marker(),'');render();
		}
		function truncate(){cleaning='trim';history.pushState(base,'');history.back()}
		function finish(){
			const remaining=depth;dialog.close();body.replaceChildren();exiting=false;id='';
			if(remaining){cleaning='return';history.go(-remaining)}else truncate();
			if(callbacks.focus?.isConnected)callbacks.focus.focus({preventScroll:true});else $('close-panel').focus();
		}
		function requestClose(){
			if(busy||exiting)return;
			if(!dirty){finish();return}
			exiting=true;$('shorts-exit').hidden=false;controls();$('shorts-stay').focus();
		}
		async function submit(){
			controls();if(next.disabled)return;
			if(current().id!=='summary'){
				answers[current().id]={...answer(current().id),accepted:answer(current().id).value,confirmed:true,needs:false};
				navigate(cards[index+1].id);return;
			}
			busy=true;controls();$('shorts-status').textContent='접수 확인 중';
			const payload={action:'shorts_intake',id,story:answer('story').value,source:answer('source').value,save:answer('save').value,conditions:config.steps.map(s=>({id:s.id,value:answer(s.id).value}))};
			try{
				const data=await callbacks.send(payload);
				if(data.ok){const message=data.message||'접수를 확정했습니다. 제작은 따로 시작합니다.';busy=false;finish();await callbacks.done(message);return}
				busy=false;
				let field=data.field||'summary';if(field==='conditions')field=config.steps.find(s=>!answer(s.id)?.confirmed)?.id||config.steps[0].id;
				if(data.code===400&&answer(field))answers[field].confirmed=false;
				if(field!==current().id)navigate(field);
				$('shorts-status').textContent=data.message||'접수 결과를 확인할 수 없습니다.';
			}catch{$('shorts-status').textContent='결과를 받지 못했습니다. 최근 접수를 확인한 뒤 같은 요청을 다시 확인해 주세요.'}
			finally{busy=false;if(dialog.open)controls()}
		}
		function viewport(){
			if(!dialog.open)return;
			const view=window.visualViewport;
			dialog.style.setProperty('--shorts-height',(view?.height||innerHeight)+'px');
			dialog.style.setProperty('--shorts-top',((view?.offsetTop||0)+12)+'px');
			if(body.contains(document.activeElement))document.activeElement.scrollIntoView({block:'nearest'});
		}
		function open(data,options){
			if(cleaning||dialog.open||document.querySelector('dialog[open]'))return;
			config=data;callbacks=options;answers={};index=0;depth=1;dirty=false;busy=false;exiting=false;outside=false;
			id='s-'+crypto.randomUUID().replace(/-/g,'');
			cards=[{id:'story',label:'이야기'},{id:'source',label:'소스 폴더'},...config.steps,{id:'save',label:'저장 폴더'},{id:'summary',label:'최종 요약'}];
			base=history.state;history.pushState(marker(),'');$('shorts-exit').hidden=true;$('meeting-popover').hidden=true;
			dialog.showModal();render();viewport();
		}
		body.onclick=e=>{
			if(busy||exiting)return;
			const button=e.target.closest('button');if(!button||button.disabled)return;
			if(button.dataset.shortsEdit){navigate(button.dataset.shortsEdit);return}
			if(button.dataset.shortsPreset){setAnswer(current().id,{kind:'preset',id:button.dataset.shortsPreset});render(false);[...body.querySelectorAll('[data-shorts-preset]')].find(b=>b.dataset.shortsPreset===button.dataset.shortsPreset)?.focus();return}
			const value=button.dataset.shortsChip??(button.dataset.shortsProposal!==undefined?proposal(current()):null);
			if(value!==null){setAnswer(current().id,value,value);render(false);$('shorts-value').focus()}
		};
		body.oninput=e=>{
			if(busy||exiting)return;
			const field=e.target;
			if(field.id==='shorts-story')setAnswer('story',field.value);
			else if(field.id==='shorts-path')setAnswer(current().id,{kind:'path',value:field.value});
			else if(field.id==='shorts-value')setAnswer(current().id,field.value);
			else return;
			body.querySelectorAll('[aria-pressed]').forEach(button=>{button.setAttribute('aria-pressed','false');button.setAttribute('aria-expanded','false')});
			if($('shorts-difference'))$('shorts-difference').hidden=true;
			if($('shorts-remaining'))$('shorts-remaining').textContent=((current().id==='story'?2000:300)-length(field.value))+'자 남음';
		};
		previous.onclick=()=>{if(!previous.disabled)history.back()};next.onclick=submit;$('shorts-close').onclick=requestClose;
		$('shorts-discard').onclick=finish;
		$('shorts-stay').onclick=()=>{exiting=false;$('shorts-exit').hidden=true;if(outside){outside=false;depth=1;history.pushState(marker(),'')}controls();$('shorts-title').focus()};
		dialog.addEventListener('cancel',e=>{e.preventDefault();requestClose()});
		dialog.addEventListener('focusin',()=>viewport());
		window.addEventListener('popstate',e=>{
			if(cleaning==='return'){truncate();return}
			if(cleaning==='trim'){cleaning=false;return}
			if(!dialog.open)return;
			if(e.state?.shorts?.id===id){
				depth=e.state.shorts.depth;index=cards.findIndex(c=>c.id===e.state.shorts.card);render();
			}else{depth=0;outside=true;requestClose()}
		});
		window.addEventListener('keydown',e=>{if(dialog.open){e.stopImmediatePropagation();if(e.key==='Escape'){e.preventDefault();requestClose()}}},true);
		window.visualViewport?.addEventListener('resize',viewport);window.visualViewport?.addEventListener('scroll',viewport);window.addEventListener('resize',viewport);
		window.officeShortsWizard={open};
	}
	if(window.officeBridge)init();else document.addEventListener('office-ready',init,{once:true});
})();
