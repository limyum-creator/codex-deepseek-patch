"""Small Responses <-> OpenAI-compatible Chat Completions wire adapter.

ToolAdapter must flatten namespace/custom/discovery tools before this adapter;
apply its response/events transforms afterwards. No tools run here. This is
a protocol adapter and never guesses capabilities from a model.
"""
from __future__ import annotations

import copy
import json
import time
import uuid


MAX_FRAME = 8 * 1024 * 1024
DONE = object()


class ChatCompatibilityError(ValueError):
    def __init__(self, code, message, status=400):
        self.code, self.status = code, status
        super().__init__(message)


def _require(ok, message, code='unsupported_chat_input', status=400):
    if not ok:
        raise ChatCompatibilityError(code, message, status)


def _content(value, role):
    if isinstance(value, str):
        return value
    _require(isinstance(value, list), 'Chat message content must be text or supported content parts')
    result = []
    for part in value:
        _require(isinstance(part, dict), 'Invalid chat content part')
        kind = part.get('type')
        if kind in ('input_text', 'output_text', 'text'):
            _require(isinstance(part.get('text'), str), 'Invalid text content')
            result.append({'type':'text', 'text':part['text']})
        elif kind in ('input_image', 'image_url'):
            _require(role == 'user', 'This Chat protocol supports image content only in user messages')
            image = part.get('image_url')
            image = copy.deepcopy(image) if isinstance(image, dict) else {'url':image}
            _require(isinstance(image.get('url'), str) and image['url'],
                     'Image file IDs require an explicit upload capability; use an image URL or data URL')
            if part.get('detail') is not None: image['detail'] = part['detail']
            result.append({'type':'image_url', 'image_url':image})
        elif kind == 'refusal' and role == 'assistant':
            _require(isinstance(part.get('refusal'), str), 'Invalid historical refusal')
            result.append({'type':'text', 'text':part['refusal']})
        else:
            raise ChatCompatibilityError('unsupported_chat_content',
                'This Chat route supports text and user image URLs; file, audio and other content need a declared adapter')
    # Text-only messages use the most widely supported string form. Mixed
    # text/image arrays retain exact order and image/data URLs.
    return ''.join(v['text'] for v in result) if all(v['type']=='text' for v in result) else result


def _messages(body, *, reasoning_decoder=None):
    result, pending, completed = [], set(), set()
    instructions = body.get('instructions')
    if instructions:
        _require(isinstance(instructions, str), 'Instructions must be text')
        result.append({'role':'system', 'content':instructions})
    source = body.get('input', [])
    if isinstance(source, str): source = [{'role':'user', 'content':source}]
    _require(isinstance(source, list), 'Responses input must contain full message history')
    for item in source:
        _require(isinstance(item, dict), 'Invalid Responses history item')
        kind = item.get('type', 'message' if item.get('role') else None)
        if kind in ('message', 'reasoning'):
            if kind == 'reasoning':
                if reasoning_decoder and item.get('encrypted_content'):
                    _require(not pending,'Reasoning cannot interrupt tool results')
                    result.append({'role':'assistant','content':None,'anthropic_thinking':reasoning_decoder(item['encrypted_content'])})
                    continue
                _require(not item.get('encrypted_content'), 'Private reasoning must be resolved by its original-provider adapter')
                parts = item.get('summary', [])
                _require(isinstance(parts, list), 'Invalid public reasoning summary')
                _require(all(isinstance(p,dict) and p.get('type')=='summary_text'
                             and isinstance(p.get('text'),str) for p in parts), 'Unsupported public reasoning content')
                text = '\n'.join(p['text'] for p in parts)
                if not text:
                    content = item.get('content')
                    if content: text = _content(content, 'assistant')
                _require(bool(text), 'An empty private reasoning item has no Chat-compatible representation')
                role, content = 'assistant', text
            else:
                role = item.get('role')
                _require(role in ('system','developer','user','assistant'), 'Unsupported historical message role')
                content = _content(item.get('content', ''), role)
            _require(not pending, 'Tool results must complete before another historical message')
            result.append({'role':role, 'content':content})
        elif kind == 'function_call':
            _require(not item.get('namespace'), 'Flatten namespaced tools with ToolAdapter before Chat conversion')
            identifier, name, arguments = item.get('call_id'), item.get('name'), item.get('arguments')
            _require(isinstance(identifier,str) and identifier and identifier not in pending|completed,
                     'Historical tool calls require unique call IDs')
            _require(isinstance(name,str) and name and isinstance(arguments,str), 'Invalid historical function call')
            _require(not result or result[-1]['role']!='tool' or not pending,
                     'A new tool call cannot interrupt pending tool results')
            if not result or result[-1]['role']!='assistant':
                result.append({'role':'assistant','content':None,'tool_calls':[]})
            result[-1].setdefault('tool_calls', []).append({'id':identifier,'type':'function',
                'function':{'name':name,'arguments':arguments}})
            pending.add(identifier)
        elif kind == 'function_call_output':
            identifier = item.get('call_id')
            _require(identifier in pending, 'Tool output has no matching pending call ID')
            result.append({'role':'tool','tool_call_id':identifier,'content':_content(item.get('output',''),'tool')})
            pending.remove(identifier); completed.add(identifier)
        else:
            raise ChatCompatibilityError('unsupported_chat_history',
                'Unconverted Responses history requires a tool, compaction or media adapter before Chat conversion')
    _require(not pending, 'Historical tool calls are missing their results')
    return result


