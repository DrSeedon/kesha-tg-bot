from pathlib import Path
import runpy
import subprocess
import tempfile

notes = Path(__file__).resolve().parent
spec = runpy.run_path(str(notes / 'build-websearch-patch.py'))
patch = (notes / 'websearch-index.patch').resolve()
with tempfile.TemporaryDirectory(prefix='websearch-fixture-', dir=notes) as name:
    fixture = Path(name)
    lines = ['// Unavailable source context: synthetic fixture only.\n'] * 280
    for start, old, new in spec['index_chunks']:
        old_lines = old.splitlines(True)
        lines[start - 1:start - 1 + len(old_lines)] = old_lines
    index_before = ''.join(lines)
    readme_lines = ['Synthetic README context.\n'] * 65
    readme_lines[52:56] = spec['README_OLD'].splitlines(True)
    readme_before = ''.join(readme_lines)
    (fixture / 'index.js').write_text(index_before)
    (fixture / 'README.md').write_text(readme_before)
    (fixture / '.gitignore').write_text(spec['IGNORE_OLD'])
    command = ['git', 'apply', '--check', str(patch)]
    plain = subprocess.run(command, cwd=fixture, text=True, capture_output=True)
    print('Plain git apply --check on supplied-fragment fixture:', plain.returncode)
    if plain.returncode:
        print(plain.stderr, end='')
        print('Missing adjacent source context prevents plain git apply; checking with --unidiff-zero separately.')
        subprocess.run(['git', 'apply', '--check', '--unidiff-zero', str(patch)], cwd=fixture, check=True)
        subprocess.run(['git', 'apply', '--unidiff-zero', str(patch)], cwd=fixture, check=True)
    else:
        subprocess.run(['git', 'apply', str(patch)], cwd=fixture, check=True)
    expected_index = index_before
    for start, old, new in spec['index_chunks']:
        assert expected_index.count(old) == 1
        expected_index = expected_index.replace(old, new, 1)
    assert (fixture / 'index.js').read_text() == expected_index
    assert (fixture / 'README.md').read_text() == readme_before.replace(spec['README_OLD'], spec['README_NEW'], 1)
    assert (fixture / '.gitignore').read_text() == spec['IGNORE_NEW']
    subprocess.run(['git', 'apply', '--reverse', '--check', '--unidiff-zero', str(patch)], cwd=fixture, check=True)
    subprocess.run(['git', 'apply', '--reverse', '--unidiff-zero', str(patch)], cwd=fixture, check=True)
    assert (fixture / 'index.js').read_text() == index_before
    assert (fixture / 'README.md').read_text() == readme_before
    assert (fixture / '.gitignore').read_text() == spec['IGNORE_OLD']
    print('PASS: applying the patch changes exactly the supplied fragments; reverse application restores all fixture files.')
    print('This fixture contains supplied fragments only, not the real MCP repository.')
