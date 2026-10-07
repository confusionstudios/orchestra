"""Preview or copy literal shell role exports into ignored shared preferences."""
from __future__ import annotations

import argparse
from copy import deepcopy
import os
from pathlib import Path
import re
import stat
import tempfile

import yaml
import agent_registry

PREFIX = 'ORCHESTRA_DEFAULT_'
NAME = re.compile(r'\bORCHESTRA_DEFAULT_[A-Z_]+\b')
EXPORT = re.compile(
    r"^[ \t]*export[ \t]+(?P<key>ORCHESTRA_DEFAULT_[A-Z_]+)="
    r"(?P<quote>['\"]?)(?P<agent>[A-Za-z0-9_.:/-]+)(?P=quote)"
    r"(?:[ \t]+#[^\r\n]*)?[ \t]*$"
)


def display_path(path: Path) -> str:
    try:
        return '~/' + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def extract_defaults(text: str) -> dict:
    defaults = {}
    for number, line in enumerate(text.split('\n'), 1):
        if line.lstrip().startswith('#') or not NAME.search(line):
            continue
        match = EXPORT.fullmatch(line)
        if match is None:
            raise ValueError(f'line {number}: expected one literal export NAME=agent; set unsupported declarations manually')
        key, agent = match['key'], match['agent']
        role = key.removeprefix(PREFIX).lower()
        if role not in agent_registry.ROLE_FALLBACKS:
            raise ValueError(f'line {number}: unknown Orchestra role')
        entry = {'agent': agent}
        if role in defaults and defaults[role] != entry:
            raise ValueError(f'line {number}: conflicting exports for {role}; choose its value manually')
        defaults[role] = entry
    return defaults


def validate_document(document: dict) -> None:
    # Validate through the actual registry, independently of the launched work repo
    # and this installation's existing preferences.
    with tempfile.TemporaryDirectory(prefix='orchestra-default-migration-') as directory:
        root = Path(directory)
        candidate = root / 'agents.local.yaml'
        candidate.write_text(yaml.safe_dump(document, sort_keys=False))
        try:
            agent_registry.EffectiveRegistry(root, shared_config_path=candidate)
        except ValueError as exc:
            raise ValueError(str(exc).replace(str(candidate), 'proposed shared configuration')) from exc


def merge_defaults(document: dict, defaults: dict) -> tuple[dict, list[str]]:
    validate_document(document)
    proposed = deepcopy(document)
    conflicts = []
    roles = proposed.setdefault('defaults', {})
    for role, entry in defaults.items():
        if role in roles and roles[role] != entry:
            conflicts.append(role)
        else:
            roles[role] = entry
    validate_document(proposed)
    return proposed, conflicts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zshrc', type=Path, default=Path.home() / '.zshrc', help='shell file to read as text')
    parser.add_argument('--config', type=Path, default=agent_registry.SHARED_CONFIG_PATH,
                        help='shared ignored agent configuration')
    parser.add_argument('--write', action='store_true', help='write the preview when no conflicts exist')
    args = parser.parse_args(argv)
    source, target = args.zshrc.expanduser().resolve(), args.config.expanduser().resolve()
    try:
        if source == target:
            raise ValueError('source and destination must differ; the shell file must remain untouched')
        defaults = extract_defaults(source.read_bytes().decode('utf-8'))
        original = target.read_bytes() if target.exists() else None
        document = yaml.safe_load(original) if original is not None else {'version': 1}
        proposed, conflicts = merge_defaults(document, defaults)
        print(f'Source: {display_path(source)} (read only)')
        print(f'Destination: {display_path(target)}')
        if not defaults:
            print('No literal Orchestra role exports found; nothing to write.')
            return 0
        print('\nProposed Configuration (Existing Conflicting Settings Preserved):')
        rendered = yaml.safe_dump(proposed, sort_keys=False)
        print(rendered, end='')
        print('\nShell exports still take precedence. Removing them is optional and manual;')
        print('clear inherited values from shells and tmux to use file-based defaults.')
        if conflicts:
            print('Conflicts: ' + ', '.join(conflicts) + '. Resolve them manually; no file was written.')
            return 1
        if original is not None and b'#' in original and document != proposed:
            print('Existing configuration contains comments or # text. Automatic rewriting is refused;')
            print('copy the proposed additions manually to preserve that content. No file was written.')
            return 1 if args.write else 0
        if not args.write:
            print('Preview only. Pass --write to save; restart workers and dashboards afterward.')
            return 0
        if original is not None and document == proposed:
            print('Already migrated; no file was written.')
            return 0
        # Avoid overwriting edits made while producing the preview.
        if (target.read_bytes() if target.exists() else None) != original:
            raise ValueError('destination changed during migration; retry the preview')
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.agents-migration-', dir=target.parent)
        try:
            if original is not None:
                os.fchmod(descriptor, stat.S_IMODE(target.stat().st_mode))
            with os.fdopen(descriptor, 'w') as output:
                output.write(rendered)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print('Saved shared defaults. Restart workers and dashboards to load them.')
        return 0
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print('Migration refused: ' + str(exc).replace(str(Path.home()), '~'))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