def _records(stream):
    """Accept decoded chat chunk dicts or SSE byte chunks/lines, bounded per frame."""
    buffer, data = b'', []
    for chunk in stream:
        if isinstance(chunk, dict):
            _require(not buffer and not data, 'Mixed decoded and SSE upstream stream', 'invalid_upstream_response', 502)
            yield chunk
            continue
        _require(isinstance(chunk, (bytes, str)), 'Invalid upstream stream chunk', 'invalid_upstream_response', 502)
        buffer += chunk.encode() if isinstance(chunk,str) else chunk
        _require(len(buffer)+sum(map(len,data))<=MAX_FRAME, 'Upstream event is too large','event_too_large',502)
        while b'\n' in buffer:
            line,buffer=buffer.split(b'\n',1);line=line.rstrip(b'\r')
            if line:
                if line.startswith(b'data:'):data.append(line[5:].lstrip(b' '))
            elif data:
                raw=b'\n'.join(data);data=[]
                if raw==b'[DONE]':yield DONE
                else:
                    value=json.loads(raw)
                    _require(isinstance(value,dict),'Invalid upstream JSON event','invalid_upstream_response',502)
                    yield value
    _require(not buffer and not data,'Upstream ended inside an SSE frame','truncated_stream',502)


def _usage(value):
    if not isinstance(value,dict):return None
    def count(mapping,key):
        _require(isinstance(mapping,dict),'Invalid upstream token usage','invalid_upstream_response',502)
        value=mapping.get(key,0)
        _require(type(value) is int and value>=0,'Invalid upstream token usage','invalid_upstream_response',502)
        return value
    prompt,completion=count(value,'prompt_tokens'),count(value,'completion_tokens')
    return {'input_tokens':prompt,'output_tokens':completion,
            'total_tokens':count(value,'total_tokens') if 'total_tokens' in value else prompt+completion,
            'input_tokens_details':{'cached_tokens':count(value.get('prompt_tokens_details') or {},'cached_tokens')},
            'output_tokens_details':{'reasoning_tokens':count(value.get('completion_tokens_details') or {},'reasoning_tokens')}}


