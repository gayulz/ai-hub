'use strict';
(()=>{
const $=id=>document.getElementById(id),dialog=$('agency-dialog'),body=$('agency-body'),status=$('agency-status');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={merge:'본 가지 병합(시험 후 빨리 감기)',push:'push 및 원격 해시 확인',restart:'재기동 안내만 남김',document:'납품 문서 확정(병합·push 없음)'};
let data=null,view='',stage='',busy=false,uncertain=false,focus=null;
const actions=()=>'<ul>'+data.actions.map(action=>'<li>'+esc(labels[action]||action)+'</li>').join('')+'</ul>';
function buttons(close,back,submit){
	$('agency-close').textContent=close;$('agency-close').disabled=busy;
	$('agency-reject').hidden=!back;$('agency-reject').textContent=back||'';$('agency-reject').disabled=busy;
	$('agency-submit').hidden=!submit;$('agency-submit').textContent=submit||'';$('agency-submit').disabled=busy||uncertain;
}
function questions(values={}){
	stage='questions';$('agency-title').textContent='대행사 접수 질문';
	body.innerHTML='<p>'+esc(data.title)+' · '+esc(data.id)+'</p>'+(data.questions.map(q=>'<div class="agency-question"><label for="agency-answer-'+q.no+'"><b>질문 '+q.no+'.</b> '+esc(q.question)+'</label><textarea id="agency-answer-'+q.no+'" maxlength="1000" rows="3"></textarea><button class="apply-button" data-agency-send="'+q.no+'">답변 보내기</button></div>').join('')||'<p>열린 질문이 없습니다.</p>');
	for(const q of data.questions)if(values[q.no])$('agency-answer-'+q.no).value=values[q.no];
	buttons('닫기',null,null);
}
function delivery(){
	stage='delivery';$('agency-title').textContent='납품 묶음 확인';
	const verdict={pass:'통과',fail:'실패',unverified:'미검증'};
	body.innerHTML='<p>'+esc(data.title)+' · '+esc(data.id)+'</p><h3>기준별 판정</h3><ul>'+data.criteria.map(item=>'<li>'+esc(item.criterion)+' · '+esc(verdict[item.verdict])+'</li>').join('')+'</ul>'+(data.kind==='document'?'<h3>납품 문서</h3><p>'+esc(data.documents.join(', '))+' · 묶음 hash '+esc(data.hash.slice(0,12))+'</p>':'<h3>시험 결과</h3><p>'+esc(data.tests.ok?'통과':'실패 또는 미실행')+(data.tests.detail?' · '+esc(data.tests.detail):'')+'</p><p>바뀐 파일 '+esc(data.changed)+'개 · 묶음 hash '+esc(data.hash.slice(0,12))+'</p>')+'<h3>승인하면 실행될 바깥 작업</h3>'+actions()+(data.restart_notice?'<p>'+esc(data.restart_notice)+'</p>':'');
	buttons('닫기','반려','승인');
}
function confirmation(number){
	stage='confirm'+number;$('agency-title').textContent='납품 승인 확인 '+number+' / 2';
	body.innerHTML='<p>묶음 hash '+esc(data.hash.slice(0,12))+'</p><p>'+(number===1?'아래 작업을 승인할지 확인해 주세요.':'마지막 확인입니다. 아래 작업이 실행됩니다.')+'</p><h3>승인하면 실행될 바깥 작업</h3>'+actions()+(data.restart_notice?'<p>'+esc(data.restart_notice)+'</p>':'');
	buttons('취소','이전',number===1?'확인하고 계속':'최종 승인 · 실행');
}
function reject(){
	stage='reject';$('agency-title').textContent='납품 반려';
	body.innerHTML='<p>묶음 hash '+esc(data.hash.slice(0,12))+'</p><label for="agency-reason">반려 이유</label><textarea id="agency-reason" maxlength="1000" rows="4"></textarea>';
	buttons('취소','이전','반려하기');$('agency-reason').focus();
}
function close(){
	if(busy)return;
	dialog.close();data=null;uncertain=false;
	if(focus?.isConnected)focus.focus({preventScroll:true});else $('close-panel').focus({preventScroll:true});
}
async function read(){
	const response=await fetch('/api/agency?id='+encodeURIComponent(data.id)+'&view='+view,{cache:'no-store',headers:{'X-Office-Token':window.officeBridge.token()}});
	const value=await response.json();if(!response.ok)throw new Error(value.message||'자료를 읽을 수 없습니다.');
	return value;
}
async function open(id,kind,button){
	if(!window.officeBridge?.token()||document.querySelector('dialog[open]'))return;
	data={id};view=kind;stage='loading';busy=true;uncertain=false;focus=button;
	$('agency-title').textContent='대행사 자료 조회';body.textContent='';status.textContent='조회 중';buttons('닫기',null,null);dialog.showModal();
	try{data=await read();status.textContent='';if(view==='questions')questions();else delivery()}
	catch(error){status.textContent=error.message||'자료를 읽지 못했습니다.'}
	finally{busy=false;$('agency-close').disabled=false;$('agency-submit').disabled=false;$('agency-reject').disabled=false;$('agency-close').focus()}
}
function valid(text){return text.trim()&&Array.from(text).length<=1000&&!/[\u0000-\u001f\u007f-\u009f]/.test(text)}
async function send(payload){
	if(busy||uncertain)return null;
	busy=true;$('agency-close').disabled=true;$('agency-reject').disabled=true;$('agency-submit').disabled=true;
	body.querySelectorAll('button,textarea').forEach(el=>el.disabled=true);status.textContent='처리 중입니다. 결과를 기다려 주세요.';
	try{
		const response=await fetch('/api/action',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-Office-Token':window.officeBridge.token()},body:JSON.stringify(payload)});
		return await response.json();
	}catch{
		uncertain=true;status.textContent='결과를 받지 못했습니다. 창을 닫고 프로젝트 상태를 확인한 뒤 다시 시도하세요.';return null;
	}finally{
		busy=false;$('agency-close').disabled=false;$('agency-reject').disabled=false;$('agency-submit').disabled=uncertain;
		body.querySelectorAll('button,textarea').forEach(el=>el.disabled=uncertain);
	}
}
async function answer(no){
	const text=$('agency-answer-'+no)?.value||'';
	if(!valid(text)){status.textContent='답은 제어 문자 없이 1~1000자로 입력해 주세요.';return}
	const values={};for(const q of data.questions)values[q.no]=$('agency-answer-'+q.no).value;delete values[no];
	const result=await send({action:'agency_answer',id:data.id,no,text});if(!result)return;
	status.textContent=result.message;if(!result.ok)return;
	if(result.status&&result.status!=='접수 중'){
		body.textContent='프로젝트 상태: '+result.status;buttons('닫기',null,null);await window.officeBridge.refresh();return;
	}
	busy=true;$('agency-close').disabled=true;body.querySelectorAll('button,textarea').forEach(el=>el.disabled=true);
	try{data=await read();questions(values)}catch{
		uncertain=true;status.textContent='답변은 저장됐지만 목록을 다시 읽지 못했습니다. 다른 입력을 확인한 뒤 창을 다시 열어 주세요.';
	}finally{
		busy=false;$('agency-close').disabled=false;body.querySelectorAll('button').forEach(el=>el.disabled=uncertain);
		body.querySelectorAll('textarea').forEach(el=>el.disabled=false);
	}
	await window.officeBridge.refresh();
}
async function submit(){
	if(busy||uncertain)return;
	if(stage==='delivery'){status.textContent='';confirmation(1);return}
	if(stage==='confirm1'){confirmation(2);return}
	if(stage==='confirm2'||stage==='reject'){
		const rejecting=stage==='reject',text=rejecting?$('agency-reason').value:'';
		if(rejecting&&!valid(text)){status.textContent='반려 이유는 제어 문자 없이 1~1000자로 입력해 주세요.';return}
		const payload={action:rejecting?'agency_reject':'agency_approve',id:data.id,hash:data.hash};if(rejecting)payload.text=text;
		const result=await send(payload);if(!result)return;
		status.textContent=result.message;
		if(!result.ok&&!result.status)return;
		stage='result';$('agency-title').textContent=rejecting?'반려 결과':'납품 결과';
		body.innerHTML='<p>상태: '+esc(result.status||'확인 불가')+'</p>'+(result.reason?'<p>이유: '+esc(result.reason)+'</p>':'')+(rejecting||data.kind==='document'?'':'<p>push: '+(result.pushed?'완료':'실행되지 않음')+'</p><p>원격 대조: '+(result.remote_matches?'일치':'미확인 또는 불일치')+'</p>')+(result.restart_notice?'<p>재기동 안내: '+esc(result.restart_notice)+'</p>':'');
		buttons('닫기',null,null);await window.officeBridge.refresh();
	}
}
document.addEventListener('click',event=>{
	const button=event.target.closest('button');if(!button||button.disabled)return;
	if(button.dataset.agencyId)open(button.dataset.agencyId,button.dataset.agencyView,button);
	else if(button.dataset.agencySend)answer(Number(button.dataset.agencySend));
});
$('agency-close').onclick=close;$('agency-submit').onclick=submit;
$('agency-reject').onclick=()=>{if(busy)return;if(stage==='delivery')reject();else if(stage==='confirm2')confirmation(1);else delivery()};
dialog.addEventListener('cancel',event=>{event.preventDefault();close()});
})();
