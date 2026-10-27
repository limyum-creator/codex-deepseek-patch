import copy
import io
import json
import unittest

from chat_completions_compat import ChatCompatibilityError, ResponsesToChat
from responses_compat import ToolAdapter, SEARCH_NAME


def chunk(delta=None, finish=None, **extra):
    return {'id':'chatcmpl-fixture','created':123,'model':'arbitrary-provider-model',
            'choices':[{'index':0,'delta':delta or {},'finish_reason':finish}],**extra}


def sse(records, done=True):
    return b''.join(b'data: '+json.dumps(record,ensure_ascii=False).encode()+b'\r\n\r\n' for record in records) + (
        b'data: [DONE]\r\n\r\n' if done else b'')


def final(events):
    return [event for event in events if event['type'] in ('response.completed','response.failed','response.incomplete')][-1]


class RequestTests(unittest.TestCase):
    def test_basic_options_are_standard_and_effort_requires_capability(self):
        source={'model':'not-a-known-vendor-name','input':'Hello','instructions':'Be concise.',
                'stream':True,'max_output_tokens':120,'temperature':.2,'parallel_tool_calls':False,
                'reasoning':{'effort':'high','summary':'auto'},'text':{'format':{'type':'json_object'}}}
        original=copy.deepcopy(source)
        adapter=ResponsesToChat(source)
        self.assertEqual(adapter.body['messages'],[{'role':'system','content':'Be concise.'},{'role':'user','content':'Hello'}])
        self.assertEqual(adapter.body['max_completion_tokens'],120)
        self.assertEqual(adapter.body['stream_options'],{'include_usage':True})
        self.assertNotIn('reasoning_effort',adapter.body)
        self.assertNotIn('thinking',adapter.body)
        self.assertEqual(ResponsesToChat(source,supports_reasoning_effort=True).body['reasoning_effort'],'high')
        self.assertEqual(source,original)

    def test_user_image_urls_and_data_urls_preserve_order(self):
        data='data:image/png;base64,AQID'
        source={'model':'any','input':[{'role':'user','content':[{'type':'input_text','text':'Describe '},
            {'type':'input_image','image_url':data,'detail':'low'},
            {'type':'input_text','text':' and '},{'type':'input_image','image_url':'https://example.org/image.png'}]}]}
        result=ResponsesToChat(source).body['messages'][0]['content']
        self.assertEqual([part['type'] for part in result],['text','image_url','text','image_url'])
        self.assertEqual(result[1],{'type':'image_url','image_url':{'url':data,'detail':'low'}})

    def test_multiple_tool_round_trips_keep_ids_order_and_assistant_text(self):
        source={'model':'any','input':[{'role':'user','content':'Read both.'},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'Checking.'}]},
            {'type':'function_call','call_id':'a','name':'read','arguments':'{"path":"a"}'},
            {'type':'function_call','call_id':'b','name':'read','arguments':'{"path":"b"}'},
            {'type':'function_call_output','call_id':'b','output':'second'},
            {'type':'function_call_output','call_id':'a','output':[{'type':'input_text','text':'first'}]},
            {'type':'function_call','call_id':'c','name':'write','arguments':'{}'},
            {'type':'function_call_output','call_id':'c','output':'saved'},
            {'role':'user','content':'Continue.'}]}
        messages=ResponsesToChat(source).body['messages']
        self.assertEqual([m['role'] for m in messages],['user','assistant','tool','tool','assistant','tool','user'])
        self.assertEqual(messages[1]['content'],'Checking.')
        self.assertEqual([c['id'] for c in messages[1]['tool_calls']],['a','b'])
        self.assertEqual([messages[i]['tool_call_id'] for i in (2,3,5)],['b','a','c'])

    def test_unknown_or_missing_tool_result_is_explicit(self):
        cases=[[{'type':'function_call_output','call_id':'unknown','output':'x'}],
               [{'type':'function_call','call_id':'a','name':'read','arguments':'{}'}],
               [{'type':'function_call','call_id':'a','name':'read','arguments':'{}'}, {'role':'user','content':'next'}]]
        for items in cases:
            with self.subTest(items=items), self.assertRaises(ChatCompatibilityError):
                ResponsesToChat({'model':'any','input':items})

    def test_file_audio_opaque_and_unconverted_items_are_never_dropped(self):
        cases=[{'role':'user','content':[{'type':'input_file','file_id':'f'}]},
               {'role':'user','content':[{'type':'input_audio','data':'AA'}]},
               {'role':'user','content':[{'type':'input_image','file_id':'f'}]},
               {'type':'reasoning','encrypted_content':'opaque'},
               {'type':'compaction','encrypted_content':'opaque'},
               {'type':'custom_tool_call','call_id':'c','input':'hello'}]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ChatCompatibilityError):
                ResponsesToChat({'model':'any','input':[item]})

    def test_public_reasoning_history_becomes_standard_assistant_text(self):
        messages=ResponsesToChat({'model':'any','input':[{'type':'reasoning','summary':[
            {'type':'summary_text','text':'A public summary.'}]},{'role':'user','content':'Next.'}]}).body['messages']
        self.assertEqual(messages[0],{'role':'assistant','content':'A public summary.'})
        self.assertNotIn('reasoning_content',messages[0])

    def test_tooladapter_namespace_custom_search_pipeline(self):
        body={'model':'any','input':'Do it.','tools':[
            {'type':'namespace','name':'files','tools':[{'type':'custom','name':'patch'}]},
            {'type':'tool_search','parameters':{'type':'object'}}],
            'tool_choice':{'type':'custom','namespace':'files','name':'patch'}}
        tools=ToolAdapter(body,flatten_namespaces=True)
        chat=ResponsesToChat(tools.body)
        self.assertEqual([t['function']['name'] for t in chat.body['tools']],['files__patch',SEARCH_NAME])
        self.assertEqual(chat.body['tool_choice'],{'type':'function','function':{'name':'files__patch'}})
        result=chat.response({'choices':[{'index':0,'message':{'role':'assistant','tool_calls':[
            {'id':'c','type':'function','function':{'name':'files__patch','arguments':'{"input":"exact text"}'}}]},'finish_reason':'tool_calls'}]})
        native=tools.response(result)['output'][0]
        self.assertEqual((native['type'],native['namespace'],native['name'],native['input']),
                         ('custom_tool_call','files','patch','exact text'))

    def test_structured_output_schema_and_developer_role(self):
        body={'model':'any','input':[{'role':'developer','content':'Rule.'}],
              'text':{'format':{'type':'json_schema','name':'answer','schema':{'type':'object'},'strict':True}}}
        converted=ResponsesToChat(body).body
        self.assertEqual(converted['messages'][0]['role'],'developer')
        self.assertEqual(converted['response_format']['json_schema']['name'],'answer')