class ResponsesToChat:
    def __init__(self, body, *, supports_reasoning_effort=False):
        _require(isinstance(body,dict) and isinstance(body.get('model'),str), 'A model and Responses request are required')
        _require(not body.get('previous_response_id'), 'Chat requests require complete history, not previous_response_id')
        self.model=body['model']
        self.reasoning_source={'protocol':'chat_completions','model':self.model}
        result={'model':self.model,'messages':_messages(body),'stream':bool(body.get('stream',False))}
        for key in ('temperature','top_p','parallel_tool_calls','service_tier','metadata','store'):
            if key in body:result[key]=copy.deepcopy(body[key])
        if 'max_output_tokens' in body:result['max_completion_tokens']=body['max_output_tokens']
        if supports_reasoning_effort and body.get('reasoning',{}).get('effort') is not None:
            result['reasoning_effort']=body['reasoning']['effort']
        if result['stream']:result['stream_options']={'include_usage':True}
        if 'tools' in body:
            _require(isinstance(body['tools'],list),'Chat tools must be a list')
            tools=[]
            for tool in body['tools']:
                _require(isinstance(tool,dict) and tool.get('type')=='function' and not tool.get('namespace')
                         and isinstance(tool.get('name'),str) and tool['name'],
                         'Convert namespace/custom/search tools with ToolAdapter before Chat conversion')
                tools.append({'type':'function','function':{k:copy.deepcopy(tool[k])
                    for k in ('name','description','parameters','strict') if k in tool}})
            result['tools']=tools
        choice=body.get('tool_choice')
        if choice is not None:
            if isinstance(choice,str):
                _require(choice in ('auto','none','required'),'Unsupported Chat tool choice')
                result['tool_choice']=choice
            else:
                _require(isinstance(choice,dict) and choice.get('type')=='function'
                         and not choice.get('namespace') and isinstance(choice.get('name'),str),'Unsupported Chat tool choice')
                result['tool_choice']={'type':'function','function':{'name':choice['name']}}
        if isinstance(body.get('text'),dict) and body['text'].get('format'):
            form=copy.deepcopy(body['text']['format']);kind=form.get('type')
            _require(kind in ('text','json_object','json_schema'),'Unsupported Chat text format')
            if kind=='json_object':result['response_format']={'type':kind}
            elif kind=='json_schema':result['response_format']={'type':kind,'json_schema':{k:v for k,v in form.items() if k!='type'}}
        self.body=result

    def response(self, value):
        _require(isinstance(value,dict),'Invalid Chat completion','invalid_upstream_response',502)
        if value.get('error'):
            records=[value]
        else:
            choices=value.get('choices')
            _require(isinstance(choices,list) and len(choices)==1,'Exactly one Chat completion is required','invalid_upstream_response',502)
            choice=choices[0]
            _require(isinstance(choice,dict) and isinstance(choice.get('message'),dict),
                     'Invalid Chat completion message','invalid_upstream_response',502)
            message=copy.deepcopy(choice['message'])
            if message.get('tool_calls'):
                message['tool_calls']=[{**call,'index':i} for i,call in enumerate(message['tool_calls'])]
            records=[{**value,'choices':[{'index':0,'delta':message,'finish_reason':choice.get('finish_reason')}]}]
        final=None
        for event in self.events(records):
            if event['type'] in ('response.completed','response.incomplete','response.failed'):final=event['response']
        return final

    def stream(self, upstream):
        for event in self.events(upstream):
            yield ('event: '+event['type']+'\ndata: '+json.dumps(event,ensure_ascii=False,separators=(',',':'))+'\n\n').encode()

    def events(self, upstream):
        state=_Completion(self.model)
        sequence=0
        try:
            for record in _records(upstream):
                if record is DONE:
                    _require(state.finish is not None,'Chat stream ended without a finish reason','truncated_stream',502)
                    state.ended=True
                    break
                for event in state.consume(record):
                    yield {**event,'sequence_number':sequence};sequence+=1
            _require(state.finish is not None,'Chat stream ended before completion','truncated_stream',502)
            for event in state.complete():
                yield {**event,'sequence_number':sequence};sequence+=1
        except (ChatCompatibilityError,ValueError,TypeError,KeyError,UnicodeDecodeError) as error:
            if not state.started:
                yield {'type':'response.created','response':state.response('in_progress'),'sequence_number':sequence};sequence+=1
            response=state.response('failed')
            response['error']={'code':getattr(error,'code','invalid_upstream_response'),
                               'message':str(error) if isinstance(error,ChatCompatibilityError) else 'Invalid Chat completion stream'}
            yield {'type':'response.failed','response':response,'sequence_number':sequence}


