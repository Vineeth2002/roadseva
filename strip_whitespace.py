import re

for f in ['database.py', 'tests/test_routes.py']:
    with open(f, encoding='utf-8') as fh:
        content = fh.read()
    cleaned = re.sub(r'[ \t]+\n', '\n', content)
    with open(f, 'w', encoding='utf-8') as fh:
        fh.write(cleaned)
    print(f'cleaned: {f}')

print('done')