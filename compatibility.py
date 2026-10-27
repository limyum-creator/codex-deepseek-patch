"""Conservative source contracts for locally supplied Codex desktop bundles.

No vendor source is shipped here. Unknown structure fails closed; app versions and
asset hashes are evidence, not a compatibility allowlist.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

FORMAT = 1
MARKER = "CodexDesktopDeepSeekFlashPatch"
HELPER = "codexPatchAppServerParams"
IDENT = r"[A-Za-z_$][\w$]*"
GUARD = 'if(this.dispatchMessage==null)throw Error(`AppServerRequestClient is missing a message dispatcher`);'


class CompatibilityError(RuntimeError):
    pass


def _require(condition, message):
    if not condition:
        raise CompatibilityError(message)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tokens(source, start=0):
    """Yield lexical token spans, keeping quoted text/comments out of anchors.

    This is a small lexer, not a JS rewriter. Unknown/unterminated literals are
    rejected and every proposed edit must also pass Node's syntax/VM checks.
    """
    i, previous = start, None
    regex_after = {None, "(", "[", "{", "=", ":", ",", ";", "!", "?", "&&", "||", "??", "=>", "return", "throw", "case", "yield", "typeof", "void", "delete", "in", "of"}
    while i < len(source):
        c = source[i]
        if c.isspace():
            i += 1
            continue
        if source.startswith("//", i):
            end = source.find("\n", i + 2)
            i = len(source) if end == -1 else end + 1
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            _require(end != -1, "Unterminated JavaScript comment")
            i = end + 2
            continue
        begin = i
        if c in "'\"`":
            quote = c
            i += 1
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif source[i] == quote:
                    i += 1
                    break
                elif quote == "`" and source.startswith("${", i):
                    i = _balanced_end(source, i + 1)
                else:
                    i += 1
            else:
                raise CompatibilityError("Unterminated JavaScript string/template")
            token = "<literal>"
        elif c == "/" and previous in regex_after:
            i += 1
            in_class = False
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif source[i] == "[":
                    in_class = True; i += 1
                elif source[i] == "]":
                    in_class = False; i += 1
                elif source[i] == "/" and not in_class:
                    i += 1
                    while i < len(source) and source[i].isalpha():
                        i += 1
                    break
                elif source[i] in "\r\n":
                    raise CompatibilityError("Unterminated JavaScript regular expression")
                else:
                    i += 1
            else:
                raise CompatibilityError("Unterminated JavaScript regular expression")
            token = "<literal>"
        elif c.isalpha() or c in "_$" or c.isdigit():
            i += 1
            while i < len(source) and (source[i].isalnum() or source[i] in "_$"):
                i += 1
            token = source[begin:i]
        else:
            token = next((p for p in ("===", "!==", "=>", "&&", "||", "??", "?.", "==", "!=", "++", "--", "**") if source.startswith(p, i)), c)
            i += len(token)
        yield token, begin, i
        previous = token


def _balanced_end(source, opening):
    pairs = {"{": "}", "(": ")", "[": "]"}
    _require(source[opening] in pairs, "Expected a balanced JavaScript group")
    stack = []
    for token, begin, end in _tokens(source, opening):
        if token in pairs:
            stack.append(pairs[token])
        elif token in pairs.values():
            _require(stack and stack.pop() == token, "Unbalanced JavaScript group")
            if not stack:
                return end
    raise CompatibilityError("Unterminated JavaScript group")


def _methods(source, executable_positions=None):
    found = {"sendRequest": [], "prewarmThreadStart": []}
    prior = []
    scopes, declarations, owners = [], {name: [] for name in found}, {}
    for token, begin, end in _tokens(source):
        if executable_positions is not None and begin in executable_positions:
            executable_positions[begin] = True
        if token in found and (not prior or prior[-1][0] not in (".", "?.")):
            declarations[token].append((begin, scopes[-1] if scopes else None))
        if token in found and prior and prior[-1][0] == "async":
            found[token].append(prior[-1][1])
            owners[prior[-1][1]] = scopes[-1] if scopes else None
        if token == "{":
            scopes.append(begin)
        elif token == "}":
            _require(bool(scopes), "Unbalanced source block")
            scopes.pop()
        prior = [(token, begin, end)]
    result = {}
    for name, positions in found.items():
        candidates = []
        for start in positions:
            header = re.match(r"async\s+" + name + r"\(([^)]*)\)\{", source[start:])
            if not header:
                continue
            opening = start + header.end() - 1
            if not source.startswith(GUARD, opening + 1):
                continue
            end = _balanced_end(source, opening)
            candidates.append({"start": start, "end": end, "source": source[start:end], "params": header[1].split(","), "body_start": opening + 1})
        _require(len(candidates) == 1, f"Missing or ambiguous AppServerRequestClient {name} method ({len(candidates)} candidates)")
        candidate = candidates[0]
        _require(sum(scope == owners[candidate["start"]] for _, scope in declarations[name]) == 1,
                 f"Ambiguous {name} declarations in target scope")
        result[name] = candidate
    return result


def analyze_source(source: str) -> dict:
    """Return exact semantic-anchor/edit evidence or raise CompatibilityError."""
    _require(MARKER not in source and HELPER not in source, "Patch marker/helper is already present; use an original bundle")
    consume_pattern = re.compile(r"(?P<plan>" + IDENT + r")\.canUsePrewarmedThread&&this\.params\.prewarmedThreadManager\.hasPrewarmedThread\(")
    consumers = list(consume_pattern.finditer(source))
    executable_positions = {m.start(): False for m in consumers}
    methods = _methods(source, executable_positions)
    send = methods["sendRequest"]
    pattern = (
        r"async\s+sendRequest\((?P<m>" + IDENT + r"),(?P<p>" + IDENT + r"),(?P<o>" + IDENT + r")\)\{" + re.escape(GUARD)
        + r"return (?P=m)===`config/read`\?this\.sendConfigReadRequest\((?P=p),(?P=o)\):this\.enqueueRequest\((?P=m),(?P=p),(?P=m)===`plugin/list`&&(?P=o)\?\.timeoutMs==null\?\{\.\.\.(?P=o),timeoutMs:(?P<timeout>" + IDENT + r")\}:(?P=o)\)\}"
    )
    match = re.fullmatch(pattern, send["source"])
    _require(match, "sendRequest control flow/dispatcher/config/plugin contract changed")
    _require(len(set(match.group(x) for x in ("m", "p", "o", "timeout"))) == 4, "Overlapping request parameters/timeout constant")
    edits = []
    insert = send["body_start"] + len(GUARD)
    assignment = f'{match["p"]}={HELPER}({match["m"]},{match["p"]},this.hostId);'
    edits.append({"kind": "send-request", "start": insert, "end": insert, "before": "", "after": assignment})

    prewarm = methods["prewarmThreadStart"]
    prefix_pattern = (
        r"async\s+prewarmThreadStart\((?P<p>" + IDENT + r"),(?P<o>" + IDENT + r")\)\{" + re.escape(GUARD)
        + r"let (?P<priority>" + IDENT + r")=(?P=o)\?\.priority\?\?`critical`,(?P<source>" + IDENT + r")=(?P<source_fn>" + IDENT + r")\(`thread/start`,(?P=o)\?\.source\)"
    )
    pm = re.match(prefix_pattern, prewarm["source"])
    _require(pm, "prewarmThreadStart dispatcher/priority/source prefix changed")
    _require(pm["p"] != pm["o"], "Overlapping prewarm parameters")
    body = prewarm["source"]
    enqueues = list(re.finditer(r"this\.enqueueRequest\(", body))
    _require(len(enqueues) == 1, "Prewarm must enqueue exactly one request")
    enqueue = enqueues[0]
    _require(body[enqueue.start():].startswith(f'this.enqueueRequest(`thread/start`,{pm["p"]},{{...{pm["o"]},priority:{pm["priority"]},'), "Prewarm request method/parameter/options contract changed")
    # A changed control-flow before enqueue needs review rather than guessing.
    middle = body[pm.end():enqueue.start()]
    trace_match = re.fullmatch(r",(?P<trace>" + IDENT + r")=" + re.escape(pm["o"]) + r"\?\.trace===void 0\?(?P<trace_fn>" + IDENT + r")\(\{attributes:\{\"app_server\.method\":`thread/start`,\"app_server\.priority\":" + re.escape(pm["priority"]) + r",\"app_server\.source\":" + re.escape(pm["source"]) + r"\},childName:`thread\.prewarm`,rootName:`desktop\.thread_prewarm`\}\):null;try\{let (?P<result>" + IDENT + r")=await ", middle)
    _require(trace_match, "Prewarm tracing/enqueue control flow changed")
    tail = ('this.enqueueRequest(`thread/start`,@p@,{...@o@,priority:@priority@,trace:@o@?.trace??@trace@?.trace??null},@callback@=>{this.dispatchMessage?.(`thread-prewarm-start`,{request:@callback@,savedEnvironmentConfig:@o@?.savedEnvironmentConfig,hostId:this.hostId,retainResponse:@o@?.onOutcomeUnknown!=null||void 0,priority:@priority@,source:@source@,timeoutMs:@o@?.timeoutMs,expiresAtMs:@o@?.timeoutMs!=null&&@o@.timeoutMs>0?Date.now()+@o@.timeoutMs:void 0,...@callback@.trace==null?{}:{dispatchedAtMs:Date.now()}})});return @trace@?.finish(),@result@}catch(@caught@){throw @trace@?.finish(@caught@),@caught@}}')
    names = {key: pm[key] for key in ("p", "o", "priority", "source")}
    names.update({key: trace_match[key] for key in ("trace", "result")})
    _require(len(set(names.values())) == len(names), "Overlapping prewarm local parameters")
    for key, value in names.items():
        tail = tail.replace("@" + key + "@", value)
    tail_pattern = re.escape(tail)
    for name in ("callback", "caught"):
        placeholder = re.escape("@" + name + "@")
        first = tail_pattern.find(placeholder)
        tail_pattern = tail_pattern[:first] + "(?P<" + name + ">" + IDENT + ")" + tail_pattern[first + len(placeholder):]
        tail_pattern = tail_pattern.replace(placeholder, "(?P=" + name + ")")
    _require(re.fullmatch(tail_pattern, body[enqueue.start():]), "Prewarm options/event/return control flow changed")
    _require(body.count('hostId:this.hostId') == 1 and body.count('`thread-prewarm-start`') == 1, "Prewarm event/host identity contract changed")
    _require('this.dispatchMessage?.(`thread-prewarm-start`,{' in body, "Prewarm dispatcher event contract changed")
    insert = prewarm["body_start"] + len(GUARD)
    assignment = f'{pm["p"]}={HELPER}(`thread/start`,{pm["p"]},this.hostId);'
    edits.append({"kind": "prewarm-request", "start": insert, "end": insert, "before": "", "after": assignment})

    # This spelling is a routing contract; minified plan variable may change.
    _require(len(consumers) == 1, "Missing or ambiguous prewarm reuse guard")
    _require(source.count('.consumePrewarmedThread(') == 1, "Missing or ambiguous prewarm consumption")
    consume = consumers[0]
    # Ensure guard is code rather than quoted text/comment.
    _require(executable_positions[consume.start()], "Prewarm reuse guard is not executable code")
    edits.append({"kind": "prewarm-consume", "start": consume.start(), "end": consume.start(), "before": "", "after": 'this.params.hostId!==`local`&&'})
    prefix_end = prewarm["body_start"] + len(GUARD)
    return {"format": FORMAT, "status": "compatible", "source_sha256": _sha(source), "source_length": len(source), "edits": sorted(edits, key=lambda x: x["start"]),
            "methods": methods, "request": dict(match.groupdict()), "prewarm": {**pm.groupdict(), "prefix": source[prewarm["start"]:prefix_end], "consume_guard": consume.group(), "consume_plan": consume["plan"]}}


def apply_source(source: str, route_js: str) -> tuple[str, dict]:
    """Apply only recorded insertions and append the local route helper."""
    analysis = analyze_source(source)
    _require(route_js.count(MARKER) == 1 and re.search(r"function\s+" + HELPER + r"\(", route_js), "Invalid route helper/marker")
    if "function codexPatchManagedRequest(" in route_js:
        request=analysis["request"]
        edit=next(e for e in analysis["edits"] if e["kind"]=="send-request")
        edit["after"]=(f'const __codexModelRequest=codexPatchManagedRequest(this,{request["m"]},{request["p"]},{request["o"]});'
                       'if(__codexModelRequest!==null)return await __codexModelRequest;'+edit["after"])
    result = source
    for edit in reversed(analysis["edits"]):
        _require(result[edit["start"]:edit["end"]] == edit["before"], "Source changed during patch application")
        result = result[:edit["start"]] + edit["after"] + result[edit["end"]:]
    result += "\n;\n" + route_js + "\n"
    send = analysis["methods"]["sendRequest"]["source"]
    send_edit = next(e for e in analysis["edits"] if e["kind"] == "send-request")
    local = send_edit["start"] - analysis["methods"]["sendRequest"]["start"]
    patched_send = send[:local] + send_edit["after"] + send[local:]
    prewarm_edit = next(e for e in analysis["edits"] if e["kind"] == "prewarm-request")
    # Execute the exact original guard plus actual inserted route assignment.
    patched_prewarm_probe = analysis["prewarm"]["prefix"] + prewarm_edit["after"] + f'return {analysis["prewarm"]["p"]};}}'
    evidence = {**analysis, "patched_sha256": _sha(result), "route_js": route_js,
                "probe": {"send_request": patched_send, "prewarm": patched_prewarm_probe,
                          "timeout_identifier": analysis["request"]["timeout"], "consume_guard": 'this.params.hostId!==`local`&&' + analysis["prewarm"]["consume_guard"]}}
    return result, evidence


def locate_bundle(archive: Path) -> tuple[str, str, dict]:
    """Discover one supported, integrity-verified hashed app-initial bundle."""
    from patch import archive_header, entries, read_member, check_member
    header, base, _ = archive_header(archive)
    candidates, failures = [], []
    for name, item in entries(header):
        if re.fullmatch(r"webview/assets/app-initial-[A-Za-z0-9_-]+\.js", name):
            data = read_member(archive, item, base)
            check_member(data, item)
            try:
                source = data.decode("utf-8")
                analysis = analyze_source(source)
            except (CompatibilityError, UnicodeDecodeError) as error:
                failures.append(f"{name}: {error}")
            else:
                candidates.append((name, source, analysis))
    _require(len(candidates) == 1, "Expected one compatible app-initial bundle; found " + str(len(candidates)) + ("; " + "; ".join(failures) if failures else ""))
    return candidates[0]