class _Completion:
    def __init__(self,model):
        self.model=model;self.id='resp_'+uuid.uuid4().hex;self.created=int(time.time())
        self.items=[];self.text=None;self.reasoning=None;self.refusal=None;self.tools={}
        self.finish=None;self.usage=None;self.started=False;self.ended=False

    def response(self,status):
        return {'id':self.id,'object':'response','created_at':self.created,'model':self.model,
                'status':status,'output':copy.deepcopy(self.items),'usage':self.usage,
                'error':None,'incomplete_details':None}

    def add_text(self,delta,kind):
        _require(isinstance(delta,str),'Unsupported upstream content modality','invalid_upstream_response',502)
        attr='reasoning' if kind=='reasoning' else 'refusal' if kind=='refusal' else 'text'
        index=getattr(self,attr)
        if index is None:
            index=len(self.items);setattr(self,attr,index)
            if kind=='reasoning':
                item={'id':'rs_'+uuid.uuid4().hex,'type':'reasoning','summary':[],'status':'in_progress'}
                part={'type':'summary_text','text':''};field='summary'
            else:
                item={'id':'msg_'+uuid.uuid4().hex,'type':'message','role':'assistant','content':[],'status':'in_progress'}
                part={'type':'refusal','refusal':''} if kind=='refusal' else {'type':'output_text','text':'','annotations':[]}
                field='content'
            self.items.append(item)
            yield {'type':'response.output_item.added','output_index':index,'item':copy.deepcopy(item)}
            item[field].append(part)
            yield {'type':'response.reasoning_summary_part.added' if kind=='reasoning' else 'response.content_part.added',
                   'item_id':item['id'],'output_index':index, 'summary_index' if kind=='reasoning' else 'content_index':0,
                   'part':copy.deepcopy(part)}
        item=self.items[index];part=item['summary' if kind=='reasoning' else 'content'][0]
        part['refusal' if kind=='refusal' else 'text']+=delta
        yield {'type':'response.reasoning_summary_text.delta' if kind=='reasoning' else
                     'response.refusal.delta' if kind=='refusal' else 'response.output_text.delta',
               'item_id':item['id'],'output_index':index,'summary_index' if kind=='reasoning' else 'content_index':0,'delta':delta}

    def consume(self,chunk):
        if chunk.get('error'):
            error=chunk['error'];message=error.get('message','Chat upstream error') if isinstance(error,dict) else str(error)
            raise ChatCompatibilityError(str(error.get('code') or 'upstream_error') if isinstance(error,dict) else 'upstream_error',message,502)
        if not self.started:
            if isinstance(chunk.get('created'),int):self.created=chunk['created']
            if isinstance(chunk.get('model'),str):self.model=chunk['model']
            self.started=True
            yield {'type':'response.created','response':self.response('in_progress')}
        if chunk.get('usage') is not None:self.usage=_usage(chunk['usage'])
        choices=chunk.get('choices',[])
        _require(isinstance(choices,list) and len(choices)<=1,'Multiple Chat choices are unsupported','invalid_upstream_response',502)
        if not choices:return
        choice=choices[0]
        _require(isinstance(choice,dict),'Invalid Chat choice','invalid_upstream_response',502)
        _require(choice.get('index',0)==0,'Unexpected Chat choice index','invalid_upstream_response',502)
        delta=choice.get('delta') or {}
        _require(isinstance(delta,dict),'Invalid Chat delta','invalid_upstream_response',502)
        _require(delta.get('role') in (None,'assistant'),'Invalid Chat output role','invalid_upstream_response',502)
        _require(not any(delta.get(k) for k in ('audio','images','image')),
                 'This Chat route cannot convert output audio or images','unsupported_chat_output',502)
        _require(self.finish is None or not any(delta.get(k) for k in ('content','reasoning_content','tool_calls','refusal')),
                 'Chat content arrived after its finish reason','invalid_upstream_response',502)
        for field,kind in (('reasoning_content','reasoning'),('content','text'),('refusal','refusal')):
            if delta.get(field) is not None and delta[field]!='':yield from self.add_text(delta[field],kind)
        _require(not delta.get('function_call'),'Legacy Chat function_call requires an explicit compatibility capability','invalid_upstream_response',502)
        calls=delta.get('tool_calls') or []
        _require(isinstance(calls,list),'Invalid Chat tool call list','invalid_upstream_response',502)
        for call in calls:
            _require(isinstance(call,dict),'Invalid streamed tool call','invalid_upstream_response',502)
            index=call.get('index')
            _require(type(index) is int and index>=0,'Invalid streamed tool index','invalid_upstream_response',502)
            if index not in self.tools:
                item={'id':'fc_'+uuid.uuid4().hex,'type':'function_call','call_id':'','name':'','arguments':'','status':'in_progress'}
                self.tools[index]={'item':item,'index':len(self.items),'fragments':[]}
                self.items.append(item)
            tool=self.tools[index];item=tool['item'];function=call.get('function') or {}
            _require(isinstance(function,dict),'Invalid streamed function','invalid_upstream_response',502)
            _require(call.get('type','function')=='function','Unsupported Chat tool kind','invalid_upstream_response',502)
            if call.get('id'):
                _require(isinstance(call['id'],str),'Invalid Chat tool ID','invalid_upstream_response',502)
                _require(not item['call_id'] or item['call_id']==call['id'],'Chat tool ID changed','invalid_upstream_response',502)
                item['call_id']=call['id']
            if function.get('name'):
                _require(isinstance(function['name'],str) and (not item['name'] or item['name']==function['name']),
                         'Chat function name changed','invalid_upstream_response',502)
                item['name']=function['name']
            if function.get('arguments') is not None:
                _require(isinstance(function['arguments'],str),'Invalid function arguments fragment','invalid_upstream_response',502)
                item['arguments']+=function['arguments'];tool['fragments'].append(function['arguments'])
        reason=choice.get('finish_reason')
        if reason is not None:
            _require(reason in ('stop','tool_calls','length','content_filter'),'Unknown Chat finish reason','invalid_upstream_response',502)
            _require(self.finish is None or self.finish==reason,'Conflicting Chat finish reasons','invalid_upstream_response',502)
            self.finish=reason

    def complete(self):
        _require(self.finish!='tool_calls' or self.tools,'Tool finish has no function calls','invalid_upstream_response',502)
        if self.tools:
            _require(sorted(self.tools)==list(range(len(self.tools))),
                     'Chat tool indices are incomplete','truncated_stream',502)
            _require(len({tool['item']['call_id'] for tool in self.tools.values()})==len(self.tools),
                     'Chat tool IDs are duplicated','invalid_upstream_response',502)
            # Tool indices define their array order even when fragments arrive
            # out of order. They have not been emitted before this finalization.
            slots=sorted(tool['index'] for tool in self.tools.values())
            for slot,key in zip(slots,sorted(self.tools)):
                self.tools[key]['index']=slot;self.items[slot]=self.tools[key]['item']
        status='incomplete' if self.finish in ('length','content_filter') else 'completed'
        for index,item in enumerate(self.items):
            if item['type']=='function_call':
                _require(item['call_id'] and item['name'],'Incomplete streamed function identity','truncated_stream',502)
                item['status']='in_progress'
                added={**item,'arguments':''}
                yield {'type':'response.output_item.added','output_index':index,'item':copy.deepcopy(added)}
                tool=next(v for v in self.tools.values() if v['index']==index)
                for fragment in tool['fragments']:
                    yield {'type':'response.function_call_arguments.delta','item_id':item['id'],'output_index':index,'delta':fragment}
                yield {'type':'response.function_call_arguments.done','item_id':item['id'],'output_index':index,
                       'arguments':item['arguments'],'name':item['name']}
            else:
                reasoning=item['type']=='reasoning';part=item['summary' if reasoning else 'content'][0]
                refusal=part['type']=='refusal'
                yield {'type':'response.reasoning_summary_text.done' if reasoning else
                             'response.refusal.done' if refusal else 'response.output_text.done',
                       'item_id':item['id'],'output_index':index,'summary_index' if reasoning else 'content_index':0,
                       'refusal' if refusal else 'text':part['refusal' if refusal else 'text']}
                yield {'type':'response.reasoning_summary_part.done' if reasoning else 'response.content_part.done',
                       'item_id':item['id'],'output_index':index,'summary_index' if reasoning else 'content_index':0,
                       'part':copy.deepcopy(part)}
            item['status']='completed' if status=='completed' else 'incomplete'
            yield {'type':'response.output_item.done','output_index':index,'item':copy.deepcopy(item)}
        response=self.response(status)
        if status=='incomplete':response['incomplete_details']={'reason':'max_output_tokens' if self.finish=='length' else 'content_filter'}
        yield {'type':'response.incomplete' if status=='incomplete' else 'response.completed','response':response}


