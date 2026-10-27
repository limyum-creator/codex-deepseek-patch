"""Per-request native tool discovery adaptation for function-only Responses peers.

No tools are executed here. Codex retains discovery, execution and approvals.
"""
import copy
import hashlib
import json
from decimal import Decimal, InvalidOperation

SEARCH_NAME = 'codex_patch_tool_search'


def has_numeric_schema(value, depth=0):
    if depth > 32 or not isinstance(value, dict):
        return False
    kind = value.get('type')
    if kind in ('integer','number') or isinstance(kind, list) and any(k in kind for k in ('integer','number')):
        return True
    return any(has_numeric_schema(child, depth+1) for child in value.values() if isinstance(child, dict)) or any(
        has_numeric_schema(child, depth+1) for children in value.values() if isinstance(children, list) for child in children)


def normalize_numeric_arguments(arguments, schema):
    """Preserve numeric values; write integral decimals as JSON integers in declared numeric fields."""
    if not isinstance(arguments, str):
        return arguments
    changed = False
    def walk(value, rule, depth=0):
        nonlocal changed
        if depth > 32 or not isinstance(rule, dict):
            return value
        reference = rule.get('$ref')
        if isinstance(reference, str) and reference.startswith('#/'):
            resolved = schema
            for part in reference[2:].split('/'):
                resolved = resolved.get(part.replace('~1','/').replace('~0','~'), {}) if isinstance(resolved,dict) else {}
            value = walk(value, resolved, depth+1)
        kind = rule.get('type')
        numeric = kind in ('integer','number') or isinstance(kind,list) and any(k in kind for k in ('integer','number'))
        if numeric and isinstance(value,Decimal) and value.is_finite() and -(2**63) <= value <= 2**64-1 and value == value.to_integral_value():
            changed = True
            return int(value)
        if isinstance(value, dict):
            properties = rule.get('properties', {})
            for key in value:
                child = properties.get(key, rule.get('additionalProperties', {}))
                value[key] = walk(value[key], child, depth+1)
        elif isinstance(value, list):
            prefixes = rule.get('prefixItems', [])
            value = [walk(item, prefixes[i] if i < len(prefixes) else rule.get('items',{}), depth+1) for i,item in enumerate(value)]
        for keyword in ('allOf','anyOf','oneOf'):
            for child in rule.get(keyword, []):
                value = walk(value, child, depth+1)
        return value
    def encode(value):
        if isinstance(value,Decimal):
            if not value.is_finite():raise ValueError('Nonfinite number')
            return str(value)
        if isinstance(value,dict):return '{'+','.join(json.dumps(k)+':'+encode(v) for k,v in value.items())+'}'
        if isinstance(value,list):return '['+','.join(encode(v) for v in value)+']'
        return json.dumps(value,allow_nan=False)
    try:
        value=walk(json.loads(arguments,parse_float=Decimal),schema)
        return encode(value) if changed else arguments
    except (ValueError,TypeError,InvalidOperation,RecursionError):
        return arguments  # Codex retains validation and reports malformed calls.


