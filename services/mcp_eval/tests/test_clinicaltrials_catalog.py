"""Verify native 2.x grants without changing benchmark questions or evidence."""
import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
from migrate_clinicaltrials_catalog import migrate_tools, PREFIX
NATIVE = {'clinicaltrials_search_studies', 'clinicaltrials_get_study_record',
          'clinicaltrials_get_study_count', 'clinicaltrials_get_field_definitions',
          'clinicaltrials_get_field_values', 'clinicaltrials_get_study_results', 'clinicaltrials_find_eligible'}


def test_baseline_changes_only_tool_grants():
    csv.field_size_limit(100_000_000)
    with (ROOT/'services/mcp_eval/MCP-Atlas-origin.csv').open(newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 500
    evidence = [{k:v for k,v in row.items() if k != 'ENABLED_TOOLS'} for row in rows]
    assert hashlib.sha256(json.dumps(evidence, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest() == '61039a2ea1b6283643c7c24ff520ae4e374cc41fd43f1c3d0997df4ef273702e'
    affected = 0
    for row in rows:
        tools = json.loads(row['ENABLED_TOOLS'])
        assert migrate_tools(tools) == tools
        names = [t if isinstance(t,str) else t['name'] for t in tools]
        clinical = [n[len(PREFIX):] for n in names if n.startswith(PREFIX)]
        if clinical:
            affected += 1
            assert set(clinical) <= NATIVE
            assert len(clinical) == len(set(clinical))
            assert 'clinicaltrials_get_field_definitions' in clinical
    assert affected == 63


def test_migration_preserves_other_tools_and_removes_old_parameter_metadata():
    unrelated = {'name':'other_tool','requiredParams':['untouched']}
    old = [unrelated, {'name':PREFIX+'clinicaltrials_get_study','requiredParams':['nctIds']},
           PREFIX+'clinicaltrials_search_studies', PREFIX+'clinicaltrials_analyze_trends']
    out = migrate_tools(old)
    assert out[0] == unrelated
    assert {'name':PREFIX+'clinicaltrials_get_study_record'} in out
    names = [t if isinstance(t,str) else t['name'] for t in out]
    assert names.count(PREFIX+'clinicaltrials_search_studies') == 1
    assert PREFIX+'clinicaltrials_get_study_count' in names
    assert old[1]['requiredParams'] == ['nctIds']


def test_clinicaltrials_runtime_is_independent_of_other_node_mcps():
    root=ROOT/'services/agent-environment'
    servers=json.loads((root/'src/agent_environment/mcp_server_template.json').read_text())['mcpServers']
    clinical=servers['clinicaltrialsgov-mcp-server']
    assert clinical['command']=='/opt/clinicaltrials-node/bin/node'
    assert clinical['args']==['/opt/clinicaltrials-runtime/node_modules/clinicaltrialsgov-mcp-server/dist/index.js']
    assert all(s.get('command')!=clinical['command'] for n,s in servers.items() if n!='clinicaltrialsgov-mcp-server')
    lock=json.loads((root/'clinicaltrials-runtime/package-lock.json').read_text())['packages']
    assert lock['node_modules/clinicaltrialsgov-mcp-server']['version']=='2.11.3'
    assert lock['node_modules/@cyanheads/mcp-ts-core']['version']=='0.13.22'
    for file in [root/'Dockerfile',ROOT/'services/atlas-runtime/Dockerfile']:
        text=file.read_text()
        assert 'FROM node:24.22.3-bookworm-slim@sha256:' in text
        assert 'COPY --from=clinicaltrials-node /usr/local/bin/node /opt/clinicaltrials-node/bin/node' in text
        assert 'setup_22.x' in text