ANTHROPIC_THINKING = 'codex-anthropic-thinking:v1:'


def _thinking_blocks(value):
    import base64
    _require(isinstance(value,str) and value.startswith(ANTHROPIC_THINKING),
             'Private reasoning belongs to a different protocol')
    try:blocks=json.loads(base64.b64decode(value[len(ANTHROPIC_THINKING):],validate=True))
    except (ValueError,TypeError):raise ChatCompatibilityError('invalid_reasoning','Invalid Anthropic thinking record') from None
    _require(isinstance(blocks,list) and all(isinstance(b,dict) and
        (set(b)=={'type','thinking','signature'} and b['type']=='thinking' and isinstance(b['thinking'],str) and isinstance(b['signature'],str)
         or set(b)=={'type','data'} and b['type']=='redacted_thinking' and isinstance(b['data'],str)) for b in blocks),
        'Invalid Anthropic thinking blocks')
    return blocks


def _anthropic_content(content):
    import re
    if content is None:return []
    if isinstance(content,str):return [{'type':'text','text':content}] if content else []
    result=[]
    for part in content:
        if part['type']=='text':result.append(copy.deepcopy(part))
        else:
            url=part['image_url']['url']
            if url.startswith('data:'):
                match=re.fullmatch(r'data:(image/(?:jpeg|png|gif|webp));base64,([A-Za-z0-9+/=\s]+)',url)
                _require(match,'Anthropic images require a supported base64 image or HTTP(S) URL')
                source={'type':'base64','media_type':match[1],'data':match[2]}
            else:
                _require(url.startswith(('https://','http://')),'Unsupported Anthropic image URL')
                source={'type':'url','url':url}
            result.append({'type':'image','source':source})
    return result