class StreamingTests(unittest.TestCase):
    def setUp(self):self.adapter=ResponsesToChat({'model':'any','input':'Hello','stream':True})

    def test_text_fragments_usage_and_sequence_form_native_events(self):
        records=[chunk({'role':'assistant','content':'你'}),chunk({'content':'好'}),chunk(finish='stop'),
                 {'choices':[],'usage':{'prompt_tokens':13,'completion_tokens':7,'total_tokens':20,
                  'prompt_tokens_details':{'cached_tokens':3},'completion_tokens_details':{'reasoning_tokens':2}}}]
        raw=sse(records)
        # Split arbitrary bytes inside UTF-8 and SSE boundaries, not just lines.
        events=list(self.adapter.events(raw[i:i+7] for i in range(0,len(raw),7)))
        self.assertEqual(events[0]['type'],'response.created')
        self.assertEqual([e['delta'] for e in events if e['type']=='response.output_text.delta'],['你','好'])
        result=final(events)['response']
        self.assertEqual(result['output'][0]['content'][0]['text'],'你好')
        self.assertEqual(result['usage']['input_tokens'],13)
        self.assertEqual(result['usage']['output_tokens_details'],{'reasoning_tokens':2})
        self.assertEqual([e['sequence_number'] for e in events],list(range(len(events))))

    def test_interleaved_multiple_tool_fragments_preserve_each_call(self):
        records=[chunk({'tool_calls':[
            {'index':0,'id':'call_a','type':'function','function':{'name':'read','arguments':'{"a":'}},
            {'index':1,'id':'call_b','type':'function','function':{'name':'write','arguments':'{"b":'}}]}),
            chunk({'tool_calls':[{'index':1,'function':{'arguments':'2}'}},{'index':0,'function':{'arguments':'1}'}}]}),
            chunk(finish='tool_calls')]
        events=list(self.adapter.events(io.BytesIO(sse(records))))
        calls=final(events)['response']['output']
        self.assertEqual([(c['call_id'],c['name'],c['arguments']) for c in calls],
                         [('call_a','read','{"a":1}'),('call_b','write','{"b":2}')])
        for call in calls:
            fragments=[e['delta'] for e in events if e['type']=='response.function_call_arguments.delta' and e['item_id']==call['id']]
            self.assertEqual(''.join(fragments),call['arguments'])
            self.assertTrue(any(e['type']=='response.function_call_arguments.done' and e['item_id']==call['id'] for e in events))

    def test_tooladapter_stream_restores_custom_call_after_fragment_assembly(self):
        tools=ToolAdapter({'model':'any','input':'Do it','tools':[{'type':'custom','name':'exec'}]},flatten_namespaces=True)
        chat=ResponsesToChat(tools.body)
        records=[chunk({'tool_calls':[{'index':0,'id':'c','function':{'name':'exec','arguments':'{"input":'}}]}),
                 chunk({'tool_calls':[{'index':0,'function':{'arguments':'"hello"}'}}]}),chunk(finish='tool_calls')]
        events=[out for event in chat.events(records) for out in tools.events(event)]
        done=next(e['item'] for e in events if e['type']=='response.output_item.done')
        self.assertEqual((done['type'],done['call_id'],done['input']),('custom_tool_call','c','hello'))

    def test_out_of_order_tools_use_protocol_indices_and_repeated_names_do_not_duplicate(self):
        records=[chunk({'tool_calls':[{'index':1,'id':'b','function':{'name':'second','arguments':'{'}}]}),
                 chunk({'tool_calls':[{'index':0,'id':'a','function':{'name':'first','arguments':'{}'}},
                     {'index':1,'function':{'name':'second','arguments':'}'}}]}),chunk(finish='tool_calls')]
        result=final(list(self.adapter.events(records)))['response']
        self.assertEqual([(i['call_id'],i['name'],i['arguments']) for i in result['output']],
                         [('a','first','{}'),('b','second','{}')])

    def test_unsupported_output_media_and_malformed_records_fail_explicitly(self):
        cases=[[chunk({'audio':{'data':'AA'}}),chunk(finish='stop')],
               [chunk({'content':[{'type':'image','data':'AA'}]}),chunk(finish='stop')],
               [{'choices':['invalid']}],
               [chunk(finish='stop',usage={'prompt_tokens_details':'invalid'})],
               [chunk({'tool_calls':[{'index':1,'id':'b','function':{'name':'f','arguments':'{}'}}]}),chunk(finish='tool_calls')]]
        for records in cases:
            with self.subTest(records=records):
                self.assertEqual(final(list(self.adapter.events(records)))['type'],'response.failed')

    def test_public_reasoning_content_is_summary_with_no_fake_private_state(self):
        events=list(self.adapter.events([chunk({'reasoning_content':'公开'}),chunk({'reasoning_content':'摘要'}),
                                        chunk({'content':'Answer.'}),chunk(finish='stop')]))
        result=final(events)['response'];reasoning=result['output'][0]
        self.assertEqual(reasoning['type'],'reasoning')
        self.assertEqual(reasoning['summary'],[{'type':'summary_text','text':'公开摘要'}])
        self.assertNotIn('encrypted_content',reasoning)
        self.assertNotIn('thinking',reasoning)
        self.assertTrue(any(e['type']=='response.reasoning_summary_text.done' for e in events))
        self.assertEqual(self.adapter.reasoning_source['protocol'],'chat_completions')

    def test_empty_success_and_length_or_filter_finish(self):
        events=list(self.adapter.events([chunk(finish='stop')]))
        self.assertEqual(final(events)['type'],'response.completed')
        self.assertEqual(final(events)['response']['output'],[])
        for finish,reason in [('length','max_output_tokens'),('content_filter','content_filter')]:
            with self.subTest(finish=finish):
                result=final(list(self.adapter.events([chunk({'content':'partial'},finish=finish)])))
                self.assertEqual(result['type'],'response.incomplete')
                self.assertEqual(result['response']['incomplete_details']['reason'],reason)

    def test_upstream_error_and_truncated_sse_never_complete(self):
        inputs=[[{'error':{'code':'quota_exceeded','message':'No capacity'}}],
                io.BytesIO(sse([chunk({'content':'partial'})],done=False)),
                io.BytesIO(b'data: {"choices":[]'),
                io.BytesIO(b'data: [DONE]\n\n'),
                io.BytesIO(b'data: not-json\n\n')]
        for stream in inputs:
            with self.subTest(stream=stream):
                events=list(self.adapter.events(stream))
                self.assertEqual(final(events)['type'],'response.failed')
                self.assertFalse(any(e['type']=='response.completed' for e in events))
        error=final(list(self.adapter.events(inputs[0])))['response']['error']
        self.assertEqual(error['code'],'quota_exceeded')

    def test_comments_multiline_json_and_finish_without_done_are_supported(self):
        raw=b':keepalive\n\ndata: {"choices":\ndata: [{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        self.assertEqual(final(list(self.adapter.events(io.BytesIO(raw))))['type'],'response.completed')

    def test_stream_bytes_are_responses_sse(self):
        raw=b''.join(self.adapter.stream([chunk({'content':'ok'}),chunk(finish='stop')]))
        self.assertIn(b'event: response.output_text.delta\n',raw)
        events=[json.loads(line[5:]) for line in raw.splitlines() if line.startswith(b'data:')]
        self.assertEqual(final(events)['response']['output'][0]['content'][0]['text'],'ok')

    def test_changed_tool_id_multiple_choices_and_data_after_finish_fail(self):
        cases=[[chunk({'tool_calls':[{'index':0,'id':'a','function':{'name':'read'}}]}),
                chunk({'tool_calls':[{'index':0,'id':'b','function':{'arguments':'{}'}}]}),chunk(finish='tool_calls')],
               [{'choices':[{'index':0,'delta':{}},{'index':1,'delta':{}}]}],
               [chunk(finish='stop'),chunk({'content':'too late'})],
               [chunk(finish='tool_calls')]]
        for records in cases:
            with self.subTest(records=records):
                self.assertEqual(final(list(self.adapter.events(records)))['type'],'response.failed')


