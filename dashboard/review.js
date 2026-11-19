/* Screen capture starts only from the Record button; uploads stay on the local Relay server. */
const clipPage=document.querySelector('[data-review-project]');
const MAX_CLIP_BYTES=Number(clipPage.dataset.maxClipBytes);
const MAX_CAPTURE_SECONDS=Number(clipPage.dataset.maxClipSeconds);
const clipControllers=[];
let activeCapture=null;

function clipUrl(task,path){return '/api/artifact?'+new URLSearchParams({task,path})}
function clipFigure(task,item){
  const figure=document.createElement('figure'),video=document.createElement('video'),caption=document.createElement('figcaption'),link=document.createElement('a');
  video.controls=true;video.preload='metadata';video.src=clipUrl(task,item.path);
  link.href=video.src;link.textContent='Open or download clip';link.target='_blank';link.rel='noopener';
  const media=item.media||{},parts=[item.note||'Workflow clip','Attached to commit '+item.head.slice(0,8)];
  if(media.projectCommitAtStart)parts.push('Project commit at recording start: '+media.projectCommitAtStart.slice(0,8));
  if(media.taskChangedDuringCapture)parts.push('Task changed during capture: compare it with the current checks');
  caption.textContent=parts.join(' · ');figure.append(video,caption,link);return figure;
}

