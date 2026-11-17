#!/usr/bin/env python3
"""Migrate only ClinicalTrials tool grants to the native 2.x catalog."""
from __future__ import annotations

import argparse
import copy
import csv
import json
from pathlib import Path

PREFIX = 'clinicaltrialsgov-mcp-server_'
REPLACEMENTS = {
    'clinicaltrials_get_study': ['clinicaltrials_get_study_record', 'clinicaltrials_search_studies'],
    'clinicaltrials_analyze_trends': ['clinicaltrials_search_studies', 'clinicaltrials_get_study_count'],
    'clinicaltrials_compare_studies': ['clinicaltrials_search_studies', 'clinicaltrials_get_study_record'],
    'clinicaltrials_find_eligible_studies': ['clinicaltrials_find_eligible'],
}


def migrate_tools(tools: list) -> list:
    """Keep non-ClinicalTrials grants and metadata intact; never rewrite traces."""
    result, seen = [], set()
    clinical = False
    for item in tools:
        name = item if isinstance(item, str) else item['name']
        if not name.startswith(PREFIX):
            result.append(copy.deepcopy(item))
            continue
        clinical = True
        suffix = name[len(PREFIX):]
        for replacement in REPLACEMENTS.get(suffix, [suffix]):
            target = PREFIX + replacement
            if target in seen:
                continue
            seen.add(target)
            if isinstance(item, str):
                result.append(target)
            elif suffix not in REPLACEMENTS:
                result.append(copy.deepcopy(item))
            else:
                # Old required/optional parameters describe a different API.
                # The runtime supplies the new native input schema.
                result.append({'name': target})
    if clinical:
        metadata_tool = PREFIX + 'clinicaltrials_get_field_definitions'
        if metadata_tool not in seen:
            result.append(metadata_tool)
    return result


def migrate_csv(source: Path, destination: Path) -> int:
    csv.field_size_limit(100_000_000)
    with source.open(newline='', encoding='utf-8') as handle:
        reader = csv.DictReader(handle)
        fields, rows = reader.fieldnames, list(reader)
    changed = 0
    for row in rows:
        tools = json.loads(row['ENABLED_TOOLS'])
        migrated = migrate_tools(tools)
        if tools != migrated:
            row['ENABLED_TOOLS'] = json.dumps(migrated, ensure_ascii=False)
            changed += 1
    with destination.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return changed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print(f'Updated tool grants for {migrate_csv(args.source, args.destination)} tasks')