class ToolAdapter:
    def __init__(self, body, flatten_namespaces=False):
        self.kinds = {}
        self.schemas = {}
        self.pending = {}
        self.sequence = 0
        self.body = copy.deepcopy(body)
        self.body['tools'] = [self.tool(t) for t in body.get('tools', [])]
        if 'tools' not in body:
            self.body.pop('tools')
        if isinstance(body.get('input'), list):
            # ResponsesLite carries the current declarations in a prompt-only
            # additional_tools item. Function-only peers need ordinary tools.
            for item in body['input']:
                if item.get('type') == 'additional_tools':
                    if not isinstance(item.get('tools'), list):
                        raise ValueError('Invalid additional tool declarations')
                    for tool in item['tools']:
                        self.add_loaded(self.tool(tool))
            # Native Responses carries discovered declarations in history. A
            # function-only peer also needs them in its current tool catalog.
            discovery = SEARCH_NAME in self.kinds
            for item in body['input']:
                if discovery and item.get('type') == 'tool_search_output':
                    for tool in item.get('tools', []):
                        self.add_loaded(self.tool(tool))
            self.body['input'] = [self.input_item(x) for x in body['input'] if x.get('type') != 'additional_tools']
        choice = self.body.get('tool_choice')
        if isinstance(choice, dict):
            if choice.get('type') == 'tool_search':
                self.body['tool_choice'] = {'type': 'function', 'name': SEARCH_NAME}
            elif choice.get('type') == 'custom':
                self.body['tool_choice'] = {**choice, 'type': 'function'}
        if flatten_namespaces:
            self.flatten_namespaces()

    @staticmethod
    def alias(namespace, name):
        value=namespace+'__'+name
        return value if len(value)<=64 else 'cp_'+hashlib.sha256(value.encode()).hexdigest()[:40]

    def flatten_namespaces(self):
        flattened=[]
        names={t.get('name') for t in self.body.get('tools',[]) if t.get('type')!='namespace'}
        for group in self.body.get('tools',[]):
            if group.get('type')!='namespace':
                flattened.append(group);continue
            namespace=group['name']
            for tool in group['tools']:
                name=tool['name'];alias=self.alias(namespace,name)
                if alias in names:raise ValueError('Namespaced tool alias collision')
                names.add(alias)
                self.kinds[alias]=self.kinds.get(namespace+'_'+name,('function',namespace,name))
                flattened.append({**tool,'name':alias,'description':namespace+'.'+name+'\n'+tool.get('description','')})
        if 'tools' in self.body:self.body['tools']=flattened
        for item in self.body.get('input',[]) if isinstance(self.body.get('input'),list) else []:
            if item.get('type')=='function_call' and item.get('namespace'):
                item['name']=self.alias(item.pop('namespace'),item['name'])
        choice=self.body.get('tool_choice')
        if isinstance(choice,dict) and choice.get('namespace'):
            choice['name']=self.alias(choice.pop('namespace'),choice['name'])

    def tool(self, tool, namespace=None):
        tool = copy.deepcopy(tool)
        tool.pop('defer_loading', None)
        kind = tool.get('type')
        if kind == 'namespace':
            tool['tools'] = [self.tool(t, tool['name']) for t in tool['tools']]
        elif kind == 'tool_search':
            if namespace is not None:
                raise ValueError('Unexpected namespaced tool search')
            self.kinds[SEARCH_NAME] = ('search', None, 'tool_search')
            tool = {'type': 'function', 'name': SEARCH_NAME,
                    'description': tool.get('description', 'Discover available tools.')
                                   .replace('`tool_search`', '`'+SEARCH_NAME+'`'),
                    'parameters': tool['parameters']}
        elif kind == 'custom':
            name = tool['name']
            key = name if namespace is None else namespace+'_'+name
            self.kinds[key] = ('custom', namespace, name)
            tool = {'type': 'function', 'name': name,
                    'description': tool.get('description', '')+'\nPass the exact tool input as the input string.',
                    'parameters': {'type': 'object', 'properties': {'input': {'type': 'string'}},
                                   'required': ['input'], 'additionalProperties': False}}
        elif tool.get('name') == SEARCH_NAME:
            raise ValueError('Tool name collides with the discovery adapter')
        elif kind == 'function' and has_numeric_schema(tool.get('parameters')):
            name=tool['name'];key=name if namespace is None else namespace+'_'+name
            self.kinds[key]=('function',namespace,name)
            self.schemas[(namespace,name)]=tool['parameters']
        return tool

    def add_loaded(self, tool):
        tools = self.body.setdefault('tools', [])
        prior = next((t for t in tools if t.get('name') == tool.get('name')
                      and t.get('type') == tool.get('type')), None)
        if prior is None:
            tools.append(tool)
        elif tool.get('type') == 'namespace':
            names = {t['name'] for t in prior['tools']}
            prior['tools'].extend(t for t in tool['tools'] if t['name'] not in names)

    def input_item(self, item):
        item = copy.deepcopy(item)
        kind = item.get('type')
        if kind == 'custom_tool_call':
            item.update(type='function_call', arguments=json.dumps({'input': item.pop('input')}))
        elif kind == 'custom_tool_call_output':
            item['type'] = 'function_call_output'
        elif kind == 'tool_search_call':
            arguments = item.get('arguments', {})
            item = {k: v for k, v in item.items() if k in ('id', 'call_id', 'status')}
            item.update(type='function_call', name=SEARCH_NAME,
                        arguments=json.dumps(arguments))
        elif kind == 'tool_search_output':
            item = {'type': 'function_call_output', 'call_id': item['call_id'],
                    'output': json.dumps(item.get('tools', item.get('output', [])), ensure_ascii=False)}
        return item

    def kind(self, item):
        if item.get('type') != 'function_call':
            return None
        name = item.get('name', '')
        namespace = item.get('namespace')
        return self.kinds.get(namespace+'_'+name if namespace else name)

    def output_item(self, item):
        descriptor = self.kind(item)
        if not descriptor:
            return item
        if descriptor[0]=='function':
            result={**item,'name':descriptor[2]}
            if descriptor[1] is not None:result['namespace']=descriptor[1]
            schema=self.schemas.get((descriptor[1],descriptor[2]))
            if schema:result['arguments']=normalize_numeric_arguments(item.get('arguments'),schema)
            return result
        arguments = item.get('arguments', '{}')
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        result = {k: v for k, v in item.items() if k in ('id', 'call_id', 'status')}
        if descriptor[0] == 'search':
            if not isinstance(args, dict) or not isinstance(args.get('query'), str):
                raise ValueError('Invalid discovery arguments')
            if isinstance(args.get('limit'), float) and args['limit'].is_integer():
                args['limit'] = int(args['limit'])
            result.update(type='tool_search_call', execution='client', arguments=args)
        else:
            if not isinstance(args, dict) or not isinstance(args.get('input'), str):
                raise ValueError('Invalid custom tool input')
            result.update(type='custom_tool_call', name=descriptor[2], input=args['input'])
            if descriptor[1]:
                result['namespace'] = descriptor[1]
        return result

    def response(self, body):
        body = copy.deepcopy(body)
        if isinstance(body.get('output'), list):
            body['output'] = [self.output_item(x) for x in body['output']]
        return body

    def events(self, event):
        kind = event.get('type', '')
        item = event.get('item', {})
        if kind == 'response.output_item.added' and self.kind(item):
            self.pending[item['id']] = copy.deepcopy(event)
            return []
        if kind.startswith('response.function_call_arguments.') and event.get('item_id') in self.pending:
            return []
        if kind == 'response.output_item.done' and self.kind(item):
            converted = self.output_item(item)
            added = self.pending.pop(item['id'], {**event, 'type': 'response.output_item.added'})
            return [{**added, 'item': {**converted, 'status': 'in_progress'}}, {**event, 'item': converted}]
        if isinstance(event.get('response'), dict):
            return [{**event, 'response': self.response(event['response'])}]
        return [event]

    def stream(self, response):
        """Keep SSE framing; emit converted complete calls without JSON fragments."""
        lines = []
        for line in response:
            if line.strip():
                lines.append(line)
                continue
            if not lines:
                continue
            data = b'\n'.join(x[5:].strip() for x in lines if x.startswith(b'data:'))
            original = b''.join(lines)+b'\n'
            lines = []
            if not data or data == b'[DONE]':
                yield original
                continue
            event = json.loads(data)
            for value in self.events(event):
                value = {**value, 'sequence_number': self.sequence}
                self.sequence += 1
                yield ('event: '+value['type']+'\ndata: '+json.dumps(value, ensure_ascii=False)+'\n\n').encode()
        if lines:
            raise ValueError('Truncated upstream event frame')
