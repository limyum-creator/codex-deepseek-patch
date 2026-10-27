import copy
import io
import json
import unittest

from responses_compat import ToolAdapter, SEARCH_NAME, normalize_numeric_arguments


SEARCH = {'type': 'tool_search', 'execution': 'client', 'description': 'Find tools',
          'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}
LOADED = {'type': 'namespace', 'name': 'files', 'description': 'File tools', 'tools': [
    {'type': 'function', 'name': 'read', 'description': 'Read file', 'defer_loading': True,
     'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}]}


class ToolAdapterTests(unittest.TestCase):
    def test_grok_integral_decimal_tool_argument_becomes_native_integer(self):
        tool={'type':'function','name':'exec_command','parameters':{'type':'object','properties':{'cmd':{'type':'string'},'yield_time_ms':{'type':'number'}}}}
        adapter=ToolAdapter({'tools':[tool]})
        call={'type':'function_call','name':'exec_command','call_id':'same','arguments':'{"cmd":"print(30000.0)","yield_time_ms":30000.0}'}
        actual=adapter.output_item(call)
        self.assertEqual(json.loads(actual['arguments']),{'cmd':'print(30000.0)','yield_time_ms':30000})
        self.assertIs(type(json.loads(actual['arguments'])['yield_time_ms']),int)
        self.assertEqual(actual['call_id'],'same')
        self.assertNotIn('namespace',actual)

    def test_normalization_keeps_fractional_strings_unknown_fields_and_exact_numbers(self):
        schema={'type':'object','properties':{'integer':{'type':'integer'},'fraction':{'type':'number'}}}
        for value in ['1.5','"30000.0"','true','null','1e99']:
            raw='{"integer":'+value+'}'
            self.assertEqual(normalize_numeric_arguments(raw,schema),raw)
        raw='{"integer":3e4,"fraction":0.123456789012345678901,"unknown":99.0,"command":"30000.0"}'
        normalized=normalize_numeric_arguments(raw,schema)
        self.assertIn('"integer":30000',normalized)
        self.assertIn('0.123456789012345678901',normalized)
        self.assertIn('"unknown":99.0',normalized)
        self.assertIn('"command":"30000.0"',normalized)
        self.assertEqual(normalize_numeric_arguments('{broken',schema),'{broken')

    def test_nested_integer_schema_and_namespace_alias_are_restored(self):
        schema={'type':'object','$defs':{'count':{'type':['integer','null']}},'properties':{'items':{'type':'array','items':{'$ref':'#/$defs/count'}}}}
        tool={'type':'namespace','name':'batch','tools':[{'type':'function','name':'read','parameters':schema}]}
        adapter=ToolAdapter({'tools':[tool]},flatten_namespaces=True)
        item=adapter.output_item({'type':'function_call','name':'batch__read','arguments':'{"items":[1.0,null,2.5]}','call_id':'c'})
        self.assertEqual((item['namespace'],item['name']),('batch','read'))
        self.assertEqual(item['arguments'],'{"items":[1,null,2.5]}')

    def test_stream_normalizes_only_complete_integer_calls(self):
        adapter=ToolAdapter({'tools':[{'type':'function','name':'shell','parameters':{'type':'object','properties':{'timeout_ms':{'type':'integer'}}}}]})
        start={'id':'i','type':'function_call','name':'shell','call_id':'c','arguments':''}
        self.assertEqual(adapter.events({'type':'response.output_item.added','item':start}),[])
        self.assertEqual(adapter.events({'type':'response.function_call_arguments.delta','item_id':'i','delta':'{"timeout_ms":30000.0}'}),[])
        done={**start,'arguments':'{"timeout_ms":30000.0}'}
        events=adapter.events({'type':'response.output_item.done','item':done})
        self.assertEqual([json.loads(e['item']['arguments']) for e in events],[{'timeout_ms':30000}]*2)
        self.assertEqual(adapter.response({'output':[done]})['output'][0]['arguments'],'{"timeout_ms":30000}')

    def test_search_round_trip_preserves_call_identity_and_loads_only_matches(self):
        adapter = ToolAdapter({'tools': [SEARCH]})
        function = adapter.body['tools'][0]
        self.assertEqual((function['type'], function['name']), ('function', SEARCH_NAME))
        native = adapter.output_item({'type': 'function_call', 'name': SEARCH_NAME, 'id': 'fc_a',
                                      'call_id': 'call_a', 'arguments': '{"query":"read","limit":1.0}'})
        self.assertEqual(native['arguments'], {'query': 'read', 'limit': 1})
        source = {'tools': [SEARCH], 'input': [native, {'type': 'tool_search_output',
                  'call_id': 'call_a', 'tools': [LOADED]}]}
        before = copy.deepcopy(source)
        next_call = ToolAdapter(source).body
        self.assertEqual(source, before)
        self.assertEqual([t['name'] for t in next_call['tools']], [SEARCH_NAME, 'files'])
        self.assertNotIn('defer_loading', next_call['tools'][1]['tools'][0])
        self.assertEqual(json.loads(next_call['input'][0]['arguments']), native['arguments'])
        self.assertEqual(next_call['input'][1]['call_id'], 'call_a')
        self.assertEqual(json.loads(next_call['input'][1]['output']), [LOADED])

    def test_prompt_additional_tools_are_current_declarations_and_roundtrip_custom(self):
        custom = {"type":"namespace", "name":"functions", "tools":[{"type":"custom","name":"apply_patch"}]}
        source = {"input":[{"type":"additional_tools","tools":[SEARCH,custom]},
                            {"role":"user","content":"Patch"},
                            {"type":"tool_search_output","call_id":"s","tools":[LOADED]}]}
        original = copy.deepcopy(source)
        adapter = ToolAdapter(source, flatten_namespaces=True)
        self.assertEqual([v["name"] for v in adapter.body["tools"]],
                         [SEARCH_NAME,"functions__apply_patch","files__read"])
        self.assertFalse(any(v.get("type") == "additional_tools" for v in adapter.body["input"]))
        actual = adapter.output_item({"type":"function_call","name":"functions__apply_patch",
                                      "call_id":"c","arguments":'{"input":"exact patch"}'})
        self.assertEqual((actual["type"],actual["namespace"],actual["input"]),
                         ("custom_tool_call","functions","exact patch"))
        self.assertEqual(source, original)

    def test_additional_tools_merge_namespace_without_duplicates(self):
        extra = copy.deepcopy(LOADED); extra["tools"][0]["name"] = "write"
        actual = ToolAdapter({"tools":[LOADED],"input":[
            {"type":"additional_tools","tools":[LOADED,extra]}]}).body
        self.assertEqual([t["name"] for t in actual["tools"][0]["tools"]],["read","write"])
        with self.assertRaises(ValueError):
            ToolAdapter({"input":[{"type":"additional_tools","tools":"not a tool list"}]})

    def test_repeated_search_does_not_duplicate_tools_or_discard_new_matches(self):
        extra = copy.deepcopy(LOADED); extra['tools'][0]['name'] = 'write'
        body = ToolAdapter({'tools': [SEARCH, LOADED], 'input': [
            {'type': 'tool_search_output', 'call_id': 'c', 'tools': [LOADED, extra]}]}).body
        self.assertEqual([t['name'] for t in body['tools'][1]['tools']], ['read', 'write'])

    def test_compaction_does_not_reenable_tools_from_old_discovery_history(self):
        for tools in (None, []):
            source={'input':[{'type':'tool_search_output','call_id':'c','tools':[LOADED]}]}
            if tools is not None:source['tools']=tools
            result=ToolAdapter(source).body
            self.assertFalse(result.get('tools'))

    def test_function_only_namespace_round_trip_and_history(self):
        source={'tools':[SEARCH], 'input':[{'type':'tool_search_output','call_id':'s','tools':[LOADED]},
            {'type':'function_call','namespace':'files','name':'read','call_id':'c','arguments':'{"path":"a"}'}]}
        adapter=ToolAdapter(source,flatten_namespaces=True)
        function=adapter.body['tools'][-1]
        self.assertEqual((function['type'],function['name']),('function','files__read'))
        self.assertEqual(adapter.body['input'][-1]['name'],'files__read')
        self.assertNotIn('namespace',adapter.body['input'][-1])
        native=adapter.output_item({'type':'function_call','name':'files__read','call_id':'c','arguments':'{"path":"a"}'})
        self.assertEqual(native,source['input'][-1])
        self.assertLessEqual(len(ToolAdapter.alias('x'*80,'y'*80)),64)

    def test_custom_tool_input_is_data_and_returns_as_native_call(self):
        source = {'tools': [{'type': 'custom', 'name': 'apply_patch', 'description': 'Patch files'}]}
        adapter = ToolAdapter(source)
        payload = '*** Begin Patch\n*** Add File: a\n+ok\n*** End Patch'
        native = adapter.output_item({'type': 'function_call', 'name': 'apply_patch', 'call_id': 'c',
                                      'arguments': json.dumps({'input': payload})})
        self.assertEqual(native['type'], 'custom_tool_call')
        self.assertEqual(native['input'], payload)
        history = ToolAdapter({'input': [native, {'type': 'custom_tool_call_output',
                              'call_id': 'c', 'output': 'done'}]}).body['input']
        self.assertEqual(json.loads(history[0]['arguments']), {'input': payload})
        self.assertEqual(history[1]['type'], 'function_call_output')

    def test_stream_filters_fragmented_arguments_and_converts_final_response(self):
        adapter = ToolAdapter({'tools': [SEARCH]})
        item = {'type': 'function_call', 'name': SEARCH_NAME, 'id': 'fc_1', 'call_id': 'call_1', 'arguments': ''}
        done = {**item, 'arguments': '{"query":"read"}'}
        events = [
            {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
            {'type': 'response.function_call_arguments.delta', 'item_id': 'fc_1', 'delta': '{"query":'},
            {'type': 'response.function_call_arguments.delta', 'item_id': 'fc_1', 'delta': '"read"}'},
            {'type': 'response.function_call_arguments.done', 'item_id': 'fc_1', 'arguments': done['arguments']},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': done},
            {'type': 'response.completed', 'response': {'output': [done], 'usage': {'input_tokens': 12}}},
        ]
        raw = ''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
        output = b''.join(adapter.stream(io.BytesIO(raw)))
        values = [json.loads(l[5:]) for l in output.splitlines() if l.startswith(b'data:')]
        self.assertEqual(len(values), 3)
        self.assertEqual([e['sequence_number'] for e in values], [0, 1, 2])
        self.assertEqual(values[1]['item']['arguments'], {'query': 'read'})
        self.assertEqual(values[1]['item']['call_id'], 'call_1')
        self.assertEqual(values[-1]['response']['output'][0]['type'], 'tool_search_call')
        self.assertEqual(values[-1]['response']['usage']['input_tokens'], 12)

    def test_unrelated_function_and_text_events_pass_unchanged(self):
        event = {'type': 'response.output_item.done', 'item': {'type': 'function_call', 'name': 'shell',
                 'arguments': '{"cmd":"pwd"}', 'call_id': 'normal'}}
        self.assertEqual(ToolAdapter({'tools': [SEARCH]}).events(event), [event])

    def test_invalid_native_call_and_alias_collision_are_explicit_errors(self):
        with self.assertRaises(ValueError):
            ToolAdapter({'tools': [{'type': 'function', 'name': SEARCH_NAME}]})
        adapter = ToolAdapter({'tools': [SEARCH]})
        with self.assertRaises(ValueError):
            adapter.output_item({'type': 'function_call', 'name': SEARCH_NAME, 'arguments': '{}'})


if __name__ == '__main__':
    unittest.main()