class ReviewClip{
  constructor(root){
    this.root=root;this.task=root.dataset.captureTask;
    ({revision:this.revision,build:this.build}=root.closest('.card').dataset);
    this.record=root.querySelector('[data-record]');this.stop=root.querySelector('[data-stop]');this.retry=root.querySelector('[data-retry]');
    this.file=root.querySelector('[data-clip-upload]');this.note=root.querySelector('[data-clip-note]');this.status=root.querySelector('[data-capture-status]');
    this.timerLabel=root.querySelector('[data-capture-timer]');
    this.download=root.querySelector('[data-download-clip]');this.clips=document.querySelector('[data-clips="'+this.task+'"]');
    this.record.onclick=()=>this.start();this.stop.onclick=()=>this.stopCapture();this.retry.onclick=()=>this.save();this.file.onchange=()=>this.upload();
    this.supported=!!(navigator.mediaDevices?.getDisplayMedia&&window.MediaRecorder);
    this.update();
    if(!this.supported)this.status.textContent='Screen recording is unavailable in this app view. Upload a WebM or MP4 clip instead.';
  }
  update(){
    this.record.disabled=!this.supported||this.busy||!!this.blob;
    this.file.disabled=!!this.busy||!!this.blob;
    this.stop.disabled=!this.recorder||this.recorder.state!=='recording';
    this.retry.hidden=!this.blob||!!this.busy;
    this.download.hidden=!this.blob;
  }
  async context(){
    const response=await fetch('/api/recordings/context?'+new URLSearchParams({task:this.task}));
    const value=await response.json();
    if(!response.ok)throw new Error(value.error||'Could not read the task context');
    if(value.projectId!==clipPage.dataset.reviewProject)throw new Error('Project changed. Reopen the review page before recording.');
    return value;
  }
  async start(){
    if(activeCapture){this.status.textContent='Stop the other recording before starting another.';return}
    if(this.build&&!await RelayDialog.ask({heading:'Confirm the recording’s build',detail:'Share a window running build '+this.build.slice(0,12)+'.',action:'Choose window',trigger:this.record}))return;
    activeCapture=this;this.busy=true;this.update();this.timerLabel.textContent='';this.status.textContent='Choose the window or app to show the reviewer.';
    try{
      this.stream=await navigator.mediaDevices.getDisplayMedia({video:{frameRate:15,width:{ideal:1280},height:{ideal:720}},audio:false});
      for(const track of this.stream.getAudioTracks()){track.stop();this.stream.removeTrack(track)}
      this.captureContext=await this.context();
      const mime=['video/webm;codecs=vp8','video/webm','video/mp4'].find(type=>MediaRecorder.isTypeSupported(type));
      if(!mime)throw new Error('This app view has no supported video recorder. Upload an existing clip instead.');
      this.recorder=new MediaRecorder(this.stream,{mimeType:mime,videoBitsPerSecond:2000000});
      this.parts=[];this.bytes=0;this.source='screen-recording';this.captureError='';this.startedAt=performance.now();
      this.recorder.ondataavailable=event=>{if(event.data.size){this.parts.push(event.data);this.bytes+=event.data.size}if(this.bytes>MAX_CLIP_BYTES*.95)this.stopCapture()};
      this.recorder.onerror=event=>{this.captureError=event.error?.message||'Recording failed';this.stopCapture()};
      this.recorder.onstop=()=>this.finished();
      for(const track of this.stream.getVideoTracks())track.addEventListener('ended',()=>this.stopCapture(),{once:true});
      this.recorder.start(1000);this.status.textContent='Recording what you chose to share. Stop when the workflow is shown.';this.update();this.tick();this.timer=setInterval(()=>this.tick(),1000);
    }catch(error){
      this.release();this.busy=false;activeCapture=null;this.update();
      this.status.textContent=error.name==='NotAllowedError'?'Sharing was cancelled or denied. Try again or upload an existing clip.':error.message;
    }
  }
  tick(){
    const seconds=Math.floor((performance.now()-this.startedAt)/1000);
    this.timerLabel.textContent=seconds+'s / '+MAX_CAPTURE_SECONDS+'s · '+(this.bytes/1024/1024).toFixed(1)+' MB · no audio';
    if(seconds>=MAX_CAPTURE_SECONDS)this.stopCapture();
  }
  stopCapture(){if(this.recorder?.state==='recording'){this.stop.disabled=true;this.recorder.stop()}}
  release(){clearInterval(this.timer);for(const track of this.stream?.getTracks()||[])track.stop();this.stream=null}
  async finished(){
    this.duration=(performance.now()-this.startedAt)/1000;
    this.timerLabel.textContent='Captured about '+Math.round(this.duration)+'s';
    const blob=new Blob(this.parts,{type:this.recorder.mimeType});this.parts=[];this.recorder=null;
    this.release();this.busy=false;if(activeCapture===this)activeCapture=null;
    if(!blob.size){this.status.textContent='No video was captured. Record again or upload a clip.';this.update();return}
    this.keepDraft(blob);
    if(this.captureError){this.status.textContent=this.captureError+'. A partial clip is available to download or save.';return}
    await this.save();
  }
  keepDraft(blob){
    this.blob=blob;this.draftUrl=URL.createObjectURL(blob);this.download.href=this.draftUrl;
    this.download.download=this.task+'-workflow.'+(blob.type.startsWith('video/mp4')?'mp4':'webm');this.update();
  }
  async upload(){
    const file=this.file.files[0];if(!file)return;
    if(this.blob||this.busy){this.file.value='';return}
    if(file.size>MAX_CLIP_BYTES||!file.size){this.status.textContent='Choose a nonempty clip of 45 MB or less.';this.file.value='';return}
    const mime=file.type.split(';')[0]||(/\.mp4$/i.test(file.name)?'video/mp4':/\.webm$/i.test(file.name)?'video/webm':'');
    if(!['video/webm','video/mp4'].includes(mime)){this.status.textContent='Choose a WebM or MP4 video.';this.file.value='';return}
    if(this.build&&!await RelayDialog.ask({heading:'Confirm the clip’s build',detail:'This clip should show build '+this.build.slice(0,12)+'.',action:'Upload clip',trigger:this.file})){this.file.value='';return}
    this.busy=true;this.timerLabel.textContent='';this.update();
    try{
      this.captureContext=await this.context();this.duration=null;this.source='upload';
      this.keepDraft(file.type?file:new Blob([file],{type:mime}));this.busy=false;await this.save();
    }catch(error){this.busy=false;if(!this.blob)this.file.value='';this.update();this.status.textContent=error.message}
  }
  async save(){
    if(!this.blob||this.busy)return;
    if(this.blob.size>MAX_CLIP_BYTES){this.status.textContent='Clip exceeds 45 MB. Download it and trim it before uploading.';return}
    this.busy=true;this.update();this.status.textContent='Saving clip in this project…';
    try{
      const headers={'Content-Type':this.blob.type.split(';')[0],'X-Review-Note':encodeURIComponent(this.note.value.trim()),
        'X-Clip-Project':this.captureContext.projectId,'X-Clip-Source':this.source};
      if(this.revision){headers['X-Review-Revision']=this.revision;headers['X-Review-Build']=this.build}
      if(this.source==='screen-recording'){
        headers['X-Clip-Commit']=this.captureContext.head;headers['X-Clip-Task-Hash']=this.captureContext.taskHash;headers['X-Clip-Duration']=String(this.duration);
      }
      const response=await fetch('/api/recordings/save?'+new URLSearchParams({task:this.task}),{method:'POST',headers,body:this.blob});
      const result=await response.json();if(!response.ok||!result.ok)throw new Error(result.error||'Could not save clip');
      this.clips.append(clipFigure(this.task,result.artifact));URL.revokeObjectURL(this.draftUrl);this.blob=null;this.file.value='';
      this.root.closest('.card').querySelector('[data-playthrough]')?.remove();
      this.status.textContent='Clip attached. Approval remains a separate action.';
    }catch(error){this.status.textContent='Clip not saved: '+error.message+'. Retry or download the unsaved clip before leaving.'}
    finally{this.busy=false;this.update()}
  }
}

document.querySelectorAll('[data-capture-task]').forEach(root=>clipControllers.push(new ReviewClip(root)));
window.addEventListener('beforeunload',event=>{
  if(activeCapture||clipControllers.some(controller=>controller.blob||controller.busy)){event.preventDefault();event.returnValue=''}
});
