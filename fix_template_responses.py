import re

pattern = re.compile(
    r'templates\.TemplateResponse\(\s*"([^"]+)",\s*\{\s*"request":\s*request\s*,?\s*',
    re.DOTALL
)

def repl(m):
    return f'templates.TemplateResponse(request, "{m.group(1)}", {{'

with open('main.py', encoding='utf-8') as f:
    content = f.read()

new_content, count = pattern.subn(repl, content)
print(f"Replaced {count} occurrences")

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(new_content)