class NonstreamTests(unittest.TestCase):
    def test_nonstream_text_reasoning_usage_and_refusal(self):
        adapter=ResponsesToChat({'model':'any','input':'Hello'})
        body={'model':'peer','choices':[{'index':0,'message':{'role':'assistant',
            'reasoning_content':'Public summary.','content':'Answer.','refusal':'A refusal.'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':5,'completion_tokens':9}}
        result=adapter.response(body)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['model'],'peer')
        self.assertEqual([i['type'] for i in result['output']],['reasoning','message','message'])
        self.assertEqual(result['usage']['total_tokens'],14)
        self.assertEqual(result['output'][2]['content'][0],{'type':'refusal','refusal':'A refusal.'})

    def test_nonstream_upstream_error_or_missing_finish_is_failed(self):
        adapter=ResponsesToChat({'model':'any','input':'Hello'})
        self.assertEqual(adapter.response({'error':{'message':'upstream failure'}})['status'],'failed')
        self.assertEqual(adapter.response({'choices':[{'message':{'content':'unfinished'}}]})['status'],'failed')
        with self.assertRaises(ChatCompatibilityError):adapter.response({'choices':[]})


if __name__=='__main__':unittest.main()


class AnthropicTests(unittest.TestCase):
    def adapter(self,**body):
        from chat_completions_compat import ResponsesToAnthropic
        return ResponsesToAnthropic({'model':'arbitrary-name','input':'hello',**body},max_output_tokens=64000,reasoning_levels=['low','high'])

    def records(self):
        return [
            {'type':'message_start','message':{'usage':{'input_tokens':7,'cache_read_input_tokens':11,'cache_creation_input_tokens':5}}},
            {'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':''}},
            {'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'Public facts'}},
            {'type':'content_block_delta','index':0,'delta':{'type':'signature_delta','signature':'private-signature'}},
            {'type':'content_block_stop','index':0},
            {'type':'content_block_start','index':1,'content_block':{'type':'tool_use','id':'call_1','name':'lookup','input':{}}},
            {'type':'content_block_delta','index':1,'delta':{'type':'input_json_delta','partial_json':'{"query":'}},
            {'type':'content_block_delta','index':1,'delta':{'type':'input_json_delta','partial_json':'"星空"}'}},
            {'type':'content_block_stop','index':1},
            {'type':'message_delta','delta':{'stop_reason':'tool_use'},'usage':{'output_tokens':4}},
            {'type':'message_stop'}]

    def test_anthropic_tools_images_usage_thinking_roundtrip_and_foreign_isolation(self):
        from reasoning_origin import stamp_response,resolve_request
        adapter=self.adapter(stream=True,reasoning={'effort':'low'},tools=[{'type':'function','name':'lookup','parameters':{'type':'object'}}])
        self.assertEqual(adapter.body['max_tokens'],64000)
        self.assertEqual(adapter.body['output_config']['effort'],'low')
        result=final(adapter.events([sse(self.records(),done=False)]))['response']
        self.assertEqual(result['status'],'completed');self.assertEqual(result['usage']['input_tokens'],23)
        self.assertEqual(result['usage']['input_tokens_details']['cached_tokens'],11)
        self.assertEqual(json.loads(result['output'][-1]['arguments']),{'query':'星空'})
        stamped=stamp_response(result,'arbitrary-name')
        history=[{'role':'user','content':'hello'},*stamped['output'],{'type':'function_call_output','call_id':'call_1','output':'found'}]
        restored,_,_=resolve_request({'model':'arbitrary-name','input':history},'arbitrary-name')
        sent=self.adapter(input=restored['input']).body['messages']
        self.assertEqual(sent[1]['content'][0],{'type':'thinking','thinking':'Public facts','signature':'private-signature'})
        self.assertEqual(sent[-1]['content'][0]['tool_use_id'],'call_1')
        foreign,_,_=resolve_request({'input':history},'another-model')
        self.assertNotIn('private-signature',json.dumps(foreign));self.assertIn('Public facts',json.dumps(foreign))
        image=self.adapter(input=[{'role':'user','content':[{'type':'input_image','image_url':'data:image/png;base64,YQ=='}]}])
        self.assertEqual(image.body['messages'][0]['content'][0]['source']['type'],'base64')

    def test_anthropic_truncation_errors_and_output_limit_are_explicit(self):
        from chat_completions_compat import ResponsesToAnthropic
        with self.assertRaisesRegex(ChatCompatibilityError,'max_output_tokens'):ResponsesToAnthropic({'model':'x','input':'hello'})
        for records in (self.records()[:-1],self.records()[:-3]+[{'type':'message_stop'}],self.records()+[{'type':'ping'}]):
            self.assertEqual(final(self.adapter().events(records))['type'],'response.failed')
        error=final(self.adapter().events([{'type':'error','error':{'type':'overloaded_error','message':'fixture'}}]))
        self.assertEqual(error['response']['error']['code'],'overloaded_error')
        value={'type':'message','content':[{'type':'text','text':'partial'}],'stop_reason':'max_tokens','usage':{'input_tokens':9,'output_tokens':64}}
        result=self.adapter().response(value)
        self.assertEqual(result['status'],'incomplete');self.assertEqual(result['incomplete_details']['reason'],'max_output_tokens')

    def test_anthropic_redacted_thinking_and_tool_result_order(self):
        value={'type':'message','content':[{'type':'redacted_thinking','data':'opaque'},{'type':'text','text':'answer'}],
               'stop_reason':'end_turn','usage':{'input_tokens':1,'output_tokens':1}}
        result=self.adapter().response(value)
        self.assertEqual(result['status'],'completed');self.assertIn('encrypted_content',result['output'][0])
        body={'input':[{'role':'user','content':'hello'},{'type':'function_call','call_id':'1','name':'a','arguments':'{}'},
                       {'type':'function_call','call_id':'2','name':'b','arguments':'{}'},
                       {'type':'function_call_output','call_id':'2','output':'b'}, {'type':'function_call_output','call_id':'1','output':'a'}]}
        compact=self.adapter(input=[{'role':'assistant','content':'Readable compacted history'},{'role':'user','content':'continue'}])
        self.assertEqual(compact.body['messages'][0]['role'],'assistant')
        messages=self.adapter(**body).body['messages']
        self.assertEqual([b['tool_use_id'] for b in messages[-1]['content']],['2','1'])