class ResponsesToAnthropic:
    """Messages protocol, explicit capabilities, preserving same-model thinking."""
    def __init__(self,body,*,max_output_tokens=None,reasoning_levels=(),thinking_mode=None):
        _require(isinstance(body,dict) and isinstance(body.get('model'),str),'A model is required')
        _require(not body.get('previous_response_id'),'Messages requires complete history')
        maximum=body.get('max_output_tokens',max_output_tokens)
        _require(type(maximum) is int and maximum>0,
                 'Anthropic requires max_output_tokens from service metadata or an explicit model specification','missing_model_spec')
        if max_output_tokens is not None:
            _require(type(max_output_tokens) is int and max_output_tokens>0 and maximum<=max_output_tokens,'Output exceeds the declared model maximum')
        self.model=body['model'];self.reasoning_source={'protocol':'anthropic_messages','model':self.model}
        result={'model':self.model,'stream':bool(body.get('stream',False)),'max_tokens':maximum,'messages':[]}
        system=[]
        for row in _messages(body,reasoning_decoder=_thinking_blocks):
            blocks=copy.deepcopy(row.get('anthropic_thinking',[]))+_anthropic_content(row.get('content'))
            role=row['role']
            if role in ('system','developer'):
                system.extend(blocks);continue
            if role=='tool':
                role='user';blocks=[{'type':'tool_result','tool_use_id':row['tool_call_id'],'content':blocks}]
            for call in row.get('tool_calls',[]):
                try:arguments=json.loads(call['function']['arguments'])
                except ValueError:raise ChatCompatibilityError('invalid_tool_arguments','Historical tool arguments must be JSON') from None
                _require(isinstance(arguments,dict),'Historical tool arguments must be an object')
                blocks.append({'type':'tool_use','id':call['id'],'name':call['function']['name'],'input':arguments})
            if result['messages'] and result['messages'][-1]['role']==role:result['messages'][-1]['content'].extend(blocks)
            elif blocks:result['messages'].append({'role':role,'content':blocks})
        if system:result['system']=system
        _require(result['messages'],'Messages history must not be empty')
        for field in ('temperature','top_p'):
            if field in body:result[field]=copy.deepcopy(body[field])
        # Reuse the function/choice validation, not the Chat wire body.
        plain=copy.deepcopy(body);plain['input']='validation';plain.pop('instructions',None)
        chat=ResponsesToChat(plain).body
        if 'tools' in chat:
            result['tools']=[{'name':t['function']['name'],'input_schema':t['function'].get('parameters',{'type':'object','properties':{}}),
                **({'description':t['function']['description']} if 'description' in t['function'] else {})} for t in chat['tools']]
        choice=chat.get('tool_choice')
        if choice is not None:
            result['tool_choice']=({'type':{'required':'any','auto':'auto','none':'none'}[choice]} if isinstance(choice,str)
                                   else {'type':'tool','name':choice['function']['name']})
        if 'parallel_tool_calls' in body:
            result.setdefault('tool_choice',{'type':'auto'})['disable_parallel_tool_use']=not body['parallel_tool_calls']
        form=body.get('text',{}).get('format',{})
        if form.get('type') not in (None,'text'):
            _require(form.get('type')=='json_schema','Anthropic requires a JSON schema for structured output')
            result['output_config']={'format':{'type':'json_schema','schema':copy.deepcopy(form['schema'])}}
        effort=body.get('reasoning',{}).get('effort')
        if effort is not None and reasoning_levels:
            _require(effort in reasoning_levels,'Unsupported effort for this Anthropic model')
            result.setdefault('output_config',{})['effort']=effort
        if thinking_mode is not None:
            _require(thinking_mode in ('adaptive','disabled'),'Unsupported Anthropic thinking configuration')
            result['thinking']={'type':thinking_mode}
        self.body=result

    def response(self,value):
        _require(isinstance(value,dict) and value.get('type')=='message','Invalid Anthropic message','invalid_upstream_response',502)
        records=[{'type':'message_start','message':{**value,'content':[],'stop_reason':None}}]
        for index,block in enumerate(value.get('content',[])):
            records.extend([{'type':'content_block_start','index':index,'content_block':block},
                            {'type':'content_block_stop','index':index}])
        records.extend([{'type':'message_delta','delta':{'stop_reason':value.get('stop_reason')},'usage':value.get('usage',{})},
                        {'type':'message_stop'}])
        return list(self.events(records))[-1]['response']

    def events(self,upstream):
        import base64
        state=_Completion(self.model);blocks={};stopped=set();usage={};finish=None;ended=False;thinking=[];tool_count=0
        sequence=0;thinking_payload=None
        def event(value):
            nonlocal sequence
            value=copy.deepcopy(value)
            cipher=thinking_payload
            candidates=[value.get('item',{})]+value.get('response',{}).get('output',[])
            for item in candidates:
                if item.get('type')=='reasoning' and cipher:item['encrypted_content']=cipher
            value['sequence_number']=sequence;sequence+=1
            return value
        def chunk(delta=None,reason=None):
            return {'model':self.model,'choices':[{'index':0,'delta':delta or {},'finish_reason':reason}]}
        try:
            for record in _records(upstream):
                _require(record is not DONE,'Unexpected Messages terminator','invalid_upstream_response',502)
                kind=record.get('type')
                _require(not ended,'Messages data after message_stop','invalid_upstream_response',502)
                if kind=='error':
                    error=record.get('error',{})
                    raise ChatCompatibilityError(str(error.get('type','upstream_error')),str(error.get('message','Messages error')),502)
                if kind=='ping':continue
                if kind=='message_start':
                    _require(not state.started,'Duplicate Messages start','invalid_upstream_response',502)
                    message=record['message'];usage.update(message.get('usage',{}))
                    for e in state.consume(chunk()):yield event(e)
                    continue
                _require(state.started,'Messages event before start','invalid_upstream_response',502)
                delta={}
                if kind=='content_block_start':
                    index=record['index'];block=copy.deepcopy(record['content_block'])
                    _require(type(index) is int and index==len(blocks) and not (set(blocks)-stopped),'Invalid Messages block order','invalid_upstream_response',502)
                    blocks[index]=block
                    if block['type']=='text':delta={'content':block['text']}
                    elif block['type'] in ('thinking','redacted_thinking'):
                        thinking.append(block)
                        if state.reasoning is None:
                            for e in state.add_text('', 'reasoning'):yield event(e)
                        if block['type']=='thinking':delta={'reasoning_content':block.get('thinking','')}
                    elif block['type']=='tool_use':
                        block['_tool_index']=tool_count;tool_count+=1
                        block['_arguments']='';block['_input']=block.get('input',{})
                        delta={'tool_calls':[{'index':block['_tool_index'],'id':block['id'],'type':'function','function':{'name':block['name'],'arguments':''}}]}
                    else:raise ChatCompatibilityError('unsupported_anthropic_output','Unsupported Messages block: '+str(block['type']),502)
                elif kind=='content_block_delta':
                    index=record['index'];_require(index in blocks and index not in stopped,'Delta outside an open block','invalid_upstream_response',502)
                    block=blocks[index];part=record['delta'];typ=part['type']
                    if typ=='text_delta' and block['type']=='text':delta={'content':part['text']}
                    elif typ=='thinking_delta' and block['type']=='thinking':
                        block['thinking']=block.get('thinking','')+part['thinking'];delta={'reasoning_content':part['thinking']}
                    elif typ=='signature_delta' and block['type']=='thinking':block['signature']=block.get('signature','')+part['signature']
                    elif typ=='input_json_delta' and block['type']=='tool_use':block['_arguments']+=part['partial_json']
                    else:raise ChatCompatibilityError('unsupported_anthropic_output','Unsupported Messages delta',502)
                elif kind=='content_block_stop':
                    index=record['index'];_require(index in blocks and index not in stopped,'Invalid Messages block stop','invalid_upstream_response',502)
                    stopped.add(index);block=blocks[index]
                    if block['type']=='tool_use':
                        arguments=block['_arguments'] or json.dumps(block['_input'],separators=(',',':'))
                        _require(isinstance(json.loads(arguments),dict),'Invalid Messages tool arguments','invalid_upstream_response',502)
                        delta={'tool_calls':[{'index':block['_tool_index'],'function':{'arguments':arguments}}]}
                elif kind=='message_delta':
                    _require(set(blocks)==stopped,'Message ended before blocks closed','truncated_stream',502)
                    usage.update(record.get('usage',{}));reason=record.get('delta',{}).get('stop_reason')
                    if reason is not None:
                        _require(finish is None,'Duplicate Messages stop reason','invalid_upstream_response',502)
                        finish={'end_turn':'stop','stop_sequence':'stop','tool_use':'tool_calls','max_tokens':'length','model_context_window_exceeded':'length','refusal':'content_filter'}.get(reason)
                        _require(finish is not None,'Unsupported Messages stop reason','unsupported_anthropic_output',502)
                elif kind=='message_stop':
                    _require(finish is not None and set(blocks)==stopped,'Messages stream is incomplete','truncated_stream',502)
                    ended=True
                else:raise ChatCompatibilityError('unsupported_anthropic_output','Unsupported Messages event',502)
                for e in state.consume(chunk(delta)):yield event(e)
            _require(ended,'Messages stream ended without message_stop','truncated_stream',502)
            for block in thinking:_thinking_blocks(ANTHROPIC_THINKING+base64.b64encode(json.dumps([block]).encode()).decode())
            if thinking:thinking_payload=ANTHROPIC_THINKING+base64.b64encode(json.dumps(thinking,separators=(',',':')).encode()).decode()
            for key in ('input_tokens','output_tokens','cache_creation_input_tokens','cache_read_input_tokens'):
                _require(type(usage.get(key,0)) is int and usage.get(key,0)>=0,'Invalid Messages usage','invalid_upstream_response',502)
            total_input=sum(usage.get(key,0) for key in ('input_tokens','cache_creation_input_tokens','cache_read_input_tokens'))
            raw=chunk(reason=finish);raw['usage']={'prompt_tokens':total_input,'completion_tokens':usage.get('output_tokens',0),
                'prompt_tokens_details':{'cached_tokens':usage.get('cache_read_input_tokens',0)}}
            for e in state.consume(raw):yield event(e)
            for e in state.complete():yield event(e)
        except (ChatCompatibilityError,ValueError,TypeError,KeyError,UnicodeDecodeError) as error:
            response=state.response('failed');response['error']={'code':getattr(error,'code','invalid_upstream_response'),
                'message':str(error) if isinstance(error,ChatCompatibilityError) else 'Invalid Messages stream'}
            yield event({'type':'response.failed','response':response})
