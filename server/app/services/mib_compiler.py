"""Offline ASN.1 -> JSON symbol compilation. Never execute uploaded MIBs."""
from __future__ import annotations
import json
import hashlib
from pathlib import Path

# Standard OID roots; syntax/type dependencies must be supplied as MIBs when
# required by the generator. No HTTP readers or generated Python are used.
ROOTS = {'iso': (1,), 'org': (1, 3), 'dod': (1, 3, 6), 'internet': (1, 3, 6, 1),
         'directory': (1, 3, 6, 1, 1), 'mgmt': (1, 3, 6, 1, 2), 'mib_2': (1, 3, 6, 1, 2, 1),
         'transmission': (1, 3, 6, 1, 2, 1, 10), 'experimental': (1, 3, 6, 1, 3),
         'private': (1, 3, 6, 1, 4), 'enterprises': (1, 3, 6, 1, 4, 1),
         'security': (1, 3, 6, 1, 5), 'snmpV2': (1, 3, 6, 1, 6),
         'snmpDomains': (1, 3, 6, 1, 6, 1), 'snmpProxys': (1, 3, 6, 1, 6, 2),
         'snmpModules': (1, 3, 6, 1, 6, 3)}


def compile_sources(sources):
    from pysmi.parser.smi import parserFactory
    from pysmi.codegen.symtable import SymtableCodeGen
    from pysmi.codegen.jsondoc import JsonCodeGen
    tables = {'SNMPv2-SMI': {k: {'oid': v, 'type': 'MibIdentifier', 'origName': k}
                            for k, v in ROOTS.items()}}
    asts, errors, objects = {}, {}, []
    for filename, content in sources.items():
        try:
            for ast in parserFactory()().parse(content):
                if ast[0] in asts:
                    raise ValueError(f'Duplicate module {ast[0]}')
                _, table = SymtableCodeGen().gen_code(ast, {})
                tables[ast[0]] = table
                asts[ast[0]] = (filename, ast)
        except Exception as exc:
            errors[filename] = str(exc)[:500]
    compiled = []
    for module, (filename, ast) in asts.items():
        try:
            _, document = JsonCodeGen().gen_code(ast, tables, genTexts=True)
            doc = json.loads(document)
            for name, value in doc.items():
                if isinstance(value, dict) and value.get('oid'):
                    objects.append({'module': module, 'symbol': name, 'oid': value['oid'],
                                    'class': value.get('class'), 'syntax': value.get('syntax'),
                                    'description': value.get('description', '')})
            compiled.append(module)
        except Exception as exc:
            errors[filename] = 'Compilation failed; check imported dependencies: ' + str(exc)[:500]
    return {'format_version': 1, 'compiled_modules': compiled, 'errors': errors, 'objects': objects}


def compile_directory(directory, filenames):
    sources, hashes, size = {}, {}, 0
    for filename in filenames:
        if Path(filename).name != filename:
            raise ValueError('Invalid MIB filename')
        data = (directory / filename).read_bytes()
        size += len(data)
        if len(data) > 4 * 1024**2 or size > 32 * 1024**2:
            raise ValueError('Compilation input exceeds 32 MB total / 4 MB per file')
        sources[filename] = data.decode('utf-8-sig')
        hashes[filename] = hashlib.sha256(data).hexdigest()
    result = compile_sources(sources)
    result['source_hashes'] = hashes
    return result


def resolve_symbol(index, symbol):
    module, sep, name = symbol.partition('::')
    if not sep:
        name, module = module, None
    matches = [o for o in index['objects'] if o['symbol'] == name and (module is None or o['module'] == module)]
    if len(matches) != 1:
        raise ValueError('Symbol missing or ambiguous; use MODULE::symbol and compile its dependencies')
    return matches[0]


if __name__ == '__main__':
    import sys
    request = json.load(sys.stdin)
    print(json.dumps(compile_directory(Path(request['directory']), request['filenames'])))
