// Disposable diagnostic composition; not the packaged production provider.
import { appendFileSync, statSync } from 'node:fs';
import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';
import { dispatchGatewayMethod } from 'openclaw/plugin-sdk/gateway-method-runtime';
import { createRelayMindProvider } from '../../dist/provider.js';
import { createPersonProbe } from '../host-person-qualification/probe.mjs';
import { createRetainedBoundaryProbe, requireLibrarySuccess } from './probe.mjs';
export default definePluginEntry({id:'relaymind-retained-probe',name:'Disposable retained boundary probe',register(api){
 const record = row => {const line=JSON.stringify({...row,monotonic_ns:process.hrtime.bigint().toString()})+'\n';if(Buffer.byteLength(line)>16384||statSync(api.pluginConfig.eventFile).size+Buffer.byteLength(line)>1048576)throw Error('capture_bound');appendFileSync(api.pluginConfig.eventFile,line);};
 const held=new Map();
 api.registerGatewayMethod('rmqualification.retained',async options=>{
   const handler=createPersonProbe({origin:api.pluginConfig.origin,record,resolveWorkspace:(_options,agentId)=>api.runtime.agent.resolveAgentWorkspaceDir(options.context.getRuntimeConfig(),agentId)});
   await handler({...options,respond:(ok,payload,error)=>{
     if(ok){const id=options.sessionAccessAuthority.target.sessionId;held.get(id)?.probe.close();const probe=createRetainedBoundaryProbe({access:options.sessionAccessAuthority,record,maximumMs:60000});held.set(id,{probe,target:{...options.sessionAccessAuthority.target},calls:0});}
     options.respond(ok,payload,error);
   }});
 },{scope:'operator.write',profileAccess:'required',sessionAccess:{mode:'write'}});
 const provider=createRelayMindProvider({pluginConfig:api.pluginConfig.providerConfig,logger:api.logger});
 const originalResolve=provider.resolveDynamicModel;
 const originalWrap=provider.wrapStreamFn;
 api.registerProvider({...provider,id:'relaymind-retained-probe',resolveDynamicModel:ctx=>{const model=originalResolve({...ctx,provider:'relaymind'});return model?{...model,provider:'relaymind-retained-probe'}:undefined;},wrapStreamFn:ctx=>{
   const stream=originalWrap(ctx);
   return (model,context,options)=>stream(model,context,{...options,onPayload:async(payload,payloadModel)=>{
     const replaced=await options?.onPayload?.(payload,payloadModel);
     const item=held.get(options?.sessionId);
     record({phase:'provider_lookup',matched:!!item,sessionId:options?.sessionId??null});
     if(!item)throw Error('retained_probe_missing');
     await item.probe.onPayload(replaced??payload,options.sessionId);
     try {await item.probe.observeLibrary(async()=>{let result;try{result=await dispatchGatewayMethod('skills.library.list',{sessionKey:item.target.sessionKey},{timeoutMs:3000});}catch(error){record({phase:'library_dispatch_throw',reason:error instanceof Error && error.message.startsWith('Gateway method dispatch is reserved for authenticated plugin HTTP routes or RPC handlers')?'authenticated_request_scope_unavailable':'dispatch_failed'});throw Error('library_dispatch_failed');}record({phase:'library_dispatch_result',ok:result.ok===true});return requireLibrarySuccess(result);});}
     catch {record({phase:'library_dispatch_refused'});throw Error('retained_probe_library_unavailable');}
     if(++item.calls>1){record({phase:'forced_payload_stop'});item.probe.close();throw Error('retained_probe_forced_stop');}
     return replaced;
   }});
 }});
}});
